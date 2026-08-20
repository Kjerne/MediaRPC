"""Discord RPC connection + presence wrapper."""
import os
import time
from pypresence import Presence as PyPresence

from . import rt


class Presence(PyPresence):
    """Wrapper around pypresence that injects activity type 2 (Listening) for progress bars"""

    def update(self, **kwargs):
        """Override update to inject activity type.
        type 2 (Listening) when playing → shows progress bar.
        type 3 (Watching) when paused → shows nothing extra (no ♫ 0:00)."""
        details = kwargs.get('details')
        state = kwargs.get('state')
        start = kwargs.get('start')
        end = kwargs.get('end')
        large_image = kwargs.get('large_image')
        large_text = kwargs.get('large_text')
        small_image = kwargs.get('small_image')
        small_text = kwargs.get('small_text')
        buttons = kwargs.get('buttons', [])
        paused = kwargs.get('paused', False)
        name = kwargs.get('name')  # header text after the verb (falls back to app name)
        activity_type = kwargs.get('activity_type')  # 2=Listening, 3=Watching; None=auto

        if activity_type is None:
            activity_type = 3 if paused else 2  # auto: Watching when paused, Listening when playing
        activity = {
            "type": activity_type,
        }

        # Custom activity name → header reads "<verb> <name>" (e.g. "Watching The
        # Mentalist") instead of the Discord app name. Discord may ignore this over
        # local IPC depending on client version; harmless if so.
        if name:
            activity["name"] = name

        if details:
            activity["details"] = details
        if state:
            activity["state"] = state

        if start is not None:
            activity["timestamps"] = {"start": start}
            if end is not None:
                activity["timestamps"]["end"] = end

        if large_image or large_text or small_image or small_text:
            activity["assets"] = {}
            if large_image:
                activity["assets"]["large_image"] = large_image
            if large_text:
                activity["assets"]["large_text"] = large_text
            if small_image:
                activity["assets"]["small_image"] = small_image
            if small_text:
                activity["assets"]["small_text"] = small_text

        if buttons:
            activity["buttons"] = buttons

        try:
            self.send_data(1, {
                "cmd": "SET_ACTIVITY",
                "args": {
                    "pid": os.getpid(),
                    "activity": activity
                },
                "nonce": str(time.time())
            })
        except Exception:
            super().update(**kwargs)


def connect_rpc():

    try:
        if rt.RPC:
            try:
                rt.RPC.close()
            except Exception:
                pass

        rt.RPC = Presence(rt.CLIENT_ID)
        rt.RPC.connect()
        rt.log("Connected to Discord RPC")
        rt.last_rpc_success  = time.time()
        rt.reconnect_attempts = 0
        return True

    except Exception as e:
        rt.log(f"Discord connection failed: {e}")
        rt.reconnect_attempts += 1
        return False


def check_rpc_health():
    """Reconnect a dead RPC without blocking the caller.

    The old version slept up to 300 s inline, freezing the whole presence loop
    (and the tray tooltip) during backoff. Instead we gate attempts behind
    rt.next_reconnect_time: if we're still inside the backoff window we return
    immediately and let the loop keep ticking; when the window passes we make one
    non-blocking attempt and schedule the next.
    """
    now = time.time()
    if now - rt.last_rpc_success <= 60:
        return
    if now < rt.next_reconnect_time:
        return  # still backing off - don't block, try again on a later tick

    rt.log("RPC appears dead, attempting reconnect...")
    if connect_rpc():
        rt.log("Auto-reconnect successful")
        rt.next_reconnect_time = 0.0
    else:
        backoff = min(300, 5 * (2 ** rt.reconnect_attempts))
        rt.next_reconnect_time = now + backoff
        rt.log(f"Auto-reconnect failed (attempt {rt.reconnect_attempts}), next try in {backoff}s")


def refresh_rpc(icon=None, item=None):
    rt.log("Manually refreshing RPC")
    rt.reconnect_attempts = 0
    rt.next_reconnect_time      = 0.0
    rt.last_item_id             = None
    rt.last_position            = 0
    rt.last_paused              = False
    rt.last_start               = None
    rt.last_end                 = None
    rt.last_mode                = None
    rt.last_paused_session      = None
    rt.last_paused_session_time = 0
    rt.last_http_reconcile      = 0
    rt.last_pushed_payload      = None
    rt.last_push_time           = 0.0
    connect_rpc()


def _resolve_button(kind, label, url, ctx):
    """Turn one RPC_BUTTONS spec into a Discord button dict, or None to skip.

    Static kinds always resolve; dynamic kinds (imdb/tmdb) resolve only when the
    current item's id is present in ctx, otherwise they're skipped so the slot
    falls through to the next configured button."""
    if kind == "static":
        return {"label": label, "url": url} if label and url else None
    if kind == "letterboxd":
        return {"label": "Letterboxd", "url": rt.LETTERBOXD_URL} if rt.LETTERBOXD_URL else None
    if kind == "serializd":
        return {"label": "Serializd", "url": rt.SERIALIZD_URL} if rt.SERIALIZD_URL else None
    if kind == "trakt":
        return {"label": "Trakt", "url": rt.TRAKT_URL} if rt.TRAKT_URL else None
    if kind == "imdb":
        iid = ctx.get("imdb_id")
        if iid:
            iid = str(iid)
            iid = iid if iid.startswith("tt") else f"tt{iid}"
            return {"label": "IMDb", "url": f"https://www.imdb.com/title/{iid}/"}
        return None
    if kind == "tmdb":
        tid = ctx.get("tmdb_id")
        if tid:
            mt = (ctx.get("media_type") or "movie").lower()
            path = "tv" if mt in ("tv", "episode", "series") else "movie"
            return {"label": "TMDB", "url": f"https://www.themoviedb.org/{path}/{tid}"}
        return None
    return None


def clear_for_change(changed):
    """Clear presence so Discord drops stale timestamps on an item/pause change.

    Shared by every source renderer. Also nulls last_pushed_payload so the guarded
    push that follows always fires even if the new signature happens to match the
    old one. No-op when nothing changed."""
    if not changed:
        return
    try:
        rt.RPC.clear()
        time.sleep(rt.CLEAR_SETTLE)
    except Exception:
        pass
    rt.last_pushed_payload = None


def push_presence(payload_sig, **update_kwargs):
    """Rate-limit guard shared by all renderers: push to Discord only when the
    payload actually changed or the heartbeat window elapsed (Discord silently
    drops updates over ~5/20s, and the dropped one is often the episode change).
    Returns True if an update was sent."""
    now = time.time()
    if payload_sig != rt.last_pushed_payload or now - rt.last_push_time > rt.RPC_HEARTBEAT:
        rt.RPC.update(**update_kwargs)
        rt.last_pushed_payload = payload_sig
        rt.last_push_time      = now
        return True
    return False


def build_buttons(context=None):
    """Build the presence buttons from the RPC_BUTTONS config (max 2, Discord's cap).

    context (optional) carries the current item's ids for dynamic buttons:
    {"imdb_id": ..., "tmdb_id": ..., "media_type": "tv"|"movie"}.
    Default config is "letterboxd,serializd" - identical to the old behaviour."""
    ctx = context or {}
    buttons = []
    for kind, label, url in rt.RPC_BUTTONS:
        b = _resolve_button(kind, label, url, ctx)
        if b:
            buttons.append(b)
            if len(buttons) >= 2:
                break
    return buttons
