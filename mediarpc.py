import os
import sys
import time
import glob
import json
import base64
import re
import uuid
import sqlite3
import requests
import threading
import xml.etree.ElementTree as ET
from io import BytesIO
from http.server import BaseHTTPRequestHandler, HTTPServer
import ctypes
import ctypes.wintypes
import webbrowser
import winreg
import logging
from logging.handlers import RotatingFileHandler

from dotenv import load_dotenv
from pypresence import Presence as PyPresence
from PIL import Image, ImageFilter, ImageOps
import pystray

try:
    import websocket
    WEBSOCKET_AVAILABLE = True
except ImportError:
    WEBSOCKET_AVAILABLE = False
    print("Warning: websocket-client not installed. Real-time updates unavailable.")
    print("Install with: pip install websocket-client")

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    print("Warning: psutil not installed. Auto-pause when Emby closes will not work.")
    print("Install with: pip install psutil")


# -----------------------------
# Custom Presence wrapper for activity type support
# -----------------------------

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


# -----------------------------
# PyInstaller resource loader
# -----------------------------

def resource_path(relative):
    try:
        base = sys._MEIPASS
    except AttributeError:
        base = os.path.abspath(".")
    return os.path.join(base, relative)


# -----------------------------
# Load .env
# -----------------------------

if getattr(sys, 'frozen', False):
    exe_dir = os.path.dirname(sys.executable)
    dotenv_path = os.path.join(exe_dir, '.env')
    load_dotenv(dotenv_path)
else:
    load_dotenv()

SERVER   = os.getenv("EMBY_SERVER")
TOKEN    = os.getenv("TOKEN")
CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
USER_ID  = os.getenv("EMBY_USER_ID")
INTERVAL = int(os.getenv("UPDATE_INTERVAL", 15))
IMGBB_KEY = os.getenv("IMGBB_KEY")
OMDB_KEY  = os.getenv("OMDB_KEY")
TMDB_KEY  = os.getenv("TMDB_KEY")

# Zipline image host (preferred over ImgBB when configured)
ZIPLINE_URL     = (os.getenv("ZIPLINE_URL") or "").rstrip("/")   # e.g. https://zipline.example.com
ZIPLINE_TOKEN   = os.getenv("ZIPLINE_TOKEN")
ZIPLINE_EXPIRES = os.getenv("ZIPLINE_EXPIRES", "7d")            # 1h / 24h / 7d / 30d / never
ZIPLINE_FOLDER  = os.getenv("ZIPLINE_FOLDER", "MediaRPC")        # all uploads land in this folder
ZIPLINE_ENABLED = bool(ZIPLINE_URL and ZIPLINE_TOKEN)
UPLOAD_ENABLED  = bool(IMGBB_KEY or ZIPLINE_ENABLED)

AUTO_PAUSE_ENABLED = os.getenv("AUTO_PAUSE_WHEN_CLOSED", "true").lower() == "true"
NETFLIX_BROWSER_RPC = os.getenv("NETFLIX_BROWSER_RPC", "true").lower() == "true"
NETFLIX_RPC_HOST = os.getenv("NETFLIX_RPC_HOST", "127.0.0.1")
NETFLIX_RPC_PORT = int(os.getenv("NETFLIX_RPC_PORT", 5678))
NETFLIX_ACTIVITY_TIMEOUT = int(os.getenv("NETFLIX_ACTIVITY_TIMEOUT", 120))

# Plex / Plezy source - reads playback locally from Plezy's mpv IPC pipe.
# Requires this line in Plezy's mpv config: input-ipc-server=\\.\pipe\plezympv
PLEX_ENABLED         = os.getenv("PLEX_ENABLED", "true").lower() == "true"
PLEX_POLL_INTERVAL   = int(os.getenv("PLEX_POLL_INTERVAL", 3))   # seconds between mpv IPC polls

LETTERBOXD_URL = os.getenv("LETTERBOXD_URL", "https://letterboxd.com/Kjerne/")
SERIALIZD_URL  = os.getenv("SERIALIZD_URL", "https://www.serializd.com/user/Kjerne/profile")

POSTER_CACHE_TTL = 21600  # 6 hours
CACHE_MAX_SIZE   = 300

headers = {"X-Emby-Token": TOKEN}


# -----------------------------
# Runtime state
# -----------------------------

running    = True
paused_rpc = False
RPC        = None
poster_cache = {}   # {id: (url, timestamp)}
series_cache = {}   # {series_id: (info_dict, timestamp)}
omdb_cache   = {}   # {cache_key: (rating_float_or_None, timestamp)}
tmdb_cache   = {}   # {cache_key: (rating_float_or_None, timestamp)}
season_cache = {}   # {season_id: (community_rating, critic_rating, timestamp)}
netflix_meta_cache = {}  # {cache_key: (info_dict, timestamp)}

# Auto-reconnect tracking
last_rpc_success  = time.time()
reconnect_attempts = 0

# Thread safety: protects current_sessions (written by WS thread, read by RPC loop)
state_lock       = threading.Lock()
current_sessions = []   # kept up to date by WebSocket; HTTP fallback when WS is down
ws_connected     = False
ws_app           = None   # live WebSocketApp, so the Netflix guard can close it
ws_last_message_time = 0.0
last_http_reconcile  = 0.0
HTTP_RECONCILE_INTERVAL = max(5, min(15, INTERVAL))

# Netflix browser extension state
netflix_lock = threading.Lock()
netflix_activity = None
last_netflix_update = 0.0
NETFLIX_LOGO_URL = "https://upload.wikimedia.org/wikipedia/commons/thumb/0/08/Netflix_2015_logo.svg/512px-Netflix_2015_logo.svg.png"

# Playback state (written only by the rpc_loop thread)
last_item_id   = None
last_position  = 0
last_paused    = False
last_start     = None
last_end       = None

# Netflix timer state - mirrors the Emby caching so the timer doesn't jump every tick
last_netflix_title  = None
last_netflix_subtitle = None
last_netflix_paused = False
last_netflix_start  = None
last_netflix_end    = None
last_mode      = None   # "playing" | "browsing" | None

# Discord SET_ACTIVITY rate-limit guard (Discord drops >5 updates / 20s).
# Only push when the payload changes; otherwise heartbeat every 15s.
last_pushed_payload = None
last_push_time      = 0.0
RPC_HEARTBEAT       = 15

# Hold the last known paused session so we don't flicker to "Browsing"
# when Emby drops NowPlayingItem on pause
last_paused_session      = None
last_paused_session_time = 0
PAUSED_SESSION_HOLD      = 8    # seconds

# Tooltip heartbeat tracks when the loop last ran
last_loop_time = 0.0

icon_ref        = None
console_created = False

# Cached permanent URLs for play/pause small images (populated at startup)
STATUS_ICON_PLAY  = None
STATUS_ICON_PAUSE = None
EMBY_LOGO_URL     = None


# -----------------------------
# Logging
# -----------------------------

_log_path = os.path.join(
    os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__)),
    "MediaRPC.log"
)
_logger = logging.getLogger("MediaRPC")
_logger.setLevel(logging.DEBUG)
_fmt = logging.Formatter("[MediaRPC] %(asctime)s - %(message)s", datefmt="%H:%M:%S")
_fh = RotatingFileHandler(_log_path, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
_fh.setFormatter(_fmt)
class _SafeStreamHandler(logging.StreamHandler):
    """StreamHandler that binds to the *current* sys.stderr on each emit.

    In a frozen --noconsole build sys.stderr is None at startup, so a normal
    StreamHandler crashes on every log with 'NoneType' has no attribute 'write'.
    Once the tray "console" is opened, sys.stderr is reassigned to CONOUT$;
    reading it lazily here means logs go to the console when it exists and are
    silently dropped when it doesn't.
    """
    def emit(self, record):
        if sys.stderr is None:
            return
        self.stream = sys.stderr
        try:
            super().emit(record)
        except Exception:
            pass

_sh = _SafeStreamHandler()
_sh.setFormatter(_fmt)
_logger.addHandler(_fh)
_logger.addHandler(_sh)

def log(msg):
    _logger.info(msg)


def _evict_oldest(cache):
    if cache:
        oldest = min(cache, key=lambda k: cache[k][-1])
        del cache[oldest]


# -----------------------------
# Config validation
# -----------------------------

def validate_config():
    """Check all required .env keys are present. Exit with a clear message if not."""
    required = {
        "EMBY_SERVER":       SERVER,
        "TOKEN":             TOKEN,
        "DISCORD_CLIENT_ID": CLIENT_ID,
        "EMBY_USER_ID":      USER_ID,
    }
    optional = {
        "IMGBB_KEY": IMGBB_KEY,
        "OMDB_KEY":  OMDB_KEY,
        "TMDB_KEY":  TMDB_KEY,
    }

    missing = [name for name, val in required.items() if not val]
    if missing:
        log("FATAL: Missing required keys in .env:")
        for name in missing:
            log(f"  ✗ {name}")
        log("Fix these values and restart.")
        sys.exit(1)

    log("Config OK:")
    for name in required:
        log(f"  ✓ {name}")
    for name, val in optional.items():
        if val:
            log(f"  ✓ {name}")
        else:
            log(f"  ⚠ {name} not set - optional feature disabled")


# -----------------------------
# Status icon upload (play/pause small images)
# -----------------------------

def _upload_icon_to_imgbb(path):
    """Upload a PNG file to ImgBB with no expiration. Returns URL or None."""
    if not IMGBB_KEY:
        return None
    try:
        with open(path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        r = requests.post(
            "https://api.imgbb.com/1/upload",
            data={"key": IMGBB_KEY, "image": b64},
            timeout=20
        )
        if r.status_code == 200:
            return r.json()["data"]["url"]
        else:
            print(f"[MediaRPC] Icon upload failed ({r.status_code})")
    except Exception as e:
        print(f"[MediaRPC] Icon upload error: {e}")
    return None


def load_or_upload_status_icons():
    """Set permanent status icon URLs from Imgur (no upload needed)."""
    global STATUS_ICON_PLAY, STATUS_ICON_PAUSE, EMBY_LOGO_URL

    STATUS_ICON_PLAY  = "https://i.imgur.com/wDhrODz.png"
    STATUS_ICON_PAUSE = "https://i.imgur.com/4ZnvVao.png"
    EMBY_LOGO_URL     = "https://i.imgur.com/W9Wtkdn.png"

    log("Status icons loaded from permanent Imgur URLs")


# -----------------------------
# Windows Startup Management
# -----------------------------

def get_startup_command():
    """Return the full registry launch command for the current mode."""
    if getattr(sys, 'frozen', False):
        return f'"{sys.executable}"'
    # Script mode: Windows can't execute .py directly; use pythonw.exe (no console window)
    pythonw = os.path.join(os.path.dirname(sys.executable), 'pythonw.exe')
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    script = os.path.abspath(sys.argv[0])
    return f'"{pythonw}" "{script}"'


def is_autostart_enabled():
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_READ
        )
        try:
            winreg.QueryValueEx(key, "MediaRPC")
            winreg.CloseKey(key)
            return True
        except FileNotFoundError:
            winreg.CloseKey(key)
            return False
    except Exception:
        return False


def toggle_autostart(icon=None, item=None):
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_ALL_ACCESS
        )
        if is_autostart_enabled():
            winreg.DeleteValue(key, "MediaRPC")
            log("Auto-start disabled")
        else:
            cmd = get_startup_command()
            winreg.SetValueEx(key, "MediaRPC", 0, winreg.REG_SZ, cmd)
            log(f"Auto-start enabled: {cmd}")
        winreg.CloseKey(key)
    except Exception as e:
        _logger.exception(f"Auto-start toggle error: {e}")
    if icon_ref is not None:
        try:
            icon_ref.update_menu()
        except Exception:
            pass


