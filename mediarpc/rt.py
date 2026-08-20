"""Runtime foundation: config, shared mutable state, logging.

Every other module imports this as `from . import rt` and reads/writes shared
state through attribute access (``rt.RPC``, ``rt.last_mode`` ...). Config
constants are read-only and may also be imported by name for readability, but
anything that is ever reassigned MUST be touched as ``rt.NAME`` so all modules
see the same value.
"""

import os
import re
import sys
import time
import threading
import logging
from collections import OrderedDict
from logging.handlers import RotatingFileHandler

import requests
from requests.adapters import HTTPAdapter
from dotenv import load_dotenv

try:
    import websocket  # noqa: F401
    WEBSOCKET_AVAILABLE = True
except ImportError:
    WEBSOCKET_AVAILABLE = False
    print("Warning: websocket-client not installed. Real-time updates unavailable.")
    print("Install with: pip install websocket-client")

try:
    import psutil  # noqa: F401
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False
    print("Warning: psutil not installed. Auto-pause when Emby closes will not work.")
    print("Install with: pip install psutil")


# -----------------------------
# PyInstaller resource loader
# -----------------------------

def resource_path(relative):
    """Resolve a bundled asset (e.g. "Images/MediaRPC_Active.ico").

    Frozen: PyInstaller unpacks datas under sys._MEIPASS. Source: assets live
    inside the package dir (mediarpc/), so resolve relative to this file, not the
    current working directory."""
    try:
        base = sys._MEIPASS
    except AttributeError:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, relative)


# -----------------------------
# Load .env
# -----------------------------

if getattr(sys, 'frozen', False):
    # Frozen: .env sits next to the exe (build.bat copies it into dist\).
    dotenv_path = os.path.join(os.path.dirname(sys.executable), '.env')
else:
    # Source: .env lives inside the package dir alongside the code.
    dotenv_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env')
if os.path.exists(dotenv_path):
    load_dotenv(dotenv_path)
else:
    load_dotenv()  # fall back to the default cwd-based search

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

# Verbose per-tick logging (rating chains, timer recalcs, per-update dumps).
# Off by default so the rotating log isn't hammered every 2 s loop tick.
DEBUG = os.getenv("DEBUG", "false").lower() == "true"

# Preferred order of rating sources - first that yields a value wins. Tokens:
#   emby   = Emby community rating       tmdb = TheMovieDB
#   omdb   = IMDb rating (via OMDB)       critic = Emby critic rating (scaled /10)
# Reorder to taste, drop a token to skip that source. Unknown tokens are ignored.
RATING_ORDER = [s.strip().lower() for s in
                os.getenv("RATING_ORDER", "emby,tmdb,omdb,critic").split(",")
                if s.strip()]

# --- Per-source presence toggles (false = that source never shows on Discord) ---
# Native Emby source (Sessions API + WebSocket). Plezy and the browser bridge
# have their own switches (PLEZY_ENABLED, BRIDGE_ENABLED) further below.
EMBY_ENABLED = os.getenv("EMBY_ENABLED", "true").lower() == "true"
# Per browser-service switches (all require BRIDGE_ENABLED). Turn off one streaming
# service without disabling the whole browser bridge.
SERVICE_ENABLED = {
    "netflix": os.getenv("NETFLIX_ENABLED", "true").lower() == "true",
    "disney":  os.getenv("DISNEY_ENABLED",  "true").lower() == "true",
    "tv2":     os.getenv("TV2_ENABLED",     "true").lower() == "true",
}

AUTO_PAUSE_ENABLED = os.getenv("AUTO_PAUSE_WHEN_CLOSED", "true").lower() == "true"
# Global switch for "browsing" presence (Emby library browse + Netflix browse).
# When false, browsing never shows on Discord - only active playback does.
BROWSING_ENABLED = os.getenv("BROWSING_ENABLED", "true").lower() == "true"
# Browser bridge (Netflix + Disney+ + TV 2 Play).
BRIDGE_ENABLED = os.getenv("BRIDGE_ENABLED", "true").lower() == "true"
BRIDGE_HOST = os.getenv("BRIDGE_HOST", "127.0.0.1")
BRIDGE_PORT = int(os.getenv("BRIDGE_PORT", 5678))
BRIDGE_ACTIVITY_TIMEOUT = int(os.getenv("BRIDGE_ACTIVITY_TIMEOUT", 3700))

