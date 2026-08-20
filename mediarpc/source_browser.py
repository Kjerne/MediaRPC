"""Netflix/Disney browser-extension bridge (HTTP server + presence)."""
import time
import json
import re
from http.server import BaseHTTPRequestHandler, HTTPServer

from . import rt
from . import discord_rpc
from . import metadata
from . import tray


class NetflixRpcHandler(BaseHTTPRequestHandler):
    def _send(self, status=204, body=b""):
        self.send_response(status)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_OPTIONS(self):
        self._send()

    def do_GET(self):
        if self.path not in ("/", "/bridge", "/status"):
            self._send(404, b"not found")
            return

        activity = get_netflix_activity()
        body = json.dumps({
            "ok": True,
            "bridgeEnabled": rt.BRIDGE_ENABLED,
            "hasActivity": activity is not None,
            "activity": activity
        }, indent=2).encode("utf-8")

        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):

        if self.path != "/bridge":
            self._send(404, b"not found")
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._send(400, b"bad json")
            return

        with rt.netflix_lock:
            if payload.get("active"):
                payload["_received_at"] = time.time()
                rt.netflix_activity = payload
                rt.last_netflix_update = payload["_received_at"]
            else:
                rt.netflix_activity = None
                rt.last_netflix_update = 0.0

        self._send()

    def log_message(self, format, *args):
        return


def netflix_bridge_loop():
    server = None
    try:
        server = HTTPServer((rt.BRIDGE_HOST, rt.BRIDGE_PORT), NetflixRpcHandler)
        server.timeout = 1
        rt.log(f"Browser bridge listening on http://{rt.BRIDGE_HOST}:{rt.BRIDGE_PORT}/bridge")
        while rt.running:
            server.handle_request()
    except Exception as e:
        rt.log(f"Browser bridge error: {e}")
    finally:
        if server:
            server.server_close()


def get_netflix_activity():
    if not rt.BRIDGE_ENABLED:
        return None

    with rt.netflix_lock:
        if not rt.netflix_activity:
            return None
        if time.time() - rt.last_netflix_update > rt.BRIDGE_ACTIVITY_TIMEOUT:
            return None
        activity = dict(rt.netflix_activity)

    # Respect per-service toggles: a disabled service reads as no activity, so the
    # loop skips it (and the Emby WebSocket stays up instead of yielding to it).
    service = (activity.get("service") or "netflix").lower()
    if not rt.SERVICE_ENABLED.get(service, True):
        return None
    return activity


