"""Silage desktop app source (native Silo client, Kotlin + embedded libmpv).

Silage exposes no local IPC (libmpv runs with its default config=no, so an
input-ipc-server can't be injected), so playback is read from the Silo server the
app streams from: GET /api/v2/profiles/household/sessions lists the account's
live sessions with title, S/E, position and pause state. Silage's own
State/playback-v2/requests.json names the server, profile and current session id,
so we pick exactly the session this PC is playing.

Auth: SILAGE_API_KEY if set, else a device-pairing sign-in on first run (the
user approves a code in the Silo web UI); the token pair is kept in
silage_auth.json next to .env and refreshed on 401. The result is rendered by the
browser bridge's Silo renderer, so it looks identical to Silo-in-Firefox."""
import os
import json
import re
import time
import threading
import webbrowser
from datetime import datetime

from . import rt


_auth_lock = threading.Lock()
_auth = {}               # {"origin", "access_token", "refresh_token"}
_last_poll = 0.0
_cached = None
_proc_cache = (0.0, False)
_pairing = False
_warned = set()


def _warn_once(key, msg):
    if key not in _warned:
        _warned.add(key)
        rt.log(msg)


def _auth_path():
    return os.path.join(os.path.dirname(rt.dotenv_path), "silage_auth.json")


def _load_auth():
    global _auth
    try:
        with open(_auth_path(), encoding="utf-8") as f:
            _auth = json.load(f) or {}
    except FileNotFoundError:
        _auth = {}
    except Exception as e:
        rt.log(f"[Silage] auth file unreadable: {e}")
        _auth = {}


def _save_auth():
    try:
        with open(_auth_path(), "w", encoding="utf-8") as f:
            json.dump(_auth, f)
    except Exception as e:
        rt.log(f"[Silage] could not save auth file: {e}")


def _is_running():
    """True when silage.exe is up (cached a few seconds; psutil scans are slow)."""
    global _proc_cache
    now = time.time()
    if now - _proc_cache[0] < rt.EMBY_PROC_CACHE_TTL:
        return _proc_cache[1]
    running = True  # without psutil, rely on the session poll alone
    if rt.PSUTIL_AVAILABLE:
        import psutil
        running = False
        for p in psutil.process_iter(["name"]):
            if (p.info.get("name") or "").lower() == "silage.exe":
                running = True
                break
    _proc_cache = (now, running)
    return running


def _local_request():
    """Silage's in-flight playback request: {origin, profileId, sessionId} or None."""
    for home in rt.SILAGE_HOMES:
        p = os.path.join(home, "userdata", "State", "playback-v2", "requests.json")
        if not os.path.exists(p):
            p = os.path.join(home, "State", "playback-v2", "requests.json")
        try:
            with open(p, encoding="utf-8") as f:
                reqs = json.load(f)
        except (FileNotFoundError, ValueError):
            continue
        except Exception as e:
            rt.debug(f"[Silage] requests.json read failed: {e}")
            continue
        if isinstance(reqs, list) and reqs:
            r = reqs[-1]
            return {"origin": (r.get("origin") or "").rstrip("/"),
                    "profileId": r.get("profileId"),
                    "sessionId": r.get("sessionId")}
    return None


def _server(req):
    return rt.SILAGE_SERVER or (req or {}).get("origin") or _auth.get("origin")


def _refresh(origin):
    rtok = _auth.get("refresh_token")
    if not rtok:
        return False
    try:
        r = rt.http.post(f"{origin}/api/v2/auth/refresh", json={"refresh_token": rtok}, timeout=8)
        if r.status_code == 200:
            j = r.json()
            _auth["access_token"] = j.get("access_token")
            _auth["refresh_token"] = j.get("refresh_token") or rtok
            _save_auth()
            return True
        rt.log(f"[Silage] token refresh HTTP {r.status_code} - pairing again")
    except Exception as e:
        rt.log(f"[Silage] token refresh failed: {e}")
        return False  # network blip: keep the tokens, retry next poll
    _auth.clear()
    _save_auth()
    return False


def _pair(origin):
    """Device-pairing sign-in. Opens the approval page and polls until approved."""
    global _pairing
    try:
        r = rt.http.post(f"{origin}/api/v2/auth/device/start", json={
            "client_purpose": "device_login",
            "device_name": "MediaRPC",
            "device_platform": "windows",
            "temporary": False,
        }, timeout=10)
        if r.status_code not in (200, 201):
            rt.log(f"[Silage] pairing start HTTP {r.status_code}: {r.text[:200]}")
            return
        j = r.json()
        code = j.get("device_code")
        url = j.get("verification_uri_complete") or j.get("verification_uri")
        interval = max(2, int(j.get("interval") or 5))
        deadline = time.time() + int(j.get("expires_in") or 600)
        rt.log(f"[Silage] Sign-in needed: approve code {j.get('user_code')} at {url}")
        try:
            webbrowser.open(url)
        except Exception:
            pass
        while rt.running and time.time() < deadline:
            time.sleep(interval)
            pr = rt.http.post(f"{origin}/api/v2/auth/device/poll", json={"device_code": code}, timeout=10)
            if pr.status_code != 200:
                if pr.status_code in (400, 404):
                    rt.log(f"[Silage] pairing ended (HTTP {pr.status_code})")
                    return
                continue
            pj = pr.json()
            toks = pj.get("tokens") or {}
            if toks.get("access_token"):
                with _auth_lock:
                    _auth.update(origin=origin, access_token=toks["access_token"],
                                 refresh_token=toks.get("refresh_token"))
                    _save_auth()
                rt.log("[Silage] ✓ signed in to Silo")
                return
            status = (pj.get("status") or "").lower()
            if status in ("denied", "expired", "cancelled", "canceled"):
                rt.log(f"[Silage] pairing {status}")
                return
            interval = max(interval, int(pj.get("poll_after") or 0))
        rt.log("[Silage] pairing timed out")
    except Exception as e:
        rt.log(f"[Silage] pairing failed: {e}")
    finally:
        _pairing = False