# Plezy source - reads playback locally from Plezy's mpv IPC pipe.
# Requires this line in Plezy's mpv config: input-ipc-server=\\.\pipe\plezympv
# Old PLEX_* env keys are still honoured as fallbacks so existing configs keep working.
PLEZY_ENABLED         = (os.getenv("PLEZY_ENABLED") or os.getenv("PLEX_ENABLED") or "true").lower() == "true"  # master switch
# Plezy can stream from either a Plex or an Emby backend; toggle each independently
# (both require PLEZY_ENABLED). false = that backend's playback never shows.
PLEZY_PLEX_ENABLED   = os.getenv("PLEZY_PLEX_ENABLED", "true").lower() == "true"
PLEZY_EMBY_ENABLED   = os.getenv("PLEZY_EMBY_ENABLED", "true").lower() == "true"
PLEZY_POLL_INTERVAL   = int(os.getenv("PLEZY_POLL_INTERVAL") or os.getenv("PLEX_POLL_INTERVAL") or 3)   # secs between mpv IPC polls
PLEZY_MPV_PIPE        = os.getenv("PLEZY_MPV_PIPE") or os.getenv("PLEX_MPV_PIPE") or r"\\.\pipe\plezympv"

LETTERBOXD_URL = os.getenv("LETTERBOXD_URL", "https://letterboxd.com/Kjerne/")
SERIALIZD_URL  = os.getenv("SERIALIZD_URL", "https://www.serializd.com/user/Kjerne/profile")
TRAKT_URL      = os.getenv("TRAKT_URL", "")


def _parse_buttons(spec):
    """Parse the RPC_BUTTONS env into an ordered list of (kind, label, url) specs.

    Comma-separates buttons. Recognised built-in kinds (case-insensitive):
      letterboxd, serializd, trakt  - static profile links
      imdb, tmdb                    - dynamic, deep-link the item being watched
    A custom static button is "Label|https://url".
    Discord shows at most 2 buttons; extras past the first 2 valid ones are dropped.
    Example: RPC_BUTTONS=imdb,letterboxd   or   RPC_BUTTONS=My Blog|https://x.com,tmdb
    """
    out = []
    for tok in (spec or "").split(","):
        tok = tok.strip()
        if not tok:
            continue
        if "|" in tok:
            label, _, url = tok.partition("|")
            label, url = label.strip(), url.strip()
            if label and url:
                out.append(("static", label, url))
        else:
            out.append((tok.lower(), None, None))
    return out


# Default keeps the original two profile buttons untouched.
RPC_BUTTONS = _parse_buttons(os.getenv("RPC_BUTTONS", "letterboxd,serializd"))

POSTER_CACHE_TTL = 21600  # 6 hours
# Failed poster lookups cache a None; keep that short so a transient Emby hiccup
# doesn't pin the fallback icon for the full 6 h.
POSTER_NULL_TTL  = int(os.getenv("POSTER_NULL_TTL", 300))   # 5 min
CACHE_MAX_SIZE   = int(os.getenv("CACHE_MAX_SIZE", 300))

headers = {"X-Emby-Token": TOKEN}

# Shared HTTP session: connection pooling + keep-alive across all outbound calls
# (Emby, TMDB, OMDB, image hosts). Reuses warm TCP/TLS connections instead of a
# fresh handshake per request. requests.Session is thread-safe for concurrent
# gets/posts, which is what the RPC loop, WS thread, and Plezy thread all do.
http = requests.Session()
_http_adapter = HTTPAdapter(pool_connections=10, pool_maxsize=20)
http.mount("https://", _http_adapter)
http.mount("http://", _http_adapter)

# Emby process-scan result cache: is_emby_app_running() enumerates every running
# process, and the loop calls it every ~2 s. Cache the verdict briefly so we scan
# a few times a minute instead of ~30 times.
EMBY_PROC_CACHE_TTL = 5  # seconds

NETFLIX_LOGO_URL = "https://upload.wikimedia.org/wikipedia/commons/thumb/0/08/Netflix_2015_logo.svg/512px-Netflix_2015_logo.svg.png"

# Browser-bridge services. The Firefox extension tags each payload with a
# "service" field; these map it to Discord-facing labels and a fallback logo.
# Disney intentionally has no logo (per request) - it falls back to the TMDB
# poster, or no large image at all.
SERVICE_LABELS = {
    "netflix": "Netflix",
    "disney": "Disney+",
    "tv2": "TV 2 Play",
}
SERVICE_LOGOS = {
    "netflix": NETFLIX_LOGO_URL,
    "disney": None,
    "tv2": None,
}

RPC_HEARTBEAT       = int(os.getenv("RPC_HEARTBEAT", 15))
HTTP_RECONCILE_INTERVAL = max(5, min(15, INTERVAL))
PAUSED_SESSION_HOLD      = int(os.getenv("PAUSED_SESSION_HOLD", 8))   # seconds
# Pause after clearing presence before the next update, so Discord drops the old
# activity/timestamps before the new one lands. Tunable if pauses flicker.
CLEAR_SETTLE        = float(os.getenv("CLEAR_SETTLE", 0.5))
# Shorter timeout for the in-loop HTTP reconcile probe so a slow/hung Emby can't
# stall the whole presence loop for the full request timeout.
RECONCILE_TIMEOUT   = int(os.getenv("RECONCILE_TIMEOUT", 4))