def update_netflix_rpc(activity):

    if not rt.RPC:
        discord_rpc.connect_rpc()
        if not rt.RPC:
            return

    try:
        service = (activity.get("service") or "netflix").lower()
        svc_label = rt.SERVICE_LABELS.get(service, "Netflix")
        svc_logo = rt.SERVICE_LOGOS.get(service, rt.NETFLIX_LOGO_URL)

        title = activity.get("title") or ""
        subtitle = activity.get("subtitle") or ""
        mode = activity.get("mode") or "playing"
        paused = bool(activity.get("paused"))
        position = float(activity.get("position") or 0)
        duration = float(activity.get("duration") or 0)
        update_age = max(0, time.time() - float(activity.get("_received_at") or time.time()))
        if not activity.get("backgroundHeartbeat") and not paused and duration > 0:
            position = min(duration, position + update_age)

        rt.debug(f"[{svc_label}] mode={mode} title={title!r} subtitle={subtitle!r} paused={paused} pos={position:.0f} dur={duration:.0f}")

        if mode == "browsing":
            # Browsing presence is gated globally; when off, treat as idle.
            if not rt.BROWSING_ENABLED:
                try:
                    rt.RPC.clear()
                except Exception:
                    pass
                tray.set_icon(True)
                tray.set_tooltip("MediaRPC")
                return
            rt.RPC.update(
                name=svc_label,
                activity_type=3,  # "Watching <service>"
                details=f"Browsing {svc_label}",
                state="Choosing something to watch",
                large_image=svc_logo or None,
                large_text=svc_label,
                buttons=discord_rpc.build_buttons()
            )
            tray.set_icon(False)
            tray.set_tooltip(f"Browsing {svc_label}")
            rt.last_rpc_success = time.time()
            return

        if metadata.is_generic_netflix_title(title):
            title = svc_label
            subtitle = ""
            meta = {}
            media_type = "movie"
        else:
            meta = None
            media_type = None

        looks_like_episode = bool(subtitle) and (
            "episode" in subtitle.lower() or
            re.search(r"\bS?\d*E\d+\b", subtitle, re.IGNORECASE) is not None
        )
        season_match = re.search(r"\bS(\d+)\s*E\d+\b", subtitle, re.IGNORECASE)
        season_num = int(season_match.group(1)) if season_match else None
        if meta is None:
            media_type = "tv" if looks_like_episode else "movie"
            meta = metadata.get_tmdb_media_info(title, media_type=media_type, season=season_num)
            if media_type == "movie" and not meta:
                meta = metadata.get_tmdb_media_info(title, media_type="tv", season=season_num)
                media_type = "tv" if meta else "movie"

        meta = meta or {}  # guarantee dict so .get() calls below never crash

        display_title = title
        if meta.get("year"):
            display_title = f"{title} ({meta['year']})"

        if subtitle:
            state = subtitle
        elif meta.get("genres"):
            state = ", ".join(meta["genres"][:3])
        else:
            state = "Paused" if paused else "Watching Netflix"

        title_changed    = title != rt.last_netflix_title
        subtitle_changed = subtitle != rt.last_netflix_subtitle
        paused_changed   = paused != rt.last_netflix_paused
        # Recalculate when something meaningful changed, or we don't have a
        # start yet while playing.  Episode changes keep the same series title,
        # so subtitle_changed is what catches E21 -> E22 (new position/duration,
        # needs a fresh timer, and forces Discord to re-render the activity).
        # When already paused, start is always None - no point recalculating.
        needs_timer      = title_changed or subtitle_changed or paused_changed or (not paused and rt.last_netflix_start is None)

        # Mirror Emby's behaviour: clear Discord on a pause toggle (activity type
        # changes) and on an episode/title change. Without the clear, Discord keeps
        # the previous activity's timestamps and the bar stays pinned full on the
        # old episode even though we send fresh start/end. first-run (last_* None)
        # is skipped so we don't clear before the very first push.
        episode_changed = (subtitle_changed and rt.last_netflix_subtitle is not None) or \
                          (title_changed and rt.last_netflix_title is not None)
        discord_rpc.clear_for_change(paused_changed or episode_changed)

        if needs_timer:
            if not paused and duration > 0:
                start = int(time.time() - position)
                end   = int(time.time() + max(10, duration - position))
            elif not paused and position > 0:
                # Disney reports no duration - show an elapsed-only timer (no bar).
                start = int(time.time() - position)
                end   = None
            else:
                start = None
                end   = None
            rt.last_netflix_start    = start
            rt.last_netflix_end      = end
            rt.last_netflix_title    = title
            rt.last_netflix_subtitle = subtitle
            rt.last_netflix_paused   = paused
        else:
            start = rt.last_netflix_start
            end   = rt.last_netflix_end
            if paused:
                start = None
                end   = None

        large_text = metadata.build_large_text(meta.get("rating"), meta.get("official_rating"), duration or meta.get("runtime"))
        if large_text == "Emby":
            large_text = svc_label

        # Watching (type 3) doesn't render large_text as a visible line the way
        # Listening (type 2) did, so fold the rating onto the state line. Runtime
        # is dropped - the progress bar already shows it.
        if meta.get("rating"):
            state = f"{state} • ⭐ {meta['rating']:.1f}" if state else f"⭐ {meta['rating']:.1f}"
        elif meta.get("official_rating") and state:
            state = f"{state} • {meta['official_rating']}"

        poster_img = meta.get("poster") or svc_logo or None
        small_img  = (rt.STATUS_ICON_PAUSE if paused else rt.STATUS_ICON_PLAY) or None
        small_txt  = "Paused" if paused else "Playing"

        # Rate-limit guard: Discord silently drops updates over its ~5/20s cap.
        # This loop ticks every ~2s, so pushing unconditionally floods the cap and
        # the dropped update is often the episode change - leaving the old episode
        # and a full bar stuck. Only push when the payload actually changes (new
        # episode, pause, timer recalc) plus a periodic heartbeat.
        rt.debug(f"[{svc_label}] needs_timer={needs_timer} start={start} end={end}")
        btn_ctx = {"tmdb_id": meta.get("tmdb_id"), "media_type": media_type}
        payload_sig = (title, display_title, state, poster_img, large_text,
                       small_img, small_txt, start, end, paused)
        if discord_rpc.push_presence(
            payload_sig,
            name=title or svc_label,
            activity_type=3,  # "Watching <title>" - matches how video presences read
            details=display_title,
            state=state,
            large_image=poster_img,
            large_text=large_text,
            small_image=small_img,
            small_text=small_txt,
            start=start,
            end=end,
            paused=paused,
            buttons=discord_rpc.build_buttons(btn_ctx),
        ):
            rt.debug(f"[{svc_label}] RPC.update OK")

        tray.set_icon(paused)
        tray.set_tooltip(f"{'Paused' if paused else 'Watching'} {svc_label} - {title}")
        rt.last_rpc_success = time.time()

    except Exception as e:
        rt.log(f"[Netflix] RPC update FAILED: {e}")
        discord_rpc.check_rpc_health()
