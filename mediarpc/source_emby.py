"""Emby WebSocket + HTTP session source and presence."""
import time
import json
import re
try:
    import websocket
except ImportError:
    websocket = None
try:
    import psutil
except ImportError:
    psutil = None

from . import rt
from . import discord_rpc
from . import images
from . import metadata
from . import source_browser
from . import tray


def _make_ws_url():
    base = (rt.SERVER or "").rstrip("/")
    base = base.replace("https://", "wss://").replace("http://", "ws://")
    return f"{base}/embywebsocket?api_key={rt.TOKEN}&deviceId=mediarpc-discord-presence"


def _on_ws_open(ws):
    rt.ws_connected = True
    rt.ws_last_message_time = time.time()
    rt.log("WebSocket connected - subscribing to session updates")
    # Ask Emby to push session state every 1 s
    ws.send(json.dumps({"MessageType": "SessionsStart", "Data": "0,1000"}))


def _on_ws_message(ws, message):
    try:
        data = json.loads(message)
        if data.get("MessageType") == "Sessions":
            with rt.state_lock:
                rt.current_sessions = data.get("Data", [])
            rt.ws_last_message_time = time.time()
    except Exception as e:
        rt.log(f"WebSocket message error: {e}")


def _on_ws_close(ws, close_status_code, close_msg):
    rt.ws_connected = False
    rt.log(f"WebSocket closed (code={close_status_code})")


def _on_ws_error(ws, error):
    rt.ws_connected = False
    rt.log(f"WebSocket error: {error}")


def ws_loop():
    """Maintain a persistent WebSocket connection to Emby, reconnecting on failure.

    While Netflix is the active source we don't touch Emby at all - no point
    hammering an Emby server we aren't watching (and it stops the 502 reconnect
    spam when Emby is unreachable). The guard thread closes any live connection
    the moment Netflix takes over.
    """
    while rt.running:
        if source_browser.get_netflix_activity():
            time.sleep(3)
            continue
        try:
            url = _make_ws_url()
            rt.log(f"WebSocket connecting to Emby...")
            app = websocket.WebSocketApp(
                url,
                on_open=_on_ws_open,
                on_message=_on_ws_message,
                on_close=_on_ws_close,
                on_error=_on_ws_error,
            )
            rt.ws_app = app
            app.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            rt.log(f"WebSocket thread exception: {e}")
        finally:
            rt.ws_app = None
        if rt.running and not source_browser.get_netflix_activity():
            rt.log("WebSocket reconnecting in 10s...")
            time.sleep(10)


def ws_netflix_guard():
    """Close the Emby WebSocket as soon as Netflix becomes the active source."""
    while rt.running:
        try:
            if rt.ws_app is not None and rt.ws_connected and source_browser.get_netflix_activity():
                rt.log("Netflix active - closing Emby WebSocket")
                try:
                    rt.ws_app.close()
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(3)


def is_emby_app_running():
    if not rt.PSUTIL_AVAILABLE:
        return True

    # Cache the verdict: process enumeration is expensive and the loop calls this
    # every ~2 s. Re-scan at most once per EMBY_PROC_CACHE_TTL seconds.
    now = time.time()
    if now - getattr(is_emby_app_running, "_cache_ts", 0) < rt.EMBY_PROC_CACHE_TTL:
        return is_emby_app_running._cache_val

    try:
        emby_process_names = [
            "emby.client.winui.exe",
            "emby theater.exe",
            "embytheater.exe",
            "emby.exe",
            "emby server.exe"
        ]

        found_processes = []
        for proc in psutil.process_iter(['name']):
            try:
                proc_name = proc.info['name'].lower()
                if any(emby_name in proc_name for emby_name in emby_process_names):
                    found_processes.append(proc.info['name'])
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

        if found_processes:
            if not hasattr(is_emby_app_running, 'last_logged') or not is_emby_app_running.last_logged:
                rt.log(f"Detected Emby process: {', '.join(found_processes)}")
                is_emby_app_running.last_logged = True
            result = True
        else:
            is_emby_app_running.last_logged = False
            result = False

        is_emby_app_running._cache_val = result
        is_emby_app_running._cache_ts  = now
        return result

    except Exception as e:
        rt.log(f"Error checking Emby process: {e}")
        # Cache the optimistic default too, so a persistent error doesn't make us
        # re-scan every tick.
        is_emby_app_running._cache_val = True
        is_emby_app_running._cache_ts  = now
        return True