# -----------------------------
# Shared mutable runtime state
# -----------------------------
# All names below are reassigned at runtime; always touch them as rt.NAME.

running    = True
paused_rpc = False
RPC        = None

# OrderedDict so eviction is O(1) LRU: getters move_to_end() on a hit, and
# _evict_oldest() pops the least-recently-used item off the front.
poster_cache = OrderedDict()   # {id: (url, timestamp)}
series_cache = OrderedDict()   # {series_id: (info_dict, timestamp)}
omdb_cache   = OrderedDict()   # {cache_key: (rating_float_or_None, timestamp)}
tmdb_cache   = OrderedDict()   # {cache_key: (rating_float_or_None, timestamp)}
season_cache = OrderedDict()   # {season_id: (community_rating, critic_rating, timestamp)}
netflix_meta_cache = OrderedDict()  # {cache_key: (info_dict, timestamp)}

# Auto-reconnect tracking
last_rpc_success  = time.time()
reconnect_attempts = 0
next_reconnect_time = 0.0   # gate: don't attempt reconnect before this (non-blocking backoff)

# Thread safety: protects current_sessions (written by WS thread, read by RPC loop)
state_lock       = threading.Lock()
current_sessions = []   # kept up to date by WebSocket; HTTP fallback when WS is down
ws_connected     = False
ws_app           = None   # live WebSocketApp, so the Netflix guard can close it
ws_last_message_time = 0.0
last_http_reconcile  = 0.0

# Netflix browser extension state
netflix_lock = threading.Lock()
netflix_activity = None
last_netflix_update = 0.0

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

# Hold the last known paused session so we don't flicker to "Browsing"
# when Emby drops NowPlayingItem on pause
last_paused_session      = None
last_paused_session_time = 0

# Tooltip heartbeat tracks when the loop last ran
last_loop_time = 0.0

icon_ref        = None
console_created = False

# Cached permanent URLs for play/pause small images (populated at startup)
STATUS_ICON_PLAY  = None
STATUS_ICON_PAUSE = None
EMBY_LOGO_URL     = None

# Image-upload dedupe cache (populated lazily by images.py)
_img_cache        = {}
_img_cache_loaded = False
_img_cache_last_save = 0.0   # throttle disk writes; see images._img_cache_save
IMG_CACHE_SAVE_INTERVAL = int(os.getenv("IMG_CACHE_SAVE_INTERVAL", 15))  # seconds
_zipline_folder_id = None

# Plex/Plezy source state + caches (source_plex.py)
_PLEZY_DATA_GLOB = os.path.join(
    os.environ.get("LOCALAPPDATA", ""),
    "Packages", "edde746.Plezy_*", "LocalCache", "Roaming", "com.edde746", "Plezy",
)
plezy_state          = {}       # runtime: server_uri, server_token (parsed from the mpv path)
plezy_meta_cache     = OrderedDict()  # {thumb_path: (public_url, ts)}
plezy_part_cache     = OrderedDict()  # {part_id: metadata_dict}
plezy_poster_cache   = OrderedDict()  # {rating_key: (square_url, ts)}
plezy_emby_cache    = OrderedDict()  # {item_id: (emby_item_dict, ts)} - Plezy playing Emby content
last_plezy_ratingkey = None     # for episode/movie-change clear
last_plezy_paused    = None     # for pause-change clear
_plezy_ready         = True     # no network setup; get_plezy_activity self-gates on the pipe
_plezy_last_poll     = 0.0
_plezy_cached_activity = None
_PLEZY_MPV_PROPS = ("pause", "time-pos", "duration", "path", "idle-active")

OMDB_CACHE_TTL = 86400  # 24 hours


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


# -----------------------------
# Logging
# -----------------------------

_log_path = os.path.join(
    os.path.dirname(sys.executable if getattr(sys, "frozen", False) else os.path.abspath(os.path.join(__file__, ".."))),
    "MediaRPC.log"
)
_logger = logging.getLogger("MediaRPC")
# INFO by default; DEBUG env promotes the per-tick verbose logs (rt.debug) to disk.
_logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)
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


def ticks_to_sec(ticks):
    """Emby/Jellyfin report time in 100-ns 'ticks'. Convert to seconds."""
    return (ticks or 0) / 10_000_000


def log(msg):
    _logger.info(msg)


def debug(msg):
    """Verbose per-tick log. Emitted only when DEBUG=true (logger at DEBUG level)."""
    _logger.debug(msg)


def _evict_oldest(cache):
    """Evict the least-recently-used entry. O(1) for OrderedDict caches (front =
    LRU because getters move_to_end on a hit); falls back to an O(n) timestamp
    scan for any plain-dict cache."""
    if not cache:
        return
    if isinstance(cache, OrderedDict):
        cache.popitem(last=False)
    else:
        oldest = min(cache, key=lambda k: cache[k][-1])
        del cache[oldest]