# -----------------------------
# Console control
# -----------------------------

def show_console():
    global console_created
    if not console_created:
        ctypes.windll.kernel32.AllocConsole()
        sys.stdout = open("CONOUT$", "w")
        sys.stderr = open("CONOUT$", "w")
        # Remove the close button from the console's system menu.
        # CTRL_CLOSE_EVENT kills the process after a 5s timeout even if handled,
        # so the only reliable fix is to make the X button a no-op.
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            SC_CLOSE     = 0xF060
            MF_BYCOMMAND = 0x0000
            hmenu = ctypes.windll.user32.GetSystemMenu(hwnd, False)
            if hmenu:
                ctypes.windll.user32.DeleteMenu(hmenu, SC_CLOSE, MF_BYCOMMAND)
        console_created = True


def toggle_console(icon, item):
    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if hwnd == 0:
        show_console()
        log("Console opened")
    else:
        # Toggle visibility: hide if shown, show if hidden
        GWL_STYLE  = -16
        WS_VISIBLE = 0x10000000
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_STYLE)
        if style & WS_VISIBLE:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
        else:
            ctypes.windll.user32.ShowWindow(hwnd, 5)  # SW_SHOW


# -----------------------------
# Discord RPC with Auto-Reconnect
# -----------------------------

def connect_rpc():
    global RPC, last_rpc_success, reconnect_attempts

    try:
        if RPC:
            try:
                RPC.close()
            except Exception:
                pass

        RPC = Presence(CLIENT_ID)
        RPC.connect()
        log("Connected to Discord RPC")
        last_rpc_success  = time.time()
        reconnect_attempts = 0
        return True

    except Exception as e:
        log(f"Discord connection failed: {e}")
        reconnect_attempts += 1
        return False


def check_rpc_health():
    global last_rpc_success, reconnect_attempts

    if time.time() - last_rpc_success > 60:
        log("RPC appears dead, attempting reconnect...")
        backoff = min(300, 5 * (2 ** reconnect_attempts))
        if reconnect_attempts > 0:
            log(f"Waiting {backoff}s before reconnect (attempt {reconnect_attempts})")
            time.sleep(backoff)
        if connect_rpc():
            log("Auto-reconnect successful")
        else:
            log(f"Auto-reconnect failed (attempt {reconnect_attempts})")


def refresh_rpc(icon=None, item=None):
    global reconnect_attempts, last_item_id, last_position, last_paused
    global last_start, last_end, last_mode
    global last_paused_session, last_paused_session_time
    global last_http_reconcile
    global last_pushed_payload, last_push_time
    log("Manually refreshing RPC")
    reconnect_attempts = 0
    last_item_id             = None
    last_position            = 0
    last_paused              = False
    last_start               = None
    last_end                 = None
    last_mode                = None
    last_paused_session      = None
    last_paused_session_time = 0
    last_http_reconcile      = 0
    last_pushed_payload      = None
    last_push_time           = 0.0
    connect_rpc()