def fetch_sessions(timeout=10):
    try:
        r = rt.http.get(
            f"{rt.SERVER}/Sessions",
            headers=rt.headers,
            timeout=timeout
        )
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def get_reconciled_sessions():
    """Use WebSocket sessions, but periodically confirm them with HTTP.

    Emby can occasionally miss a WebSocket transition when playback pauses,
    changes item, or finishes. The HTTP check keeps Discord from getting stuck
    on an old cached NowPlayingItem until the user manually refreshes.
    """

    now = time.time()
    ws_is_fresh = rt.ws_connected and (now - rt.ws_last_message_time) <= max(10, rt.INTERVAL * 2)

    if ws_is_fresh:
        with rt.state_lock:
            sessions = list(rt.current_sessions)

        if now - rt.last_http_reconcile < rt.HTTP_RECONCILE_INTERVAL:
            return sessions

        # WS data is fresh - this probe is only a confirmation, and we already have
        # cached sessions to return, so cap it short: a hung Emby won't stall the loop.
        fresh_sessions = fetch_sessions(timeout=rt.RECONCILE_TIMEOUT)
        rt.last_http_reconcile = now
        if fresh_sessions is not None:
            with rt.state_lock:
                rt.current_sessions = fresh_sessions
            return fresh_sessions
        return sessions

    fresh_sessions = fetch_sessions()
    rt.last_http_reconcile = now
    if fresh_sessions is not None:
        with rt.state_lock:
            rt.current_sessions = fresh_sessions
        return fresh_sessions

    with rt.state_lock:
        return list(rt.current_sessions) if rt.ws_connected else []


