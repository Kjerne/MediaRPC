"""Config validation, main RPC loop, startup wiring."""
import os
import sys
import time
import threading

from . import rt
from . import discord_rpc
from . import images
from . import source_browser
from . import source_emby
from . import source_plezy
from . import tray


def validate_config():
    """Check all required .env keys are present. Exit with a clear message if not."""
    required = {
        "EMBY_SERVER":       rt.SERVER,
        "TOKEN":             rt.TOKEN,
        "DISCORD_CLIENT_ID": rt.CLIENT_ID,
        "EMBY_USER_ID":      rt.USER_ID,
    }
    optional = {
        "IMGBB_KEY": rt.IMGBB_KEY,
        "OMDB_KEY":  rt.OMDB_KEY,
        "TMDB_KEY":  rt.TMDB_KEY,
    }

    missing = [name for name, val in required.items() if not val]
    if missing:
        rt.log("FATAL: Missing required keys in .env:")
        for name in missing:
            rt.log(f"  ✗ {name}")
        rt.log("Fix these values and restart.")
        sys.exit(1)

    rt.log("Config OK:")
    for name in required:
        rt.log(f"  ✓ {name}")
    for name, val in optional.items():
        if val:
            rt.log(f"  ✓ {name}")
        else:
            rt.log(f"  ⚠ {name} not set - optional feature disabled")


def tooltip_heartbeat():
    """Keep the tray tooltip current when the RPC loop hasn't updated it recently."""
    while rt.running:
        time.sleep(10)
        if not rt.running:
            break
        if rt.paused_rpc:
            tray.set_tooltip("MediaRPC - Paused by user")
            continue
        # Only intervene if the loop has been quiet for a while
        if time.time() - rt.last_loop_time > rt.INTERVAL * 2 + 5:
            if not rt.ws_connected and rt.RPC is None:
                tray.set_tooltip("MediaRPC - No connection")
            elif not rt.ws_connected:
                tray.set_tooltip("MediaRPC - Server unreachable (retrying...)")
            elif rt.RPC is None:
                tray.set_tooltip("MediaRPC - Discord not connected")
            else:
                tray.set_tooltip("MediaRPC - Idle")


