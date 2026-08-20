"""Plezy player source via mpv IPC. Tested working with both Plex and Emby backends: the stream URL is classified per-play and resolved against the matching server (Plex api_cache / Emby API), branded accordingly."""
import os
import time
import glob
import json
import re
import sqlite3

from . import rt
from . import discord_rpc
from . import images
from . import metadata
from . import tray


def _mpv_ipc_get(props):
    """Query mpv properties over Plezy's Windows named pipe.
    Returns {prop: value} or None if the pipe isn't there (Plezy not playing)."""
    import ctypes
    from ctypes import wintypes
    k = ctypes.WinDLL("kernel32", use_last_error=True)
    k.CreateFileW.restype = wintypes.HANDLE
    k.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                              wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    k.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD,
                            ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    k.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
                           ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    OPEN_EXISTING = 3
    INVALID = wintypes.HANDLE(-1).value

    h = k.CreateFileW(rt.PLEZY_MPV_PIPE, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
    if h == INVALID or not h:
        return None
    out = {}
    try:
        buf = b""
        for i, p in enumerate(props, 1):
            msg = json.dumps({"command": ["get_property", p], "request_id": i}).encode() + b"\n"
            wr = wintypes.DWORD(0)
            k.WriteFile(h, msg, len(msg), ctypes.byref(wr), None)
            end = time.time() + 2
            val = None
            while time.time() < end:
                chunk = ctypes.create_string_buffer(8192)
                rd = wintypes.DWORD(0)
                ok = k.ReadFile(h, chunk, 8192, ctypes.byref(rd), None)
                if ok and rd.value:
                    buf += chunk.raw[:rd.value]
                    matched = False
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        try:
                            m = json.loads(line.decode(errors="replace"))
                        except Exception:
                            continue
                        if m.get("request_id") == i:
                            val = m.get("data") if m.get("error") == "success" else None
                            matched = True
                            break
                    if matched:
                        break
                else:
                    time.sleep(0.02)
            out[p] = val
    finally:
        k.CloseHandle(h)
    return out


def _plezy_parse_path(path):
    """Extract (base_url, token, part_id) from an mpv Plex stream path."""
    if not path:
        return None, None, None
    from urllib.parse import urlparse, parse_qs
    try:
        u = urlparse(path)
        base = f"{u.scheme}://{u.netloc}"
        token = parse_qs(u.query).get("X-Plex-Token", [None])[0]
        m = re.search(r"/library/parts/(\d+)", u.path)
        return base, token, (m.group(1) if m else None)
    except Exception:
        return None, None, None


def _parse_stream_source(path):
    """Classify an mpv stream URL as Plex or Emby playback.

    Plezy can play from a Plex server (path like /library/parts/<id> with an
    X-Plex-Token) or from an Emby/Jellyfin server (path like /Videos/<id>/stream
    or /Items/<id>/... with an api_key / X-Emby-Token). Returns
    (source, base_url, item_id) where source is 'plex', 'emby', or None."""
    if not path or not str(path).startswith("http"):
        return None, None, None
    from urllib.parse import urlparse, parse_qs
    try:
        u = urlparse(path)
        base = f"{u.scheme}://{u.netloc}"
        q = parse_qs(u.query)
        # Emby/Jellyfin: numeric or hex item id in /Videos|Items, api-key auth.
        m = re.search(r"/(?:Videos|Items)/([0-9A-Fa-f]+)", u.path)
        if m and ("api_key" in q or "X-Emby-Token" in q or "MediaSourceId" in q):
            return "emby", base, m.group(1)
        # Plex: /library/parts/<id> with X-Plex-Token.
        mp = re.search(r"/library/parts/(\d+)", u.path)
        if mp:
            return "plex", base, mp.group(1)
    except Exception:
        pass
    return None, None, None


def _emby_item_for_plezy(item_id):
    """Fetch full Emby metadata for an item Plezy is playing (cached ~30s).
    Uses the configured Emby SERVER/TOKEN, same as the native Emby source."""
    if not item_id or not rt.SERVER or not rt.TOKEN:
        return None
    now = time.time()
    cached = rt.plezy_emby_cache.get(item_id)
    if cached and now - cached[1] < 30:
        rt.plezy_emby_cache.move_to_end(item_id)
        return cached[0]
    try:
        url = f"{rt.SERVER}/Users/{rt.USER_ID}/Items/{item_id}" if rt.USER_ID else f"{rt.SERVER}/Items/{item_id}"
        r = rt.http.get(
            url,
            headers=rt.headers,
            params={"Fields": "ProviderIds,Genres,CommunityRating,OfficialRating,"
                              "CriticRating,ProductionYear,RunTimeTicks"},
            timeout=8,
        )
        if r.status_code == 200:
            item = r.json()
            if len(rt.plezy_emby_cache) >= rt.CACHE_MAX_SIZE:
                rt._evict_oldest(rt.plezy_emby_cache)
            rt.plezy_emby_cache[item_id] = (item, now)
            return item
        rt.log(f"[Plezy/Emby] item {item_id} fetch HTTP {r.status_code}")
    except Exception as e:
        rt.log(f"[Plezy/Emby] item fetch failed: {e}")
    return None


def _plezy_emby_activity(item, props):
    """Build an update_plezy_rpc-shaped activity from an Emby item + mpv props,
    branded as Emby. Mirrors the field shape the Plex renderer already consumes."""
    mtype = (item.get("Type") or "").lower()
    paused = bool(props.get("pause"))
    position = float(props.get("time-pos") or 0)
    duration = float(props.get("duration") or 0) or rt.ticks_to_sec(item.get("RunTimeTicks"))

    if mtype == "episode":
        name = item.get("SeriesName") or item.get("Name") or "Emby"
        season = item.get("ParentIndexNumber")
        episode = item.get("IndexNumber")
        ep_title = item.get("Name") or ""
        se = f"S{season}E{episode}" if (season is not None and episode is not None) else ""
        subtitle = f"{se} {ep_title}".strip()
        media_type = "tv"
        rating = item.get("CommunityRating")
        if rating is None:
            _g, series_comm, _o, _i, _t, _c, _y = metadata.get_series_info(item.get("SeriesId"))
            rating = series_comm
    else:
        name = item.get("Name") or "Emby"
        year = item.get("ProductionYear")
        subtitle = f"({year})" if year else ""
        media_type = "movie"
        rating = item.get("CommunityRating")

    try:
        rating = float(rating) if rating is not None else None
    except (TypeError, ValueError):
        rating = None

    providers = item.get("ProviderIds") or {}

    return {
        "type": media_type,
        "name": name,
        "subtitle": subtitle,
        "paused": paused,
        "position": position,
        "duration": duration,
        "rating": rating,
        "runtime": rt.ticks_to_sec(item.get("RunTimeTicks")) or None,
        "thumb": None,
        # get_poster returns an already-public (uploaded or https Emby) URL, which
        # _plezy_pick_poster passes straight through (it only re-squares tmdb URLs).
        "tmdb_poster": images.get_poster(item),
        "rating_key": item.get("Id"),
        "imdb_id": metadata._provider_id(providers, "Imdb", "IMDB", "imdb"),
        "tmdb_id": metadata._provider_id(providers, "Tmdb", "TheMovieDb", "tmdb"),
        "service": "emby",
    }


def _plezy_db_path():
    for d in glob.glob(rt._PLEZY_DATA_GLOB):
        p = os.path.join(d, "plezy_downloads.db")
        if os.path.exists(p):
            return p
    return None


def _plezy_metadata_for_part(part_id):
    """Resolve a Part id to its Plex metadata via Plezy's api_cache (cached per part)."""
    if not part_id:
        return None
    if part_id in rt.plezy_part_cache:
        rt.plezy_part_cache.move_to_end(part_id)
        return rt.plezy_part_cache[part_id]
    db = _plezy_db_path()
    if not db:
        return None
    meta = None
    try:
        # Copy db + WAL + SHM to a temp file so we see Plezy's most recent (still
        # in-WAL) api_cache writes. Opening the live DB immutable would miss them.
        import shutil, tempfile
        tmp = os.path.join(tempfile.gettempdir(), "embyrpc_plezy_cache.db")
        for suf in ("", "-wal", "-shm"):
            try:
                shutil.copy2(db + suf, tmp + suf)
            except FileNotFoundError:
                pass
        con = sqlite3.connect(tmp, timeout=2)
        try:
            for (data,) in con.execute("SELECT data FROM api_cache ORDER BY cached_at DESC"):
                if not data or part_id not in data:
                    continue
                try:
                    j = json.loads(data)
                except Exception:
                    continue
                items = (j.get("MediaContainer", {}) or {}).get("Metadata") or []
                for it in items:
                    for md in (it.get("Media") or []):
                        for pt in (md.get("Part") or []):
                            if str(pt.get("id")) == str(part_id):
                                meta = it
                                break
                        if meta:
                            break
                    if meta:
                        break
                if meta:
                    break
        finally:
            con.close()
    except Exception as e:
        rt.log(f"[Plex] api_cache lookup failed: {e}")
    if meta:
        if len(rt.plezy_part_cache) >= rt.CACHE_MAX_SIZE:
            rt._evict_oldest(rt.plezy_part_cache)
        rt.plezy_part_cache[part_id] = meta
    return meta


def plezy_init():
    """No network setup for the mpv-IPC source; just report readiness."""
    if not rt.PLEZY_ENABLED:
        return False
    rt.log(f"[Plex] mpv-IPC source enabled. If nothing shows, add this to Plezy's "
        f"mpv config: input-ipc-server={rt.PLEZY_MPV_PIPE}")
    return True


def _plezy_poster_url(thumb):
    """Upload a Plex art path to imgbb (Plex art needs a token, so it can't go to
    Discord directly). Uses the server base+token parsed from the mpv path."""
    if not thumb:
        return None
    now = time.time()
    if thumb in rt.plezy_meta_cache:
        url, ts = rt.plezy_meta_cache[thumb]
        if now - ts < rt.POSTER_CACHE_TTL:
            rt.plezy_meta_cache.move_to_end(thumb)
            return url
    uri = rt.plezy_state.get("server_uri")
    token = rt.plezy_state.get("server_token")
    if not uri or not token:
        return None
    src = f"{uri}{thumb}?X-Plex-Token={token}"
    url = images.upload_image(src, square=True) if rt.UPLOAD_ENABLED else None
    if len(rt.plezy_meta_cache) >= rt.CACHE_MAX_SIZE:
        rt._evict_oldest(rt.plezy_meta_cache)
    rt.plezy_meta_cache[thumb] = (url, now)
    return url


def _plezy_pick_poster(activity):
    """Always yield a square, Discord-safe image (or the fallback logo).

    Discord crops large_image to a square, so a raw portrait poster looks zoomed.
    Prefer an already-squared imgbb URL; otherwise square the raw TMDB poster or
    the Plex art ourselves. Cached per item; falls back to the logo when imgbb is
    unreachable rather than sending a portrait that would be cropped."""
    rk = activity.get("rating_key")
    now = time.time()
    if rk and rk in rt.plezy_poster_cache:
        url, ts = rt.plezy_poster_cache[rk]
        if now - ts < rt.POSTER_CACHE_TTL and url:
            rt.plezy_poster_cache.move_to_end(rk)
            return url

    tp = activity.get("tmdb_poster")
    url = None
    if tp and "image.tmdb.org" not in tp:
        url = tp                                   # already a squared imgbb URL
    elif tp:
        url = images.upload_image(tp, square=True)        # raw TMDB portrait → square it
    if not url:
        url = _plezy_poster_url(activity.get("thumb"))  # fall back to Plex art (squared)

    if rk and url:
        if len(rt.plezy_poster_cache) >= rt.CACHE_MAX_SIZE:
            rt._evict_oldest(rt.plezy_poster_cache)
        rt.plezy_poster_cache[rk] = (url, now)
    return url or "emby"


def get_plezy_activity():
    """Read *this* Plezy player's live playback from mpv IPC + Plezy's metadata
    cache. Returns an activity dict or None."""
    if not (rt.PLEZY_ENABLED and rt._plezy_ready):
        return None
    now = time.time()
    if now - rt._plezy_last_poll < rt.PLEZY_POLL_INTERVAL:
        return rt._plezy_cached_activity
    rt._plezy_last_poll = now

    props = _mpv_ipc_get(rt._PLEZY_MPV_PROPS)
    if not props:
        rt._plezy_cached_activity = None
        return None
    path = props.get("path")
    if props.get("idle-active") or not path or not str(path).startswith("http"):
        rt._plezy_cached_activity = None
        return None

    # Plezy can play Emby content too - its stream path points at the Emby server,
    # not Plex. Resolve it through the Emby API and brand it as Emby.
    source, _emby_base, emby_id = _parse_stream_source(path)
    if source == "emby":
        if not rt.PLEZY_EMBY_ENABLED:
            rt._plezy_cached_activity = None
            return None
        item = _emby_item_for_plezy(emby_id)
        if item:
            activity = _plezy_emby_activity(item, props)
            rt._plezy_cached_activity = activity
            return activity
        # Metadata unavailable - fall through to the generic Plex handling below.

    # Everything else (classified Plex, or unresolved) is shown Plex-branded.
    if not rt.PLEZY_PLEX_ENABLED:
        rt._plezy_cached_activity = None
        return None

    base, token, part = _plezy_parse_path(path)
    if base and token:
        rt.plezy_state["server_uri"] = base
        rt.plezy_state["server_token"] = token

    md = _plezy_metadata_for_part(part) or {}
    mtype = md.get("type")
    paused = bool(props.get("pause"))
    position = float(props.get("time-pos") or 0)
    duration = float(props.get("duration") or 0) or (int(md.get("duration") or 0) / 1000.0)

    plex_rating = md.get("audienceRating") or md.get("rating")
    try:
        plex_rating = float(plex_rating) if plex_rating else None
    except ValueError:
        plex_rating = None

    if mtype == "episode":
        name = md.get("grandparentTitle") or md.get("title") or "Plex"
        season = md.get("parentIndex")
        episode = md.get("index")
        ep_title = md.get("title") or ""
        se = f"S{season} E{episode}" if (season is not None and episode is not None) else ""
        subtitle = f"{se} - {ep_title}".strip(" -") if (se or ep_title) else ""
        thumb = md.get("grandparentThumb") or md.get("art") or md.get("thumb")
        media_type = "tv"
        season_num = int(season) if (season is not None and str(season).isdigit()) else None
    elif mtype == "movie":
        name = md.get("title") or "Plex"
        year = md.get("year")
        subtitle = f"({year})" if year else (md.get("tagline") or "")
        thumb = md.get("thumb") or md.get("art")
        media_type = "movie"
        season_num = None
    else:
        # Metadata not resolved yet (cache miss) - still show live progress with a
        # generic title so the card isn't blank.
        name = "Plex"
        subtitle = ""
        thumb = None
        media_type = "movie"
        season_num = None

    # Mirror the other sources: prefer TMDB rating + poster, fall back to Plex's own.
    meta = metadata.get_tmdb_media_info(name, media_type=media_type, season=season_num) if name and name != "Plex" else {}
    meta = meta or {}
    rating = meta.get("rating") if meta.get("rating") is not None else plex_rating

    activity = {
        "type": mtype,
        "name": name,
        "subtitle": subtitle,
        "paused": paused,
        "position": position,
        "duration": duration,
        "rating": rating,
        "runtime": meta.get("runtime"),
        "thumb": thumb,
        "tmdb_poster": meta.get("poster"),
        "tmdb_id": meta.get("tmdb_id"),
        "rating_key": md.get("ratingKey") or part,
    }
    rt._plezy_cached_activity = activity
    return activity


def update_plezy_rpc(activity):

    if not rt.RPC:
        discord_rpc.connect_rpc()
        if not rt.RPC:
            return

    try:
        # Branding is always Plezy regardless of Plex/Emby backend - it's the app
        # the user is actually watching in.
        svc      = "Plezy"
        name     = activity["name"]
        subtitle = activity["subtitle"]
        paused   = activity["paused"]
        position = activity["position"]
        duration = activity["duration"]
        rating   = activity["rating"]

        rt.debug(f"[{svc}] name={name!r} sub={subtitle!r} paused={paused} pos={position:.0f} dur={duration:.0f}")

        details = name
        state   = subtitle or ("Paused" if paused else "Watching")
        if rating:
            state = f"{state} • ⭐ {rating:.1f}" if state else f"⭐ {rating:.1f}"

        # Item change or pause toggle → clear so Discord drops the old timestamps
        # / re-renders the paused state cleanly (same as the Emby/Netflix paths).
        rk = activity.get("rating_key")
        item_changed  = rk != rt.last_plezy_ratingkey and rt.last_plezy_ratingkey is not None
        pause_changed = paused != rt.last_plezy_paused and rt.last_plezy_paused is not None
        discord_rpc.clear_for_change(item_changed or pause_changed)
        rt.last_plezy_ratingkey = rk
        rt.last_plezy_paused    = paused

        if not paused and duration > 0:
            start = int(time.time() - position)
            end   = int(time.time() + max(10, duration - position))
        else:
            start = None
            end   = None

        # Always a square, Discord-safe image (never a cropped portrait).
        poster    = _plezy_pick_poster(activity)
        small_img = (rt.STATUS_ICON_PAUSE if paused else rt.STATUS_ICON_PLAY) or None
        small_txt = "Paused" if paused else "Playing"

        large_text = metadata.build_large_text(rating, None, duration or activity.get("runtime"))
        if large_text == "Emby":
            large_text = svc

        btn_ctx = {
            "imdb_id": activity.get("imdb_id"),
            "tmdb_id": activity.get("tmdb_id"),
            "media_type": activity.get("type"),
        }
        payload_sig = (name, details, state, poster, small_img, small_txt, start, end, paused)
        if discord_rpc.push_presence(
            payload_sig,
            name=name or svc,
            activity_type=3,  # "Watching <name>"
            details=details,
            state=state,
            large_image=poster,
            large_text=large_text,
            small_image=small_img,
            small_text=small_txt,
            start=start,
            end=end,
            paused=paused,
            buttons=discord_rpc.build_buttons(btn_ctx),
        ):
            rt.debug(f"[{svc}] RPC.update OK")

        tray.set_icon(paused)
        tray.set_tooltip(f"{'Paused' if paused else 'Watching'} {svc} - {name}")
        rt.last_rpc_success = time.time()

    except Exception as e:
        rt.log(f"[{svc}] RPC update FAILED: {e}")
        discord_rpc.check_rpc_health()