# -----------------------------
# Netflix browser extension bridge
# -----------------------------

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
        if self.path not in ("/", "/netflix", "/status"):
            self._send(404, b"not found")
            return

        activity = get_netflix_activity()
        body = json.dumps({
            "ok": True,
            "netflixBrowserRpc": NETFLIX_BROWSER_RPC,
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
        global netflix_activity, last_netflix_update

        if self.path != "/netflix":
            self._send(404, b"not found")
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            self._send(400, b"bad json")
            return

        with netflix_lock:
            if payload.get("active"):
                payload["_received_at"] = time.time()
                netflix_activity = payload
                last_netflix_update = payload["_received_at"]
            else:
                netflix_activity = None
                last_netflix_update = 0.0

        self._send()

    def log_message(self, format, *args):
        return


def netflix_bridge_loop():
    server = None
    try:
        server = HTTPServer((NETFLIX_RPC_HOST, NETFLIX_RPC_PORT), NetflixRpcHandler)
        server.timeout = 1
        log(f"Netflix browser bridge listening on http://{NETFLIX_RPC_HOST}:{NETFLIX_RPC_PORT}/netflix")
        while running:
            server.handle_request()
    except Exception as e:
        log(f"Netflix browser bridge error: {e}")
    finally:
        if server:
            server.server_close()


def get_netflix_activity():
    if not NETFLIX_BROWSER_RPC:
        return None

    with netflix_lock:
        if not netflix_activity:
            return None
        if time.time() - last_netflix_update > NETFLIX_ACTIVITY_TIMEOUT:
            return None
        return dict(netflix_activity)


def update_netflix_rpc(activity):
    global RPC, last_rpc_success
    global last_netflix_title, last_netflix_subtitle, last_netflix_paused, last_netflix_start, last_netflix_end
    global last_pushed_payload, last_push_time

    if not RPC:
        connect_rpc()
        if not RPC:
            return

    try:
        title = activity.get("title") or ""
        subtitle = activity.get("subtitle") or ""
        mode = activity.get("mode") or "playing"
        paused = bool(activity.get("paused"))
        position = float(activity.get("position") or 0)
        duration = float(activity.get("duration") or 0)
        update_age = max(0, time.time() - float(activity.get("_received_at") or time.time()))
        if not activity.get("backgroundHeartbeat") and not paused and duration > 0:
            position = min(duration, position + update_age)

        log(f"[Netflix] mode={mode} title={title!r} subtitle={subtitle!r} paused={paused} pos={position:.0f} dur={duration:.0f}")

        if mode == "browsing":
            RPC.update(
                name="Netflix",
                activity_type=3,  # "Watching Netflix"
                details="Browsing Netflix",
                state="Choosing something to watch",
                large_image=NETFLIX_LOGO_URL,
                large_text="Netflix",
                buttons=build_buttons()
            )
            set_icon(False)
            set_tooltip("Browsing Netflix")
            last_rpc_success = time.time()
            return

        if is_generic_netflix_title(title):
            title = "Netflix"
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
            meta = get_tmdb_media_info(title, media_type=media_type, season=season_num)
            if media_type == "movie" and not meta:
                meta = get_tmdb_media_info(title, media_type="tv", season=season_num)
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

        title_changed    = title != last_netflix_title
        subtitle_changed = subtitle != last_netflix_subtitle
        paused_changed   = paused != last_netflix_paused
        # Recalculate when something meaningful changed, or we don't have a
        # start yet while playing.  Episode changes keep the same series title,
        # so subtitle_changed is what catches E21 -> E22 (new position/duration,
        # needs a fresh timer, and forces Discord to re-render the activity).
        # When already paused, start is always None - no point recalculating.
        needs_timer      = title_changed or subtitle_changed or paused_changed or (not paused and last_netflix_start is None)

        # Mirror Emby's behaviour: clear Discord on a pause toggle (activity type
        # changes) and on an episode/title change. Without the clear, Discord keeps
        # the previous activity's timestamps and the bar stays pinned full on the
        # old episode even though we send fresh start/end. first-run (last_* None)
        # is skipped so we don't clear before the very first push.
        episode_changed = (subtitle_changed and last_netflix_subtitle is not None) or \
                          (title_changed and last_netflix_title is not None)
        if paused_changed or episode_changed:
            try:
                RPC.clear()
                time.sleep(0.5)
            except Exception:
                pass
            # Force the guarded push below to fire even if the cached sig matches.
            last_pushed_payload = None

        if needs_timer:
            if not paused and duration > 0:
                start = int(time.time() - position)
                end   = int(time.time() + max(10, duration - position))
            else:
                start = None
                end   = None
            last_netflix_start    = start
            last_netflix_end      = end
            last_netflix_title    = title
            last_netflix_subtitle = subtitle
            last_netflix_paused   = paused
        else:
            start = last_netflix_start
            end   = last_netflix_end
            if paused:
                start = None
                end   = None

        large_text = build_large_text(meta.get("rating"), meta.get("official_rating"), duration or meta.get("runtime"))
        if large_text == "Emby":
            large_text = "Netflix"

        # Watching (type 3) doesn't render large_text as a visible line the way
        # Listening (type 2) did, so fold the rating onto the state line. Runtime
        # is dropped - the progress bar already shows it.
        if meta.get("rating"):
            state = f"{state} • ⭐ {meta['rating']:.1f}" if state else f"⭐ {meta['rating']:.1f}"
        elif meta.get("official_rating") and state:
            state = f"{state} • {meta['official_rating']}"

        poster_img = meta.get("poster") or NETFLIX_LOGO_URL
        small_img  = (STATUS_ICON_PAUSE if paused else STATUS_ICON_PLAY) or None
        small_txt  = "Paused" if paused else "Playing"

        # Rate-limit guard: Discord silently drops updates over its ~5/20s cap.
        # This loop ticks every ~2s, so pushing unconditionally floods the cap and
        # the dropped update is often the episode change - leaving the old episode
        # and a full bar stuck. Only push when the payload actually changes (new
        # episode, pause, timer recalc) plus a periodic heartbeat.
        now = time.time()
        payload_sig = (title, display_title, state, poster_img, large_text,
                       small_img, small_txt, start, end, paused)
        if payload_sig != last_pushed_payload or now - last_push_time > RPC_HEARTBEAT:
            log(f"[Netflix] calling RPC.update - needs_timer={needs_timer} start={start} end={end}")
            RPC.update(
                name=title or "Netflix",
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
                buttons=build_buttons()
            )
            last_pushed_payload = payload_sig
            last_push_time      = now
            log(f"[Netflix] RPC.update OK")

        set_icon(paused)
        set_tooltip(f"{'Paused' if paused else 'Watching'} Netflix - {title}")
        last_rpc_success = time.time()

    except Exception as e:
        log(f"[Netflix] RPC update FAILED: {e}")
        check_rpc_health()


# -----------------------------
# Plex / Plezy source
#
# Plex only exposes live sessions to the server OWNER, so a shared/guest account
# cannot use /status/sessions. Instead we read playback straight from Plezy's own
# mpv player over its IPC pipe - enable it in Plezy's mpv config with:
#     input-ipc-server=\\.\pipe\plezympv
# mpv gives live position/duration/pause plus the stream path, which carries the
# Plex server base, token and Part id. Part id -> full metadata comes from Plezy's
# own api_cache DB. Fully local, non-owner, and unambiguously *this* player (no
# confusion with other people watching on the same account).
# -----------------------------

PLEX_MPV_PIPE = os.getenv("PLEX_MPV_PIPE", r"\\.\pipe\plezympv")
_PLEZY_DATA_GLOB = os.path.join(
    os.environ.get("LOCALAPPDATA", ""),
    "Packages", "edde746.Plezy_*", "LocalCache", "Roaming", "com.edde746", "Plezy",
)

plex_state          = {}       # runtime: server_uri, server_token (parsed from the mpv path)
plex_meta_cache     = {}       # {thumb_path: (public_url, ts)}
plex_part_cache     = {}       # {part_id: metadata_dict}
plex_poster_cache   = {}       # {rating_key: (square_url, ts)}
last_plex_ratingkey = None     # for episode/movie-change clear
last_plex_paused    = None     # for pause-change clear
_plex_ready         = True     # no network setup; get_plex_activity self-gates on the pipe
_plex_last_poll     = 0.0
_plex_cached_activity = None
_PLEX_MPV_PROPS = ("pause", "time-pos", "duration", "path", "idle-active")


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

    h = k.CreateFileW(PLEX_MPV_PIPE, GENERIC_READ | GENERIC_WRITE, 0, None, OPEN_EXISTING, 0, None)
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


def _plex_parse_path(path):
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


def _plezy_db_path():
    for d in glob.glob(_PLEZY_DATA_GLOB):
        p = os.path.join(d, "plezy_downloads.db")
        if os.path.exists(p):
            return p
    return None


def _plex_metadata_for_part(part_id):
    """Resolve a Part id to its Plex metadata via Plezy's api_cache (cached per part)."""
    if not part_id:
        return None
    if part_id in plex_part_cache:
        return plex_part_cache[part_id]
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
        log(f"[Plex] api_cache lookup failed: {e}")
    if meta:
        if len(plex_part_cache) >= CACHE_MAX_SIZE:
            plex_part_cache.clear()
        plex_part_cache[part_id] = meta
    return meta


def plex_init():
    """No network setup for the mpv-IPC source; just report readiness."""
    if not PLEX_ENABLED:
        return False
    log(f"[Plex] mpv-IPC source enabled. If nothing shows, add this to Plezy's "
        f"mpv config: input-ipc-server={PLEX_MPV_PIPE}")
    return True


def _plex_poster_url(thumb):
    """Upload a Plex art path to imgbb (Plex art needs a token, so it can't go to
    Discord directly). Uses the server base+token parsed from the mpv path."""
    if not thumb:
        return None
    now = time.time()
    if thumb in plex_meta_cache:
        url, ts = plex_meta_cache[thumb]
        if now - ts < POSTER_CACHE_TTL:
            return url
    uri = plex_state.get("server_uri")
    token = plex_state.get("server_token")
    if not uri or not token:
        return None
    src = f"{uri}{thumb}?X-Plex-Token={token}"
    url = upload_image(src, square=True) if UPLOAD_ENABLED else None
    if len(plex_meta_cache) >= CACHE_MAX_SIZE:
        _evict_oldest(plex_meta_cache)
    plex_meta_cache[thumb] = (url, now)
    return url


def _plex_pick_poster(activity):
    """Always yield a square, Discord-safe image (or the fallback logo).

    Discord crops large_image to a square, so a raw portrait poster looks zoomed.
    Prefer an already-squared imgbb URL; otherwise square the raw TMDB poster or
    the Plex art ourselves. Cached per item; falls back to the logo when imgbb is
    unreachable rather than sending a portrait that would be cropped."""
    rk = activity.get("rating_key")
    now = time.time()
    if rk and rk in plex_poster_cache:
        url, ts = plex_poster_cache[rk]
        if now - ts < POSTER_CACHE_TTL and url:
            return url

    tp = activity.get("tmdb_poster")
    url = None
    if tp and "image.tmdb.org" not in tp:
        url = tp                                   # already a squared imgbb URL
    elif tp:
        url = upload_image(tp, square=True)        # raw TMDB portrait → square it
    if not url:
        url = _plex_poster_url(activity.get("thumb"))  # fall back to Plex art (squared)

    if rk and url:
        if len(plex_poster_cache) >= CACHE_MAX_SIZE:
            _evict_oldest(plex_poster_cache)
        plex_poster_cache[rk] = (url, now)
    return url or "emby"


def get_plex_activity():
    """Read *this* Plezy player's live playback from mpv IPC + Plezy's metadata
    cache. Returns an activity dict or None."""
    global _plex_last_poll, _plex_cached_activity
    if not (PLEX_ENABLED and _plex_ready):
        return None
    now = time.time()
    if now - _plex_last_poll < PLEX_POLL_INTERVAL:
        return _plex_cached_activity
    _plex_last_poll = now

    props = _mpv_ipc_get(_PLEX_MPV_PROPS)
    if not props:
        _plex_cached_activity = None
        return None
    path = props.get("path")
    if props.get("idle-active") or not path or not str(path).startswith("http"):
        _plex_cached_activity = None
        return None

    base, token, part = _plex_parse_path(path)
    if base and token:
        plex_state["server_uri"] = base
        plex_state["server_token"] = token

    md = _plex_metadata_for_part(part) or {}
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
    meta = get_tmdb_media_info(name, media_type=media_type, season=season_num) if name and name != "Plex" else {}
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
        "rating_key": md.get("ratingKey") or part,
    }
    _plex_cached_activity = activity
    return activity


def update_plex_rpc(activity):
    global RPC, last_rpc_success, last_plex_ratingkey, last_plex_paused
    global last_pushed_payload, last_push_time

    if not RPC:
        connect_rpc()
        if not RPC:
            return

    try:
        name     = activity["name"]
        subtitle = activity["subtitle"]
        paused   = activity["paused"]
        position = activity["position"]
        duration = activity["duration"]
        rating   = activity["rating"]

        log(f"[Plex] name={name!r} sub={subtitle!r} paused={paused} pos={position:.0f} dur={duration:.0f}")

        details = name
        state   = subtitle or ("Paused" if paused else "Watching")
        if rating:
            state = f"{state} • ⭐ {rating:.1f}" if state else f"⭐ {rating:.1f}"

        # Item change or pause toggle → clear so Discord drops the old timestamps
        # / re-renders the paused state cleanly (same as the Emby/Netflix paths).
        rk = activity.get("rating_key")
        item_changed  = rk != last_plex_ratingkey and last_plex_ratingkey is not None
        pause_changed = paused != last_plex_paused and last_plex_paused is not None
        if item_changed or pause_changed:
            try:
                RPC.clear()
                time.sleep(0.5)
            except Exception:
                pass
            last_pushed_payload = None
        last_plex_ratingkey = rk
        last_plex_paused    = paused

        if not paused and duration > 0:
            start = int(time.time() - position)
            end   = int(time.time() + max(10, duration - position))
        else:
            start = None
            end   = None

        # Always a square, Discord-safe image (never a cropped portrait).
        poster    = _plex_pick_poster(activity)
        small_img = (STATUS_ICON_PAUSE if paused else STATUS_ICON_PLAY) or None
        small_txt = "Paused" if paused else "Playing"

        large_text = build_large_text(rating, None, duration or activity.get("runtime"))
        if large_text == "Emby":
            large_text = "Plex"

        now = time.time()
        payload_sig = (name, details, state, poster, small_img, small_txt, start, end, paused)
        if payload_sig != last_pushed_payload or now - last_push_time > RPC_HEARTBEAT:
            RPC.update(
                name=name or "Plex",
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
                buttons=build_buttons()
            )
            last_pushed_payload = payload_sig
            last_push_time      = now
            log("[Plex] RPC.update OK")

        set_icon(paused)
        set_tooltip(f"{'Paused' if paused else 'Watching'} Plex - {name}")
        last_rpc_success = time.time()

    except Exception as e:
        log(f"[Plex] RPC update FAILED: {e}")
        check_rpc_health()


# -----------------------------
# WebSocket - real-time Emby session updates
# -----------------------------

def _make_ws_url():
    base = (SERVER or "").rstrip("/")
    base = base.replace("https://", "wss://").replace("http://", "ws://")
    return f"{base}/embywebsocket?api_key={TOKEN}&deviceId=mediarpc-discord-presence"


def _on_ws_open(ws):
    global ws_connected, ws_last_message_time
    ws_connected = True
    ws_last_message_time = time.time()
    log("WebSocket connected - subscribing to session updates")
    # Ask Emby to push session state every 1 s
    ws.send(json.dumps({"MessageType": "SessionsStart", "Data": "0,1000"}))


def _on_ws_message(ws, message):
    global current_sessions, ws_last_message_time
    try:
        data = json.loads(message)
        if data.get("MessageType") == "Sessions":
            with state_lock:
                current_sessions = data.get("Data", [])
            ws_last_message_time = time.time()
    except Exception as e:
        log(f"WebSocket message error: {e}")


def _on_ws_close(ws, close_status_code, close_msg):
    global ws_connected
    ws_connected = False
    log(f"WebSocket closed (code={close_status_code})")


def _on_ws_error(ws, error):
    global ws_connected
    ws_connected = False
    log(f"WebSocket error: {error}")


def ws_loop():
    """Maintain a persistent WebSocket connection to Emby, reconnecting on failure.

    While Netflix is the active source we don't touch Emby at all - no point
    hammering an Emby server we aren't watching (and it stops the 502 reconnect
    spam when Emby is unreachable). The guard thread closes any live connection
    the moment Netflix takes over.
    """
    global ws_app
    while running:
        if get_netflix_activity():
            time.sleep(3)
            continue
        try:
            url = _make_ws_url()
            log(f"WebSocket connecting to Emby...")
            app = websocket.WebSocketApp(
                url,
                on_open=_on_ws_open,
                on_message=_on_ws_message,
                on_close=_on_ws_close,
                on_error=_on_ws_error,
            )
            ws_app = app
            app.run_forever(ping_interval=30, ping_timeout=10)
        except Exception as e:
            log(f"WebSocket thread exception: {e}")
        finally:
            ws_app = None
        if running and not get_netflix_activity():
            log("WebSocket reconnecting in 10s...")
            time.sleep(10)


def ws_netflix_guard():
    """Close the Emby WebSocket as soon as Netflix becomes the active source."""
    while running:
        try:
            if ws_app is not None and ws_connected and get_netflix_activity():
                log("Netflix active - closing Emby WebSocket")
                try:
                    ws_app.close()
                except Exception:
                    pass
        except Exception:
            pass
        time.sleep(3)


# -----------------------------
# Emby App Detection
# -----------------------------

def is_emby_app_running():
    if not PSUTIL_AVAILABLE:
        return True

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
                log(f"Detected Emby process: {', '.join(found_processes)}")
                is_emby_app_running.last_logged = True
            return True
        else:
            is_emby_app_running.last_logged = False
            return False

    except Exception as e:
        log(f"Error checking Emby process: {e}")
        return True


# -----------------------------
# Emby API (HTTP - used as fallback when WebSocket is down)
# -----------------------------

def get_sessions():
    sessions = fetch_sessions()
    return sessions if sessions is not None else []


def fetch_sessions():
    try:
        r = requests.get(
            f"{SERVER}/Sessions",
            headers=headers,
            timeout=10
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
    global current_sessions, last_http_reconcile

    now = time.time()
    ws_is_fresh = ws_connected and (now - ws_last_message_time) <= max(10, INTERVAL * 2)

    if ws_is_fresh:
        with state_lock:
            sessions = list(current_sessions)

        if now - last_http_reconcile < HTTP_RECONCILE_INTERVAL:
            return sessions

        fresh_sessions = fetch_sessions()
        last_http_reconcile = now
        if fresh_sessions is not None:
            with state_lock:
                current_sessions = fresh_sessions
            return fresh_sessions
        return sessions

    fresh_sessions = fetch_sessions()
    last_http_reconcile = now
    if fresh_sessions is not None:
        with state_lock:
            current_sessions = fresh_sessions
        return fresh_sessions

    with state_lock:
        return list(current_sessions) if ws_connected else []


# -----------------------------
# Series info lookup (genres + rating, cached)
# -----------------------------

OMDB_CACHE_TTL = 86400  # 24 hours


def _provider_id(providers, *keys):
    """Try multiple key spellings and return the first non-empty value."""
    for k in keys:
        v = providers.get(k)
        if v:
            return v
    return None


def get_omdb_rating(imdb_id=None, title=None, year=None, media_type="series"):
    if not OMDB_KEY:
        return None

    cache_key = imdb_id or f"{title}:{year}"
    if not cache_key:
        return None

    if cache_key in omdb_cache:
        cached_rating, cached_time = omdb_cache[cache_key]
        if time.time() - cached_time < OMDB_CACHE_TTL:
            return cached_rating

    rating = None
    try:
        if imdb_id:
            params = {"apikey": OMDB_KEY, "i": imdb_id}
            r = requests.get("https://www.omdbapi.com/", params=params, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if data.get("Response") == "True":
                    raw = data.get("imdbRating", "N/A")
                    rating = float(raw) if raw not in ("N/A", "", None) else None
                    log(f"OMDB by ID {imdb_id} → {rating}")

        if rating is None and title:
            params = {"apikey": OMDB_KEY, "t": title, "type": media_type}
            if year:
                params["y"] = year
            r = requests.get("https://www.omdbapi.com/", params=params, timeout=10)
            if r.status_code == 200:
                data = r.json()
                if data.get("Response") == "True":
                    raw = data.get("imdbRating", "N/A")
                    rating = float(raw) if raw not in ("N/A", "", None) else None
                    log(f"OMDB by title '{title}' ({year}) → {rating}")
                else:
                    log(f"OMDB title search failed: {data.get('Error')} for '{title}'")

    except Exception as e:
        log(f"OMDB fetch error: {e}")

    if len(omdb_cache) >= CACHE_MAX_SIZE:
        _evict_oldest(omdb_cache)
    omdb_cache[cache_key] = (rating, time.time())
    return rating


def get_tmdb_rating(tmdb_id=None, title=None, year=None, media_type="tv"):
    if not TMDB_KEY:
        return None

    cache_key = f"tmdb:{tmdb_id or f'{title}:{year}'}"
    if cache_key in tmdb_cache:
        cached_rating, cached_time = tmdb_cache[cache_key]
        if time.time() - cached_time < OMDB_CACHE_TTL:
            return cached_rating

    rating = None
    params = {"api_key": TMDB_KEY, "language": "en-US"}
    try:
        if tmdb_id:
            url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}"
            r = requests.get(url, params=params, timeout=10)
            if r.status_code == 200:
                avg = r.json().get("vote_average")
                rating = float(avg) if avg else None
                log(f"TMDB by ID {tmdb_id} ({media_type}) → {rating}")

        if rating is None and title:
            search_url = f"https://api.themoviedb.org/3/search/{media_type}"
            year_param = "first_air_date_year" if media_type == "tv" else "year"
            search_params = {**params, "query": title}
            if year:
                search_params[year_param] = year
            r = requests.get(search_url, params=search_params, timeout=10)
            if r.status_code == 200:
                results = r.json().get("results", [])
                if results:
                    avg = results[0].get("vote_average")
                    rating = float(avg) if avg else None
                    log(f"TMDB search '{title}' ({year}) → {rating}")
                else:
                    log(f"TMDB search: no results for '{title}'")
    except Exception as e:
        log(f"TMDB fetch error: {e}")

    if len(tmdb_cache) >= CACHE_MAX_SIZE:
        _evict_oldest(tmdb_cache)
    tmdb_cache[cache_key] = (rating, time.time())
    return rating


def get_tmdb_media_info(title, media_type="tv", season=None):
    if not TMDB_KEY or not title or is_generic_netflix_title(title):
        return {}

    cache_key = f"netflix:{media_type}:{title.lower()}:s{season}"
    if cache_key in netflix_meta_cache:
        cached_data, cached_time = netflix_meta_cache[cache_key]
        if time.time() - cached_time < POSTER_CACHE_TTL:
            return cached_data

    info = {}
    params = {"api_key": TMDB_KEY, "language": "en-US"}
    try:
        search_url = f"https://api.themoviedb.org/3/search/{media_type}"
        r = requests.get(search_url, params={**params, "query": title}, timeout=10)
        if r.status_code == 200:
            results = r.json().get("results", [])
            if results:
                result = next((candidate for candidate in results if tmdb_title_matches(title, candidate, media_type)), None)
                if not result:
                    log(f"Netflix TMDB {media_type} search rejected loose matches for '{title}'")
                    if len(netflix_meta_cache) >= CACHE_MAX_SIZE:
                        _evict_oldest(netflix_meta_cache)
                    netflix_meta_cache[cache_key] = ({}, time.time())
                    return {}
                tmdb_id = result.get("id")
                details_url = f"https://api.themoviedb.org/3/{media_type}/{tmdb_id}"
                details = {}
                if tmdb_id:
                    d = requests.get(details_url, params=params, timeout=10)
                    if d.status_code == 200:
                        details = d.json()

                poster_path = result.get("poster_path") or details.get("poster_path")

                # Prefer the season-specific poster when we know the season.
                if media_type == "tv" and season and tmdb_id:
                    try:
                        s = requests.get(
                            f"https://api.themoviedb.org/3/tv/{tmdb_id}/season/{int(season)}",
                            params=params, timeout=10
                        )
                        if s.status_code == 200:
                            season_poster = s.json().get("poster_path")
                            if season_poster:
                                poster_path = season_poster
                    except Exception as e:
                        log(f"Netflix TMDB season poster error: {e}")

                genres = [g.get("name") for g in details.get("genres", []) if g.get("name")]
                runtime = None
                official_rating = None

                if media_type == "tv":
                    runtimes = details.get("episode_run_time") or []
                    runtime = runtimes[0] * 60 if runtimes else None
                    year = (details.get("first_air_date") or result.get("first_air_date") or "")[:4] or None
                else:
                    runtime_min = details.get("runtime")
                    runtime = runtime_min * 60 if runtime_min else None
                    year = (details.get("release_date") or result.get("release_date") or "")[:4] or None

                poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else None
                if poster_url and UPLOAD_ENABLED:
                    uploaded_poster = upload_image(poster_url, square=True)
                    if uploaded_poster:
                        poster_url = uploaded_poster

                info = {
                    "tmdb_id": tmdb_id,
                    "rating": float(result.get("vote_average")) if result.get("vote_average") else None,
                    "poster": poster_url,
                    "genres": genres,
                    "runtime": runtime,
                    "official_rating": official_rating,
                    "year": int(year) if year and year.isdigit() else None,
                }
                log(f"Netflix TMDB {media_type} search '{title}'{f' S{season}' if season else ''} → poster={'yes' if info.get('poster') else 'no'} rating={info.get('rating')}")
    except Exception as e:
        log(f"Netflix TMDB lookup error: {e}")

    if len(netflix_meta_cache) >= CACHE_MAX_SIZE:
        _evict_oldest(netflix_meta_cache)
    netflix_meta_cache[cache_key] = (info, time.time())
    return info


def normalize_lookup_title(value):
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def is_generic_netflix_title(value):
    normalized = (value or "").strip().lower()
    return normalized in ("", "netflix", "watching netflix", "now playing")


def tmdb_title_matches(query, candidate, media_type):
    query_norm = normalize_lookup_title(query)
    names = [
        candidate.get("name"),
        candidate.get("original_name"),
        candidate.get("title"),
        candidate.get("original_title"),
    ]
    long_enough = len(query_norm) > 7
    for name in names:
        name_norm = normalize_lookup_title(name)
        if name_norm and (name_norm == query_norm or (long_enough and (query_norm in name_norm or name_norm in query_norm))):
            return True
    return False


def resolve_rating(community_rating, official_rating, tmdb_id, imdb_id, title, year, media_type="tv", critic_rating=None):
    log(f"resolve_rating: title='{title}' year={year} type={media_type} | emby={community_rating} tmdb_id={tmdb_id} imdb_id={imdb_id} critic={critic_rating}")

    if community_rating is not None:
        log(f"  → Using Emby community rating: {community_rating}")
        return community_rating

    rating = get_tmdb_rating(tmdb_id=tmdb_id, title=title, year=year, media_type=media_type)
    if rating is not None:
        log(f"  → Using TMDB rating: {rating}")
        return rating

    omdb_type = "series" if media_type == "tv" else "movie"
    rating = get_omdb_rating(imdb_id=imdb_id, title=title, year=year, media_type=omdb_type)
    if rating is not None:
        log(f"  → Using OMDB rating: {rating}")
        return rating

    if critic_rating is not None and critic_rating > 0:
        scaled = round(critic_rating / 10.0, 1)
        log(f"  → Using Emby critic rating: {critic_rating} → {scaled}")
        return scaled

    log("  → No rating found from any source")
    return None


def get_series_info(series_id):
    if not series_id:
        return [], None, None, None, None, None, None

    if series_id in series_cache:
        cached_data, cached_time = series_cache[series_id]
        if time.time() - cached_time < POSTER_CACHE_TTL:
            d = cached_data
            return d["genres"], d["community_rating"], d["official_rating"], d["imdb_id"], d["tmdb_id"], d.get("critic_rating"), d.get("year")

    try:
        r = requests.get(
            f"{SERVER}/Items/{series_id}",
            headers=headers,
            params={"Fields": "Genres,CommunityRating,OfficialRating,CriticRating,ProviderIds,ProductionYear"},
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            providers = data.get("ProviderIds") or {}
            imdb_id = _provider_id(providers, "Imdb", "IMDB", "imdb")
            tmdb_id = _provider_id(providers, "Tmdb", "TheMovieDb", "Tmdb", "tmdb")
            info = {
                "genres":           data.get("Genres", []),
                "community_rating": data.get("CommunityRating"),
                "official_rating":  data.get("OfficialRating"),
                "critic_rating":    data.get("CriticRating"),
                "year":             data.get("ProductionYear"),
                "imdb_id":          imdb_id,
                "tmdb_id":          tmdb_id,
            }
            log(f"Series info: community={info['community_rating']}, official={info['official_rating']}, critic={info['critic_rating']}, year={info['year']}, imdb={info['imdb_id']}, tmdb={info['tmdb_id']}, raw_providers={list(providers.keys())}")
            if len(series_cache) >= CACHE_MAX_SIZE:
                _evict_oldest(series_cache)
            series_cache[series_id] = (info, time.time())
            return info["genres"], info["community_rating"], info["official_rating"], info["imdb_id"], info["tmdb_id"], info["critic_rating"], info["year"]
    except Exception as e:
        log(f"Series info fetch error: {e}")

    return [], None, None, None, None, None, None


def get_season_rating(season_id):
    """Fetch community/critic rating for a season (cached). Returns (community, critic)."""
    if not season_id:
        return None, None

    if season_id in season_cache:
        community, critic, cached_time = season_cache[season_id]
        if time.time() - cached_time < POSTER_CACHE_TTL:
            return community, critic

    try:
        r = requests.get(
            f"{SERVER}/Items/{season_id}",
            headers=headers,
            params={"Fields": "CommunityRating,CriticRating"},
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            community = data.get("CommunityRating")
            critic    = data.get("CriticRating")
            log(f"Season rating ({season_id}): community={community}, critic={critic}")
            if len(season_cache) >= CACHE_MAX_SIZE:
                _evict_oldest(season_cache)
            season_cache[season_id] = (community, critic, time.time())
            return community, critic
    except Exception as e:
        log(f"Season rating fetch error: {e}")

    if len(season_cache) >= CACHE_MAX_SIZE:
        _evict_oldest(season_cache)
    season_cache[season_id] = (None, None, time.time())
    return None, None


def get_item_rating(item_id):
    try:
        r = requests.get(
            f"{SERVER}/Items/{item_id}",
            headers=headers,
            params={"Fields": "CommunityRating,OfficialRating,CriticRating,ProviderIds"},
            timeout=10
        )
        if r.status_code == 200:
            data = r.json()
            providers = data.get("ProviderIds") or {}
            community = data.get("CommunityRating")
            official  = data.get("OfficialRating")
            critic    = data.get("CriticRating")
            imdb_id   = _provider_id(providers, "Imdb", "IMDB", "imdb")
            tmdb_id   = _provider_id(providers, "Tmdb", "TheMovieDb", "tmdb")
            log(f"Item rating fetch: community={community}, official={official}, critic={critic}, imdb={imdb_id}, tmdb={tmdb_id}, raw_providers={list(providers.keys())}")
            return community, official, imdb_id, tmdb_id, critic
    except Exception as e:
        log(f"Item rating fetch error: {e}")
    return None, None, None, None, None


# -----------------------------
# Runtime formatter
# -----------------------------

def format_runtime(seconds):
    if not seconds or seconds <= 0:
        return None
    total_min = int(seconds // 60)
    if total_min < 60:
        return f"{total_min} min"
    hours = total_min // 60
    mins  = total_min % 60
    return f"{hours}h {mins}min" if mins else f"{hours}h"


def build_large_text(community_rating, official_rating, runtime_seconds):
    runtime_str = format_runtime(runtime_seconds)
    if community_rating:
        rating_str = f"⭐ {community_rating:.1f}"
    elif official_rating:
        rating_str = official_rating
    else:
        rating_str = None

    if rating_str and runtime_str:
        return f"{rating_str} • {runtime_str}"
    elif rating_str:
        return rating_str
    elif runtime_str:
        return runtime_str
    return "Emby"


# -----------------------------
# Poster (ImgBB with direct-URL fallback, TTL cache)
# -----------------------------

def square_poster_bytes(raw):
    """Make portrait posters look good in Discord's square activity image."""
    with Image.open(BytesIO(raw)) as img:
        img = img.convert("RGB")

        bg = ImageOps.fit(img, (512, 512), method=Image.Resampling.LANCZOS)
        bg = bg.filter(ImageFilter.GaussianBlur(18))
        bg = Image.blend(bg, Image.new("RGB", (512, 512), (18, 18, 20)), 0.35)

        poster = ImageOps.contain(img, (360, 500), method=Image.Resampling.LANCZOS)
        x = (512 - poster.width) // 2
        y = (512 - poster.height) // 2
        bg.paste(poster, (x, y))

        out = BytesIO()
        bg.save(out, format="JPEG", quality=92, optimize=True)
        return out.getvalue()


_zipline_folder_id = None


def _zipline_get_folder_id():
    """Resolve (and cache) the MediaRPC folder id, creating it if missing."""
    global _zipline_folder_id
    if _zipline_folder_id is not None:
        return _zipline_folder_id
    if not (ZIPLINE_ENABLED and ZIPLINE_FOLDER):
        return None
    h = {"Authorization": ZIPLINE_TOKEN}
    try:
        r = requests.get(f"{ZIPLINE_URL}/api/user/folders", headers=h, timeout=15)
        if r.status_code == 200:
            for f in r.json():
                if f.get("name") == ZIPLINE_FOLDER and not f.get("parentId"):
                    _zipline_folder_id = f.get("id")
                    return _zipline_folder_id
        # Not found → create it
        c = requests.post(f"{ZIPLINE_URL}/api/user/folders", headers=h,
                          json={"name": ZIPLINE_FOLDER}, timeout=15)
        if c.status_code in (200, 201):
            _zipline_folder_id = c.json().get("id")
            log(f"Zipline: created folder '{ZIPLINE_FOLDER}'")
    except Exception as e:
        log(f"Zipline folder resolve error: {e}")
    return _zipline_folder_id


def _zipline_upload_bytes(raw):
    if not ZIPLINE_ENABLED:
        return None
    # Zipline v4 expiry: header x-zipline-deletes-at = "date=<ISO8601 UTC>".
    from datetime import datetime, timedelta, timezone
    deletes = (datetime.now(timezone.utc) + timedelta(seconds=_UPLOAD_EXPIRY)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    headers = {"Authorization": ZIPLINE_TOKEN, "x-zipline-deletes-at": f"date={deletes}"}
    fid = _zipline_get_folder_id()
    if fid:
        headers["X-Zipline-Folder"] = str(fid)
    try:
        r = requests.post(f"{ZIPLINE_URL}/api/upload", headers=headers,
                          files={"file": ("poster.jpg", raw, "image/jpeg")}, timeout=20)
        if r.status_code in (200, 201):
            files = (r.json() or {}).get("files") or []
            if files:
                f0 = files[0]
                return f0.get("url") if isinstance(f0, dict) else f0
        else:
            log(f"Zipline upload HTTP {r.status_code}")
    except Exception as e:
        log(f"Zipline upload error: {e}")
    return None


def _imgbb_upload_bytes(raw):
    if not IMGBB_KEY:
        return None
    try:
        r = requests.post(
            "https://api.imgbb.com/1/upload",
            params={"key": IMGBB_KEY, "expiration": str(_UPLOAD_EXPIRY)},
            files={"image": ("poster.jpg", raw, "image/jpeg")},
            timeout=15
        )
        if r.status_code == 200:
            return r.json()["data"]["url"]
    except Exception:
        pass
    return None


def upload_image_bytes(raw):
    """Host image bytes and return a public URL. Prefers Zipline, falls back to ImgBB."""
    return _zipline_upload_bytes(raw) or _imgbb_upload_bytes(raw)


# -----------------------------
# Persistent uploaded-image cache
#
# Uploaded posters live on the host (Zipline/ImgBB) for _UPLOAD_EXPIRY. Without a
# cache that survives restarts we'd re-upload the same poster every launch, piling
# up duplicate files that each start a fresh 7-day clock. Key on the source image
# (host+path, tokens stripped) so identical art reuses one hosted URL until it's
# near expiry, then re-uploads once.
# -----------------------------

def _parse_duration(s, default=7 * 86400):
    s = (s or "").strip().lower()
    if s in ("never", "0", ""):
        return 30 * 86400
    m = re.match(r"(\d+)\s*([hd])", s)
    if not m:
        return default
    n = int(m.group(1))
    return n * 3600 if m.group(2) == "h" else n * 86400


_UPLOAD_EXPIRY = _parse_duration(ZIPLINE_EXPIRES)             # host retention, seconds
_IMG_CACHE_TTL = max(3600, _UPLOAD_EXPIRY - 6 * 3600)        # re-upload ~6h before expiry
_img_cache = {}
_img_cache_loaded = False


def _img_cache_path():
    base = os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(__file__))
    return os.path.join(base, "image_cache.json")


def _img_cache_load():
    global _img_cache, _img_cache_loaded
    if _img_cache_loaded:
        return
    _img_cache_loaded = True
    try:
        with open(_img_cache_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        now = time.time()
        _img_cache = {k: v for k, v in data.items() if now - v[1] < _IMG_CACHE_TTL}
    except Exception:
        _img_cache = {}


def _img_cache_save():
    try:
        with open(_img_cache_path(), "w", encoding="utf-8") as f:
            json.dump(_img_cache, f)
    except Exception as e:
        log(f"Image cache save failed: {e}")


def _img_cache_key(url, square):
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
        base = f"{u.scheme}://{u.netloc}{u.path}"   # drop query (tokens/versions)
    except Exception:
        base = url
    return f"{'sq' if square else 'raw'}:{base}"


def _img_cache_get(url, square):
    _img_cache_load()
    v = _img_cache.get(_img_cache_key(url, square))
    if v and time.time() - v[1] < _IMG_CACHE_TTL:
        return v[0]
    return None


def _img_cache_set(url, square, hosted):
    _img_cache_load()
    _img_cache[_img_cache_key(url, square)] = [hosted, time.time()]
    _img_cache_save()


def upload_image(url, request_headers=None, square=True):
    if not UPLOAD_ENABLED:
        return None

    cached = _img_cache_get(url, square)
    if cached:
        return cached

    try:
        raw = requests.get(url, headers=request_headers or {}, timeout=10).content
        if square:
            raw = square_poster_bytes(raw)
        hosted = upload_image_bytes(raw)
        if hosted:
            _img_cache_set(url, square, hosted)
        return hosted
    except Exception as e:
        log(f"Poster upload error: {e}")
    return None


def get_poster(item):
    """Get poster URL.
    Tries ImgBB upload first (works for private servers, gives a public URL).
    Falls back to the direct Emby URL (works when your server is publicly reachable)."""
    media_type = item.get("Type")

    if media_type == "Episode":
        series_id = item.get("SeriesId")
        poster_id = series_id if series_id else item.get("Id")
    else:
        poster_id = item.get("Id")

    if not poster_id:
        return None

    # Check cache with TTL
    if poster_id in poster_cache:
        cached_url, cached_time = poster_cache[poster_id]
        if time.time() - cached_time < POSTER_CACHE_TTL:
            return cached_url
        else:
            log(f"Poster cache expired for {poster_id}, refreshing")

    emby_url = f"{SERVER}/Items/{poster_id}/Images/Primary?maxWidth=900"

    # Try the image host first (Zipline or ImgBB)
    if UPLOAD_ENABLED:
        uploaded = upload_image(emby_url, request_headers=headers, square=True)
        if uploaded:
            if len(poster_cache) >= CACHE_MAX_SIZE:
                _evict_oldest(poster_cache)
            poster_cache[poster_id] = (uploaded, time.time())
            return uploaded
        log(f"Image upload failed for {poster_id} - trying direct Emby URL")

    # Fallback: direct Emby URL - only works if HTTPS (Discord rejects plain HTTP,
    # which would silently kill the whole assets block including the small icons)
    if emby_url.startswith("https://"):
        if len(poster_cache) >= CACHE_MAX_SIZE:
            _evict_oldest(poster_cache)
        poster_cache[poster_id] = (emby_url, time.time())
        return emby_url

    # Local/HTTP server - return None so the caller uses the bundled "emby" asset
    if len(poster_cache) >= CACHE_MAX_SIZE:
        _evict_oldest(poster_cache)
    poster_cache[poster_id] = (None, time.time())
    return None


# -----------------------------
# Discord buttons
# -----------------------------

def build_buttons():
    return [
        {"label": "Letterboxd", "url": LETTERBOXD_URL},
        {"label": "Serializd",  "url": SERIALIZD_URL}
    ]


# -----------------------------
# Tray icon state
# -----------------------------

def set_icon(paused):
    global icon_ref
    if icon_ref is None:
        return

    icon_file = "MediaRPC_Inactive.ico" if paused else "MediaRPC_Active.ico"
    path = resource_path(icon_file)

    if os.path.exists(path):
        icon_ref.icon = Image.open(path)
    else:
        log(f"ERROR: Icon file not found: {path}")


def set_tooltip(text):
    global icon_ref
    if icon_ref is not None:
        try:
            icon_ref.title = text
        except Exception:
            pass


# -----------------------------
# Tooltip heartbeat
# -----------------------------

def tooltip_heartbeat():
    """Keep the tray tooltip current when the RPC loop hasn't updated it recently."""
    while running:
        time.sleep(10)
        if not running:
            break
        if paused_rpc:
            set_tooltip("MediaRPC - Paused by user")
            continue
        # Only intervene if the loop has been quiet for a while
        if time.time() - last_loop_time > INTERVAL * 2 + 5:
            if not ws_connected and RPC is None:
                set_tooltip("MediaRPC - No connection")
            elif not ws_connected:
                set_tooltip("MediaRPC - Server unreachable (retrying...)")
            elif RPC is None:
                set_tooltip("MediaRPC - Discord not connected")
            else:
                set_tooltip("MediaRPC - Idle")


# -----------------------------
# Update Discord RPC
# -----------------------------

def update_rpc(session):
    global RPC, last_rpc_success
    global last_item_id, last_position, last_paused, last_start, last_end, last_mode
    global last_paused_session, last_paused_session_time
    global last_http_reconcile
    global last_pushed_payload, last_push_time

    if not RPC:
        connect_rpc()
        if not RPC:
            return

    try:
        item   = session["NowPlayingItem"]
        play   = session.get("PlayState", {})
        paused = play.get("IsPaused", False)

        runtime  = item.get("RunTimeTicks", 0) / 10000000
        position = play.get("PositionTicks", 0) / 10000000
        item_id  = item.get("Id")

        # Clear RPC when switching to a different item to force Discord refresh
        if item_id != last_item_id and last_item_id is not None:
            log("Item changed, forcing Discord refresh...")
            try:
                RPC.clear()
                time.sleep(0.5)
            except Exception as e:
                log(f"Clear on item change failed: {e}")

        # Log and handle pause state changes
        if paused != last_paused:
            log(f"Pause state changed: {'PAUSED' if paused else 'PLAYING'}")
            log("Forcing Discord refresh by clearing RPC...")
            try:
                RPC.clear()
                time.sleep(1)
            except Exception as e:
                log(f"Clear on pause change failed: {e}")

        position_jump = abs(position - last_position) > (INTERVAL + 2)
        near_end = runtime > 0 and position >= runtime - 5
        needs_timer_update = (
            item_id != last_item_id or
            paused  != last_paused  or
            position_jump or
            near_end  # prevent stale last_end from pinning bar at 100%
        )

        # Force HTTP reconcile when near episode end so new session data arrives fast
        if near_end and not paused:
            last_http_reconcile = 0

        if needs_timer_update or last_start is None:
            if not paused:
                start = int(time.time() - position)
                end   = max(int(time.time()) + 5, int(time.time() + (runtime - position)))
                log(f"Timer: Playing - start={start}, end={end}")
            else:
                start = None
                end   = None
                log("Timer: PAUSED - NO TIMESTAMPS (timer hidden)")

            last_start = start
            last_end   = end
        else:
            start = last_start
            end   = last_end
            if paused:
                start = None
                end   = None

        last_item_id  = item_id
        last_position = position
        last_paused   = paused

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
            series_genres, series_community, series_official, series_imdb, series_tmdb, series_critic, series_year = get_series_info(series_id)
            genre_str = ", ".join(series_genres[:3])
            details   = f"{series_name}{year_str} • {genre_str}" if genre_str else f"{series_name}{year_str}"

            season  = item.get('ParentIndexNumber', '?')
            episode = item.get('IndexNumber', '?')
            state   = f"S{season}E{episode} {title}"

            # Rating fallback: Episode → Season → Series
            ep_community = item.get("CommunityRating")
            ep_critic    = item.get("CriticRating")
            season_community, season_critic = get_season_rating(season_id) if ep_community is None else (None, None)
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
            log(f"Episode rating chain: ep={ep_community}, season={season_community}, series={series_community} → using {effective_community}")

            # Use the series premiere year (not episode year) for TMDB/OMDB title searches
            rating     = resolve_rating(effective_community, series_official, series_tmdb, series_imdb, series_name, series_year, media_type="tv", critic_rating=effective_critic)
            large_text = build_large_text(rating, series_official, runtime)
            tooltip_text = f"{'⏸' if paused else '▶'} {series_name} - S{season}E{episode}"
            rpc_name = series_name

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
            movie_imdb      = _provider_id(providers, "Imdb", "IMDB", "imdb")
            movie_tmdb      = _provider_id(providers, "Tmdb", "TheMovieDb", "tmdb")
            if movie_community is None:
                fetched_community, fetched_official, fetched_imdb, fetched_tmdb, fetched_critic = get_item_rating(item_id)
                movie_community = fetched_community
                movie_official  = movie_official or fetched_official
                movie_critic    = movie_critic if movie_critic is not None else fetched_critic
                movie_imdb      = movie_imdb or fetched_imdb
                movie_tmdb      = movie_tmdb or fetched_tmdb
            rating     = resolve_rating(movie_community, movie_official, movie_tmdb, movie_imdb, title, year, media_type="movie", critic_rating=movie_critic)
            large_text = build_large_text(rating, movie_official, runtime)
            tooltip_text = f"{'⏸' if paused else '▶'} {title}{year_str}"
            rpc_name = title

        poster    = get_poster(item)
        small_img = (STATUS_ICON_PAUSE if paused else STATUS_ICON_PLAY) or None
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
        now = time.time()
        payload_sig = (rpc_name, details, state, poster if poster else "emby",
                       large_text, small_img, small_txt, start, end, paused)
        if payload_sig != last_pushed_payload or now - last_push_time > RPC_HEARTBEAT:
            RPC.update(
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
                buttons=build_buttons()
            )
            last_pushed_payload = payload_sig
            last_push_time      = now

        set_icon(paused)
        set_tooltip(tooltip_text)
        last_rpc_success = time.time()

        # Save session when paused so we can hold it if NowPlayingItem disappears.
        # Only reset the timestamp on the pause transition - holding it steady
        # ensures PAUSED_SESSION_HOLD actually expires after the configured time.
        if paused:
            if last_paused_session is None:
                last_paused_session_time = time.time()
            last_paused_session = session
        else:
            last_paused_session      = None
            last_paused_session_time = 0

    except Exception as e:
        log(f"RPC update error: {e}")
        check_rpc_health()


def update_rpc_browsing(session):
    """Update RPC when browsing (not playing)"""
    global RPC, last_rpc_success, last_mode

    if not RPC:
        connect_rpc()
        if not RPC:
            return

    try:
        client = session.get("Client", "Unknown")

        RPC.update(
            name="Emby",
            activity_type=3,  # "Watching Emby"
            details="Browsing Emby Library",
            state=f"Using {client}",
            large_image=EMBY_LOGO_URL if EMBY_LOGO_URL else "emby",
            large_text="Browsing Emby",
            buttons=build_buttons()
        )

        set_icon(False)
        set_tooltip("Browsing Emby Library")
        last_rpc_success = time.time()

    except Exception as e:
        log(f"RPC browsing update error: {e}")
        check_rpc_health()


# -----------------------------
# RPC loop
# -----------------------------

def rpc_loop():
    global running, last_item_id, last_start, last_end, last_mode
    global last_paused_session, last_paused_session_time, last_loop_time

    emby_was_running = True

    while running:
        last_loop_time = time.time()

        if paused_rpc:
            time.sleep(1)
            continue

        try:
            sessions = get_reconciled_sessions()

            def _leave_clear():
                # Clear Discord when switching away from a non-empty source.
                if RPC and last_mode is not None:
                    try:
                        RPC.clear()
                        time.sleep(0.5)
                    except Exception as e:
                        log(f"Clear on mode switch failed: {e}")

            # ---- Priority: Emby (playing) > Plex > Netflix > Emby (browsing) > idle ----

            # Emby auto-pause: when the local Emby app isn't running we skip Emby
            # entirely (both playing and browsing) so Plex/Netflix can take over,
            # instead of clearing and blocking them like before.
            emby_allowed = True
            if AUTO_PAUSE_ENABLED and PSUTIL_AVAILABLE:
                emby_running = is_emby_app_running()
                if emby_running and not emby_was_running:
                    log("Emby app detected")
                elif not emby_running and emby_was_running:
                    log("Emby app closed")
                emby_was_running = emby_running
                emby_allowed = emby_running

            # 1. EMBY playing (my session, or held paused session)
            session = None
            if emby_allowed:
                session = next(
                    (s for s in sessions if s.get("UserId") == USER_ID and "NowPlayingItem" in s),
                    None
                )
                if not session and last_paused_session is not None:
                    held_item = last_paused_session.get("NowPlayingItem", {})
                    held_play = last_paused_session.get("PlayState", {})
                    held_runtime = held_item.get("RunTimeTicks", 0) / 10000000
                    held_position = held_play.get("PositionTicks", 0) / 10000000
                    held_near_end = held_runtime > 0 and held_position >= max(0, held_runtime - 5)
                    if not held_near_end and time.time() - last_paused_session_time < PAUSED_SESSION_HOLD:
                        log("No live session - holding last paused state")
                        session = last_paused_session
                    else:
                        last_paused_session      = None
                        last_paused_session_time = 0

            if session:
                if last_mode != "playing":
                    _leave_clear()
                last_mode = "playing"
                update_rpc(session)
                time.sleep(2 if ws_connected else INTERVAL)
                continue

            # 2. PLEX
            plex = get_plex_activity()
            if plex:
                if last_mode != "plex":
                    log("Switching to Plex activity")
                    _leave_clear()
                last_mode = "plex"
                update_plex_rpc(plex)
                time.sleep(2)
                continue
            if last_mode == "plex":
                log("Plex activity ended - clearing presence")
                _leave_clear()
                last_mode = None

            # 3. NETFLIX
            netflix = get_netflix_activity()
            if netflix:
                if last_mode != "netflix":
                    log("Switching to Netflix browser activity")
                    _leave_clear()
                last_mode = "netflix"
                update_netflix_rpc(netflix)
                time.sleep(2)
                continue
            if last_mode == "netflix":
                log("Netflix activity ended - clearing presence")
                _leave_clear()
                last_mode = None

            # 4. EMBY browsing (low priority - never overrides a real Plex/Netflix watch)
            browsing_session = None
            if emby_allowed:
                browsing_session = next(
                    (s for s in sessions if s.get("UserId") == USER_ID and "NowPlayingItem" not in s),
                    None
                )

            if browsing_session:
                if last_mode != "browsing":
                    _leave_clear()
                last_mode = "browsing"
                update_rpc_browsing(browsing_session)
            else:
                # 5. Idle - nothing playing anywhere
                if RPC:
                    try:
                        RPC.clear()
                    except Exception as e:
                        log(f"RPC clear failed: {e}")
                        check_rpc_health()
                set_icon(True)
                set_tooltip("MediaRPC - App not running" if not emby_allowed else "MediaRPC")

                last_item_id             = None
                last_start               = None
                last_end                 = None
                last_mode                = None
                last_paused              = False
                last_paused_session      = None
                last_paused_session_time = 0

        except Exception as e:
            log(f"Loop error: {e}")
            check_rpc_health()

        # When WebSocket feeds us real-time data we only need the loop to
        # rate-limit Discord updates - a 2 s tick is plenty.
        # Without WebSocket, respect the full polling interval.
        time.sleep(2 if ws_connected else INTERVAL)


# -----------------------------
# Tray menu actions
# -----------------------------

def toggle_rpc(icon, item):
    global paused_rpc
    paused_rpc = not paused_rpc

    if paused_rpc:
        log("RPC manually PAUSED by user")
        if RPC:
            try:
                RPC.clear()
            except Exception:
                pass
        set_icon(True)
        set_tooltip("MediaRPC - Paused")
    else:
        log("RPC manually RESUMED by user")
        set_icon(False)
        set_tooltip("MediaRPC")


def open_emby(icon, item):
    """Launch Emby app using glob for version-agnostic detection, fallback to browser"""
    emby_launched = False

    glob_patterns = [
        r"C:\Program Files\WindowsApps\EmbyMedia.EmbyTheater_*_x64__*\Emby.Client.WinUI.exe",
        r"C:\Program Files\WindowsApps\EmbyMedia.EmbyTheater_*_x64_*\Emby.Client.WinUI.exe",
    ]
    static_paths = [
        r"C:\Program Files\Emby Theater\Emby.Client.WinUI.exe",
        r"C:\Program Files (x86)\Emby Theater\Emby.Client.WinUI.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\Emby Theater\Emby.Client.WinUI.exe"),
    ]

    candidates = []
    for pattern in glob_patterns:
        candidates.extend(glob.glob(pattern))
    candidates.extend(static_paths)

    for path in candidates:
        if os.path.exists(path):
            try:
                os.startfile(path)
                log(f"Opened Emby: {path}")
                emby_launched = True
                break
            except Exception as e:
                log(f"Failed to open {path}: {e}")

    if not emby_launched:
        log("Emby app not found, opening web browser")
        webbrowser.open(SERVER)


def open_letterboxd(icon, item):
    webbrowser.open(LETTERBOXD_URL)


def open_serializd(icon, item):
    webbrowser.open(SERIALIZD_URL)


def quit_app(icon, item):
    global running
    running = False

    if RPC:
        try:
            RPC.clear()
        except Exception:
            pass

    icon.stop()


# -----------------------------
# Tray
# -----------------------------

def create_menu():
    return pystray.Menu(
        pystray.MenuItem(
            lambda item: "▶ Resume RPC" if paused_rpc else "⏸ Pause RPC",
            toggle_rpc
        ),
        pystray.MenuItem("🔄 Refresh RPC", refresh_rpc),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("🖥 Open Emby",        open_emby),
        pystray.MenuItem("📽 Open Letterboxd",  open_letterboxd),
        pystray.MenuItem("📺 Open Serializd",   open_serializd),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Auto-start with Windows", toggle_autostart, checked=lambda item: is_autostart_enabled()),
        pystray.MenuItem("🖥 Toggle Console",   toggle_console),
        pystray.MenuItem("❌ Quit",             quit_app)
    )


def tray():
    global icon_ref

    image = Image.open(resource_path("MediaRPC_Inactive.ico"))
    menu  = create_menu()

    icon_ref = pystray.Icon(
        "MediaRPC",
        image,
        "MediaRPC",
        menu
    )

    icon_ref.run()


# -----------------------------
# Main
# -----------------------------

def main():
    log("Starting MediaRPC...")

    if getattr(sys, 'frozen', False):
        exe_dir     = os.path.dirname(sys.executable)
        dotenv_path = os.path.join(exe_dir, '.env')
        if os.path.exists(dotenv_path):
            log(f".env loaded from: {dotenv_path}")
        else:
            log(f"WARNING: .env not found at: {dotenv_path}")
            log("Place .env file next to the exe!")

    # Validate required config - exits with a clear message if anything is missing
    validate_config()

    log(f"Server: {SERVER}")
    log(f"Update interval: {INTERVAL}s (HTTP fallback) / 2s (WebSocket)")
    log(f"Poster cache TTL: {POSTER_CACHE_TTL // 3600}h")

    if is_autostart_enabled():
        log("Auto-start: ENABLED")
    else:
        log("Auto-start: DISABLED")

    if AUTO_PAUSE_ENABLED:
        if PSUTIL_AVAILABLE:
            log("Auto-pause when Emby closes: ENABLED")
            if is_emby_app_running():
                log("✓ Emby app is running")
            else:
                log("⚠ Emby app NOT detected - RPC will pause until app opens")
        else:
            log("Auto-pause when Emby closes: DISABLED (psutil not installed)")
    else:
        log("Auto-pause when Emby closes: DISABLED (via .env)")

    # --- API key diagnostics ---
    log(f"TMDB key: {'✓ loaded' if TMDB_KEY else '✗ MISSING (check .env)'}")
    log(f"OMDB key: {'✓ loaded' if OMDB_KEY else '✗ MISSING (check .env)'}")
    if TMDB_KEY:
        try:
            r = requests.get(
                "https://api.themoviedb.org/3/search/tv",
                params={"api_key": TMDB_KEY, "query": "Better Call Saul", "language": "en-US"},
                timeout=10
            )
            if r.status_code == 200:
                results = r.json().get("results", [])
                if results:
                    avg = results[0].get("vote_average")
                    log(f"TMDB test OK - Better Call Saul vote_average={avg}")
                else:
                    log("TMDB test: no results returned (unexpected)")
            elif r.status_code == 401:
                log("TMDB test FAILED - 401 Unauthorized (invalid key)")
            else:
                log(f"TMDB test FAILED - HTTP {r.status_code}")
        except Exception as e:
            log(f"TMDB test ERROR - {e}")
    if OMDB_KEY:
        try:
            r = requests.get(
                "https://www.omdbapi.com/",
                params={"apikey": OMDB_KEY, "t": "Better Call Saul", "type": "series"},
                timeout=10
            )
            if r.status_code == 200:
                data = r.json()
                if data.get("Response") == "True":
                    log(f"OMDB test OK - Better Call Saul imdbRating={data.get('imdbRating')}")
                else:
                    log(f"OMDB test FAILED - {data.get('Error')}")
            else:
                log(f"OMDB test FAILED - HTTP {r.status_code}")
        except Exception as e:
            log(f"OMDB test ERROR - {e}")

    # Upload status icons once; cache URLs permanently
    load_or_upload_status_icons()

    connect_rpc()

    if NETFLIX_BROWSER_RPC:
        threading.Thread(target=netflix_bridge_loop, daemon=True, name="netflix-bridge").start()
        log("Netflix browser RPC: ENABLED")
    else:
        log("Netflix browser RPC: DISABLED (via .env)")

    # Plex/Plezy source - init on a thread since first-run PIN sign-in blocks.
    if PLEX_ENABLED:
        threading.Thread(target=plex_init, daemon=True, name="plex-init").start()
        log("Plex source: ENABLED")
    else:
        log("Plex source: DISABLED (via .env)")

    # WebSocket thread - real-time session updates from Emby
    if WEBSOCKET_AVAILABLE:
        threading.Thread(target=ws_loop, daemon=True, name="ws-loop").start()
        if NETFLIX_BROWSER_RPC:
            threading.Thread(target=ws_netflix_guard, daemon=True, name="ws-netflix-guard").start()
        log("WebSocket thread started")
    else:
        log("websocket-client not installed - falling back to HTTP polling only")

    # Main RPC update loop
    threading.Thread(target=rpc_loop, daemon=True, name="rpc-loop").start()

    # Tooltip heartbeat - keeps tray tooltip accurate when the loop is quiet
    threading.Thread(target=tooltip_heartbeat, daemon=True, name="tooltip-heartbeat").start()

    tray()


if __name__ == "__main__":
    main()