def rpc_loop():

    emby_was_running = True

    while rt.running:
        rt.last_loop_time = time.time()

        if rt.paused_rpc:
            time.sleep(1)
            continue

        try:
            sessions = source_emby.get_reconciled_sessions()

            def _leave_clear():
                # Clear Discord when switching away from a non-empty source.
                if rt.RPC and rt.last_mode is not None:
                    try:
                        rt.RPC.clear()
                        time.sleep(0.5)
                    except Exception as e:
                        rt.log(f"Clear on mode switch failed: {e}")

            # ---- Priority: Emby (playing) > Plezy > Netflix > Emby (browsing) > idle ----

            # Emby auto-pause: when the local Emby app isn't running we skip Emby
            # entirely (both playing and browsing) so Plezy/Netflix can take over,
            # instead of clearing and blocking them like before.
            emby_allowed = rt.EMBY_ENABLED
            if emby_allowed and rt.AUTO_PAUSE_ENABLED and rt.PSUTIL_AVAILABLE:
                emby_running = source_emby.is_emby_app_running()
                if emby_running and not emby_was_running:
                    rt.log("Emby app detected")
                elif not emby_running and emby_was_running:
                    rt.log("Emby app closed")
                emby_was_running = emby_running
                emby_allowed = emby_running

            # 1. EMBY playing (my session, or held paused session)
            session = None
            if emby_allowed:
                session = next(
                    (s for s in sessions if s.get("UserId") == rt.USER_ID and "NowPlayingItem" in s),
                    None
                )
                if not session and rt.last_paused_session is not None:
                    held_item = rt.last_paused_session.get("NowPlayingItem", {})
                    held_play = rt.last_paused_session.get("PlayState", {})
                    held_runtime = rt.ticks_to_sec(held_item.get("RunTimeTicks", 0))
                    held_position = rt.ticks_to_sec(held_play.get("PositionTicks", 0))
                    held_near_end = held_runtime > 0 and held_position >= max(0, held_runtime - 5)
                    if not held_near_end and time.time() - rt.last_paused_session_time < rt.PAUSED_SESSION_HOLD:
                        rt.debug("No live session - holding last paused state")
                        session = rt.last_paused_session
                    else:
                        rt.last_paused_session      = None
                        rt.last_paused_session_time = 0

            if session:
                if rt.last_mode != "playing":
                    _leave_clear()
                rt.last_mode = "playing"
                source_emby.update_rpc(session)
                time.sleep(2 if rt.ws_connected else rt.INTERVAL)
                continue

            # 2. PLEZY (local mpv playback - Plex or Emby backend, both shown as Plezy)
            plezy = source_plezy.get_plezy_activity()
            if plezy:
                if rt.last_mode != "plezy":
                    backend = "Emby" if plezy.get("service") == "emby" else "Plex"
                    rt.log(f"Switching to Plezy activity ({backend} backend)")
                    _leave_clear()
                rt.last_mode = "plezy"
                source_plezy.update_plezy_rpc(plezy)
                time.sleep(2)
                continue
            if rt.last_mode == "plezy":
                rt.log("Plezy activity ended - clearing presence")
                _leave_clear()
                rt.last_mode = None

            # 3. NETFLIX
            netflix = source_browser.get_netflix_activity()
            if netflix:
                if rt.last_mode != "netflix":
                    rt.log("Switching to Netflix browser activity")
                    _leave_clear()
                rt.last_mode = "netflix"
                source_browser.update_netflix_rpc(netflix)
                time.sleep(2)
                continue
            if rt.last_mode == "netflix":
                rt.log("Netflix activity ended - clearing presence")
                _leave_clear()
                rt.last_mode = None

            # 4. EMBY browsing (low priority - never overrides a real Plezy/Netflix watch)
            browsing_session = None
            if emby_allowed and rt.BROWSING_ENABLED:
                browsing_session = next(
                    (s for s in sessions if s.get("UserId") == rt.USER_ID and "NowPlayingItem" not in s),
                    None
                )

            if browsing_session:
                if rt.last_mode != "browsing":
                    _leave_clear()
                rt.last_mode = "browsing"
                source_emby.update_rpc_browsing(browsing_session)
            else:
                # 5. Idle - nothing playing anywhere
                if rt.RPC:
                    try:
                        rt.RPC.clear()
                    except Exception as e:
                        rt.log(f"RPC clear failed: {e}")
                        discord_rpc.check_rpc_health()
                tray.set_icon(True)
                tray.set_tooltip("MediaRPC - App not running" if not emby_allowed else "MediaRPC")

                rt.last_item_id             = None
                rt.last_start               = None
                rt.last_end                 = None
                rt.last_mode                = None
                rt.last_paused              = False
                rt.last_paused_session      = None
                rt.last_paused_session_time = 0

        except Exception as e:
            rt.log(f"Loop error: {e}")
            discord_rpc.check_rpc_health()

        # When WebSocket feeds us real-time data we only need the loop to
        # rate-limit Discord updates - a 2 s tick is plenty.
        # Without WebSocket, respect the full polling interval.
        time.sleep(2 if rt.ws_connected else rt.INTERVAL)


def _api_selftest():
    """Ping TMDB and OMDB once to confirm the keys work. Runs on a background
    thread so the two 10 s-timeout calls never delay the tray icon appearing."""
    rt.log(f"TMDB key: {'✓ loaded' if rt.TMDB_KEY else '✗ MISSING (check .env)'}")
    rt.log(f"OMDB key: {'✓ loaded' if rt.OMDB_KEY else '✗ MISSING (check .env)'}")
    if rt.TMDB_KEY:
        try:
            r = rt.http.get(
                "https://api.themoviedb.org/3/search/tv",
                params={"api_key": rt.TMDB_KEY, "query": "Better Call Saul", "language": "en-US"},
                timeout=10
            )
            if r.status_code == 200:
                results = r.json().get("results", [])
                if results:
                    avg = results[0].get("vote_average")
                    rt.log(f"TMDB test OK - Better Call Saul vote_average={avg}")
                else:
                    rt.log("TMDB test: no results returned (unexpected)")
            elif r.status_code == 401:
                rt.log("TMDB test FAILED - 401 Unauthorized (invalid key)")
            else:
                rt.log(f"TMDB test FAILED - HTTP {r.status_code}")
        except Exception as e:
            rt.log(f"TMDB test ERROR - {e}")
    if rt.OMDB_KEY:
        try:
            r = rt.http.get(
                "https://www.omdbapi.com/",
                params={"apikey": rt.OMDB_KEY, "t": "Better Call Saul", "type": "series"},
                timeout=10
            )
            if r.status_code == 200:
                data = r.json()
                if data.get("Response") == "True":
                    rt.log(f"OMDB test OK - Better Call Saul imdbRating={data.get('imdbRating')}")
                else:
                    rt.log(f"OMDB test FAILED - {data.get('Error')}")
            else:
                rt.log(f"OMDB test FAILED - HTTP {r.status_code}")
        except Exception as e:
            rt.log(f"OMDB test ERROR - {e}")