def _ensure_auth(origin):
    """Bearer token, or None while a pairing runs in the background."""
    global _pairing
    if rt.SILAGE_API_KEY:
        return rt.SILAGE_API_KEY
    with _auth_lock:
        if _auth.get("origin") and _auth.get("origin") != origin:
            _auth.clear()  # different server than the one we paired with
        if _auth.get("access_token"):
            return _auth["access_token"]
        if not _pairing:
            _pairing = True
            threading.Thread(target=_pair, args=(origin,), daemon=True, name="silage-pair").start()
    return None


def _fetch_sessions(origin, profile_id):
    token = _ensure_auth(origin)
    if not token:
        return None
    for attempt in (1, 2):
        hdrs = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        if profile_id:
            hdrs["X-Profile-Id"] = str(profile_id)
        r = rt.http.get(f"{origin}/api/v2/profiles/household/sessions", headers=hdrs, timeout=8)
        if r.status_code == 401 and attempt == 1 and not rt.SILAGE_API_KEY:
            with _auth_lock:
                if not _refresh(origin):
                    return None
                token = _auth.get("access_token")
            continue
        if r.status_code != 200:
            _warn_once(f"http{r.status_code}", f"[Silage] sessions HTTP {r.status_code}: {r.text[:200]}")
            return None
        return r.json().get("items") or []
    return None


def _ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _pick(sessions, req):
    sid = (req or {}).get("sessionId")
    pid = (req or {}).get("profileId")
    for s in sessions:
        if sid and str(s.get("id")) == str(sid):
            return s
    mine = [s for s in sessions
            if "silage" in " ".join(str(s.get(k) or "") for k in
                                   ("client_name", "client_label", "client_label_full", "client_user_agent")).lower()
            and (not pid or str(s.get("profile_id")) == str(pid))]
    return max(mine, key=lambda s: _ts(s.get("updated_at")) or 0) if mine else None


def _to_activity(s):
    """Map a PlaybackSession onto the browser-bridge payload shape (service=silo)."""
    series = s.get("series_name")
    mtype = (s.get("media_type") or "").lower()
    if series or mtype == "episode":
        title = series or s.get("media_title") or "Silo"
        se, ep = s.get("season_number"), s.get("episode_number")
        marker = f"S{se} E{ep}" if se is not None and ep is not None else ""
        name = s.get("episode_name") or (s.get("media_title") if series else "")
        subtitle = f"{marker} - {name}".strip(" -")
    else:
        title = s.get("media_title") or "Silo"
        subtitle = ""
    # Strip a trailing " (2026)": TMDB matches better without it and the renderer
    # re-appends the year from TMDB (same as silo.js).
    title = re.sub(r"\s*\(\d{4}\)\s*$", "", title).strip() or title
    try:
        position = float(s.get("position_seconds") or 0)
    except (TypeError, ValueError):
        position = 0.0
    try:
        duration = float(s.get("file_duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration > 7 * 86400:
        duration /= 1000.0  # milliseconds
    paused = bool(s.get("is_paused"))
    # position_seconds is as of updated_at; the renderer extrapolates from _received_at.
    received = _ts(s.get("updated_at")) or time.time()
    received = min(received, time.time())
    return {
        "active": True, "service": "silo", "mode": "playing",
        "title": title, "subtitle": subtitle, "paused": paused,
        "position": position, "duration": duration,
        "_received_at": received, "_session_id": s.get("id"),
    }


def silage_init():
    _load_auth()
    rt.log("[Silage] source enabled (Silo server sessions API)")
    return True


def get_silage_activity():
    """Live Silage playback on this PC, as a browser-bridge-style activity, or None."""
    global _last_poll, _cached
    if not rt.SILAGE_ENABLED or not _is_running():
        _cached = None
        return None
    now = time.time()
    if now - _last_poll < rt.SILAGE_POLL_INTERVAL:
        return _cached
    _last_poll = now
    req = _local_request()
    origin = _server(req)
    if not origin:
        _warn_once("noserver", "[Silage] no Silo server known - set SILAGE_SERVER in .env")
        _cached = None
        return None
    try:
        sessions = _fetch_sessions(origin, (req or {}).get("profileId"))
    except Exception as e:
        rt.debug(f"[Silage] sessions fetch failed: {e}")
        return _cached  # keep the last state through a network blip
    s = _pick(sessions or [], req)
    _cached = _to_activity(s) if s else None
    return _cached