def update_rpc(session):

    if not rt.RPC:
        discord_rpc.connect_rpc()
        if not rt.RPC:
            return

    try:
        item   = session["NowPlayingItem"]
        play   = session.get("PlayState", {})
        paused = play.get("IsPaused", False)

        runtime  = rt.ticks_to_sec(item.get("RunTimeTicks", 0))
        position = rt.ticks_to_sec(play.get("PositionTicks", 0))
        item_id  = item.get("Id")

        # Clear on item switch or pause toggle so Discord drops stale timestamps and
        # re-renders cleanly. One combined clear (not two) keeps the loop stall short.
        item_changed  = item_id != rt.last_item_id and rt.last_item_id is not None
        pause_changed = paused != rt.last_paused
        if item_changed:
            rt.log("Item changed, forcing Discord refresh...")
        if pause_changed:
            rt.log(f"Pause state changed: {'PAUSED' if paused else 'PLAYING'}")
        discord_rpc.clear_for_change(item_changed or pause_changed)

        position_jump = abs(position - rt.last_position) > (rt.INTERVAL + 2)
        near_end = runtime > 0 and position >= runtime - 5
        needs_timer_update = (
            item_id != rt.last_item_id or
            paused  != rt.last_paused  or
            position_jump or
            near_end  # prevent stale last_end from pinning bar at 100%
        )

        # Force HTTP reconcile when near episode end so new session data arrives fast
        if near_end and not paused:
            rt.last_http_reconcile = 0

        if needs_timer_update or rt.last_start is None:
            if not paused:
                start = int(time.time() - position)
                end   = max(int(time.time()) + 5, int(time.time() + (runtime - position)))
                rt.debug(f"Timer: Playing - start={start}, end={end}")
            else:
                start = None
                end   = None
                rt.debug("Timer: PAUSED - NO TIMESTAMPS (timer hidden)")

            rt.last_start = start
            rt.last_end   = end
        else:
            start = rt.last_start
            end   = rt.last_end
            if paused:
                start = None
                end   = None

        rt.last_item_id  = item_id
        rt.last_position = position
        rt.last_paused   = paused

        title      = item.get("Name")
        media_type = item.get("Type")

        if media_type == "Episode":
            series_name = item.get('SeriesName', 'TV Show')
            series_id   = item.get('SeriesId')
            season_id   = item.get('SeasonId')
            year        = item.get("ProductionYear")  # episode year - only used for display
            # Some libraries bake the year into SeriesName ("Yellowstone (2018)").
            # Don't append it again or we get a double year.
            already_has_year = re.search(r"\(\d{4}\)\s*$", series_name or "")
            year_str    = "" if already_has_year else (f" ({year})" if year else "")
            series_genres, series_community, series_official, series_imdb, series_tmdb, series_critic, series_year = metadata.get_series_info(series_id)
            genre_str = ", ".join(series_genres[:3])
            details   = f"{series_name}{year_str} • {genre_str}" if genre_str else f"{series_name}{year_str}"

            season  = item.get('ParentIndexNumber', '?')
            episode = item.get('IndexNumber', '?')
            state   = f"S{season}E{episode} {title}"

            # Rating fallback: Episode → Season → Series
            ep_community = item.get("CommunityRating")
            ep_critic    = item.get("CriticRating")
            season_community, season_critic = metadata.get_season_rating(season_id) if ep_community is None else (None, None)
            effective_community = (
                ep_community if ep_community is not None else
                season_community if season_community is not None else
                series_community
            )
            effective_critic = (
                ep_critic if ep_critic is not None else
                season_critic if season_critic is not None else
                series_critic
            )
            rt.debug(f"Episode rating chain: ep={ep_community}, season={season_community}, series={series_community} → using {effective_community}")

            # Use the series premiere year (not episode year) for TMDB/OMDB title searches
            rating     = metadata.resolve_rating(effective_community, series_official, series_tmdb, series_imdb, series_name, series_year, media_type="tv", critic_rating=effective_critic)
            large_text = metadata.build_large_text(rating, series_official, runtime)
            tooltip_text = f"{'⏸' if paused else '▶'} {series_name} - S{season}E{episode}"
            rpc_name = series_name
            btn_ctx  = {"imdb_id": series_imdb, "tmdb_id": series_tmdb, "media_type": "tv"}

        else:
            year     = item.get("ProductionYear")
            year_str = f" ({year})" if year else ""
            details  = f"{title}{year_str}"

            movie_genres = ", ".join(item.get("Genres", [])[:3])
            state = movie_genres if movie_genres else "Movie"

            movie_community = item.get("CommunityRating")
            movie_official  = item.get("OfficialRating")
            movie_critic    = item.get("CriticRating")
            providers       = item.get("ProviderIds") or {}
            movie_imdb      = metadata._provider_id(providers, "Imdb", "IMDB", "imdb")
            movie_tmdb      = metadata._provider_id(providers, "Tmdb", "TheMovieDb", "tmdb")
            if movie_community is None:
                fetched_community, fetched_official, fetched_imdb, fetched_tmdb, fetched_critic = metadata.get_item_rating(item_id)
                movie_community = fetched_community
                movie_official  = movie_official or fetched_official
                movie_critic    = movie_critic if movie_critic is not None else fetched_critic
                movie_imdb      = movie_imdb or fetched_imdb
                movie_tmdb      = movie_tmdb or fetched_tmdb
            rating     = metadata.resolve_rating(movie_community, movie_official, movie_tmdb, movie_imdb, title, year, media_type="movie", critic_rating=movie_critic)
            large_text = metadata.build_large_text(rating, movie_official, runtime)
            tooltip_text = f"{'⏸' if paused else '▶'} {title}{year_str}"
            rpc_name = title
            btn_ctx  = {"imdb_id": movie_imdb, "tmdb_id": movie_tmdb, "media_type": "movie"}

        poster    = images.get_poster(item)
        small_img = (rt.STATUS_ICON_PAUSE if paused else rt.STATUS_ICON_PLAY) or None
        small_txt = "Paused" if paused else "Playing"

        # Watching (type 3) shows "Watching <name>" in the header and only two text
        # lines (no large_text row), so fold the rating onto the state line - same
        # as the Netflix path. Runtime is dropped; the progress bar already shows it.
        if isinstance(rating, (int, float)) and rating:
            state = f"{state} • ⭐ {rating:.1f}" if state else f"⭐ {rating:.1f}"
        elif isinstance(rating, str) and rating and state:
            state = f"{state} • {rating}"

        # Rate-limit guard: only hit Discord when the payload actually changes
        # (new item, pause toggle, seek/timer recalc) plus a periodic heartbeat.
        # Without this we send ~10 updates/20s on the 2s WS tick and Discord
        # silently drops the ones over its 5/20s cap - which is exactly the
        # update that carries the next episode, leaving the bar pinned full.
        payload_sig = (rpc_name, details, state, poster if poster else "emby",
                       large_text, small_img, small_txt, start, end, paused)
        discord_rpc.push_presence(
            payload_sig,
            name=rpc_name,
            activity_type=3,  # "Watching <name>"
            details=details,
            state=state,
            large_image=poster if poster else "emby",
            large_text=large_text,
            small_image=small_img,
            small_text=small_txt,
            start=start,
            end=end,
            paused=paused,
            buttons=discord_rpc.build_buttons(btn_ctx),
        )

        tray.set_icon(paused)
        tray.set_tooltip(tooltip_text)
        rt.last_rpc_success = time.time()

        # Save session when paused so we can hold it if NowPlayingItem disappears.
        # Only reset the timestamp on the pause transition - holding it steady
        # ensures PAUSED_SESSION_HOLD actually expires after the configured time.
        if paused:
            if rt.last_paused_session is None:
                rt.last_paused_session_time = time.time()
            rt.last_paused_session = session
        else:
            rt.last_paused_session      = None
            rt.last_paused_session_time = 0

    except Exception as e:
        rt.log(f"RPC update error: {e}")
        discord_rpc.check_rpc_health()


def update_rpc_browsing(session):
    """Update RPC when browsing (not playing)"""

    if not rt.RPC:
        discord_rpc.connect_rpc()
        if not rt.RPC:
            return

    try:
        client = session.get("Client", "Unknown")

        rt.RPC.update(
            name="Emby",
            activity_type=3,  # "Watching Emby"
            details="Browsing Emby Library",
            state=f"Using {client}",
            large_image=rt.EMBY_LOGO_URL if rt.EMBY_LOGO_URL else "emby",
            large_text="Browsing Emby",
            buttons=discord_rpc.build_buttons()
        )

        tray.set_icon(False)
        tray.set_tooltip("Browsing Emby Library")
        rt.last_rpc_success = time.time()

    except Exception as e:
        rt.log(f"RPC browsing update error: {e}")
        discord_rpc.check_rpc_health()