def main():
    rt.log("Starting MediaRPC...")

    if getattr(sys, 'frozen', False):
        exe_dir     = os.path.dirname(sys.executable)
        dotenv_path = os.path.join(exe_dir, '.env')
        if os.path.exists(dotenv_path):
            rt.log(f".env loaded from: {dotenv_path}")
        else:
            rt.log(f"WARNING: .env not found at: {dotenv_path}")
            rt.log("Place .env file next to the exe!")

    # Validate required config - exits with a clear message if anything is missing
    validate_config()

    rt.log(f"Server: {rt.SERVER}")
    rt.log(f"Update interval: {rt.INTERVAL}s (HTTP fallback) / 2s (WebSocket)")
    rt.log(f"Poster cache TTL: {rt.POSTER_CACHE_TTL // 3600}h")

    rt.log(f"Rating source order: {', '.join(rt.RATING_ORDER)}")
    rt.log(f"Emby source: {'ENABLED' if rt.EMBY_ENABLED else 'DISABLED (via .env)'}")

    if rt.EMBY_ENABLED:
        if rt.AUTO_PAUSE_ENABLED:
            if rt.PSUTIL_AVAILABLE:
                rt.log("Auto-pause when Emby closes: ENABLED")
                if source_emby.is_emby_app_running():
                    rt.log("✓ Emby app is running")
                else:
                    rt.log("⚠ Emby app NOT detected - RPC will pause until app opens")
            else:
                rt.log("Auto-pause when Emby closes: DISABLED (psutil not installed)")
        else:
            rt.log("Auto-pause when Emby closes: DISABLED (via .env)")

    # API key diagnostics - backgrounded so slow/failing lookups don't stall startup
    threading.Thread(target=_api_selftest, daemon=True, name="api-selftest").start()

    # Set the permanent status-icon URLs
    images.load_status_icons()

    discord_rpc.connect_rpc()

    if rt.BRIDGE_ENABLED:
        threading.Thread(target=source_browser.netflix_bridge_loop, daemon=True, name="browser-bridge").start()
        enabled_svcs  = [s for s, on in rt.SERVICE_ENABLED.items() if on]
        disabled_svcs = [s for s, on in rt.SERVICE_ENABLED.items() if not on]
        rt.log(f"Browser bridge: ENABLED - services on: {', '.join(enabled_svcs) or 'none'}"
               + (f"; off: {', '.join(disabled_svcs)}" if disabled_svcs else ""))
    else:
        rt.log("Browser bridge: DISABLED (via .env)")

    # Plex/Plezy source - init on a thread since first-run PIN sign-in blocks.
    if rt.PLEZY_ENABLED:
        threading.Thread(target=source_plezy.plezy_init, daemon=True, name="plex-init").start()
        rt.log(f"Plezy source: ENABLED - Plex backend: {'on' if rt.PLEZY_PLEX_ENABLED else 'off'}, "
               f"Emby backend: {'on' if rt.PLEZY_EMBY_ENABLED else 'off'}")
    else:
        rt.log("Plezy source: DISABLED (via .env)")

    # WebSocket thread - real-time session updates from Emby (only if Emby is on)
    if rt.EMBY_ENABLED and rt.WEBSOCKET_AVAILABLE:
        threading.Thread(target=source_emby.ws_loop, daemon=True, name="ws-loop").start()
        if rt.BRIDGE_ENABLED:
            threading.Thread(target=source_emby.ws_netflix_guard, daemon=True, name="ws-netflix-guard").start()
        rt.log("WebSocket thread started")
    elif not rt.EMBY_ENABLED:
        rt.log("WebSocket thread skipped (Emby source disabled)")
    else:
        rt.log("websocket-client not installed - falling back to HTTP polling only")

    # Main RPC update loop
    threading.Thread(target=rpc_loop, daemon=True, name="rpc-loop").start()

    # Tooltip heartbeat - keeps tray tooltip accurate when the loop is quiet
    threading.Thread(target=tooltip_heartbeat, daemon=True, name="tooltip-heartbeat").start()

    tray.tray()
