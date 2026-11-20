"""Where MediaRPC keeps its settings, finding old ones, and editing .env in place.

Imported before rt (which loads .env at import time), so it must not import rt.

Settings folder:
  * portable: a .env next to the exe wins (old layout, keeps working as is)
  * otherwise %APPDATA%\\MediaRPC (the default for new installs)
  * running from source: the mediarpc package folder (unchanged dev layout)
The folder also holds silage_auth.json, image_cache.json and the log.
"""
import os
import re
import shutil
import sys

FROZEN = getattr(sys, "frozen", False)
PKG_DIR = os.path.dirname(os.path.abspath(__file__))
EXE_DIR = os.path.dirname(sys.executable) if FROZEN else PKG_DIR
APPDATA_DIR = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "MediaRPC")

# Files that belong to a settings folder and move with it.
CARRY_FILES = (".env", "silage_auth.json", "image_cache.json")

ENV_TEMPLATE = """# MediaRPC settings. Edit them from the tray menu: Settings.
DISCORD_CLIENT_ID=
EMBY_SERVER=
TOKEN=
EMBY_USER_ID=
"""


def settings_dir():
    if not FROZEN:
        return PKG_DIR
    if os.path.exists(os.path.join(EXE_DIR, ".env")):
        return EXE_DIR
    return APPDATA_DIR


def env_path():
    return os.path.join(settings_dir(), ".env")


def has_settings():
    return os.path.exists(env_path())


def _autostart_dir():
    """Folder of the exe an existing Start-with-Windows entry points at."""
    if sys.platform != "win32":
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Run") as k:
            cmd, _ = winreg.QueryValueEx(k, "MediaRPC")
    except OSError:
        return None
    m = re.match(r'\s*"([^"]+)"|\s*(\S+)', str(cmd))
    exe = (m.group(1) or m.group(2)) if m else None
    return os.path.dirname(exe) if exe and exe.lower().endswith(".exe") else None


def legacy_candidates():
    """Folders with an older MediaRPC .env, most likely first, deduplicated."""
    home = os.path.expanduser("~")
    desktop = os.path.join(home, "Desktop")
    dirs = [
        _autostart_dir(),
        EXE_DIR,
        os.getcwd(),
        os.path.join(desktop, "MediaRPC", "dist"),
        os.path.join(desktop, "MediaRPC", "mediarpc"),
        os.path.join(desktop, "MediaRPC"),
        os.path.join(desktop, "Emby-RPC", "dist"),
        os.path.join(desktop, "Emby-RPC"),
        os.path.join(home, "Downloads", "MediaRPC"),
        APPDATA_DIR,
    ]
    out, seen, mine = [], set(), os.path.normcase(os.path.abspath(settings_dir()))
    for d in dirs:
        if not d:
            continue
        key = os.path.normcase(os.path.abspath(d))
        if key in seen or key == mine:
            continue
        seen.add(key)
        if os.path.isfile(os.path.join(d, ".env")):
            out.append(os.path.abspath(d))
    return out


def import_from(src_dir, dst_dir=None):
    """Copy settings files from an old folder. Never overwrites. Returns copied names."""
    dst_dir = dst_dir or settings_dir()
    os.makedirs(dst_dir, exist_ok=True)
    copied = []
    for name in CARRY_FILES:
        src, dst = os.path.join(src_dir, name), os.path.join(dst_dir, name)
        if os.path.isfile(src) and not os.path.exists(dst):
            shutil.copy2(src, dst)
            copied.append(name)
    return copied


def create_default():
    os.makedirs(settings_dir(), exist_ok=True)
    if not has_settings():
        with open(env_path(), "w", encoding="utf-8") as f:
            f.write(ENV_TEMPLATE)


# ── .env read / write that keeps the user's comments and order ──────────────

_LINE = re.compile(r"^\s*(#\s*)?([A-Z][A-Z0-9_]*)\s*=(.*)$")


def read_env(path=None):
    """{KEY: value} for active (uncommented) lines."""
    vals = {}
    try:
        with open(path or env_path(), encoding="utf-8") as f:
            for line in f:
                m = _LINE.match(line.rstrip("\r\n"))
                if m and not m.group(1):
                    v = m.group(3).strip()
                    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
                        v = v[1:-1]
                    vals[m.group(2)] = v
    except FileNotFoundError:
        pass
    return vals


def write_env(updates, path=None):
    """Set keys in place. A value of None comments the key out (back to its default).
    Commented example lines are reused so a key lands next to its documentation."""
    path = path or env_path()
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        lines = []
    todo = dict(updates)
    active = {}
    commented = {}
    for i, line in enumerate(lines):
        m = _LINE.match(line)
        if m:
            (commented if m.group(1) else active).setdefault(m.group(2), i)
    for key, val in list(todo.items()):
        if val is None:
            if key in active:
                lines[active[key]] = "#" + lines[active[key]].lstrip()
            del todo[key]  # an already-commented example line stays as written
            continue
        idx = active.get(key, commented.get(key))
        if idx is None:
            continue
        lines[idx] = f"{key}={val}"
        del todo[key]
    extra = [f"{k}={v}" for k, v in todo.items() if v is not None]
    if extra:
        lines += [""] + extra
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)
