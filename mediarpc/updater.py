"""Update check and one-click update from GitHub Releases (same scheme as F1DockTimers).

* Check: GET https://api.github.com/repos/<repo>/releases/latest (public, no account).
  Once ~30 s after start and then daily, plus "Check for updates" in the tray.
* Install (frozen exe only): download MediaRPC-<ver>.exe from the release, verify its
  SHA-256 against the digest GitHub publishes for the asset, then a small helper
  script waits for this process to exit, swaps the new exe over the running one
  (same path and name, so .env / silage_auth.json next to it and the Start-with-
  Windows entry keep working) and starts it again.
* Running from source: just open the release page.
"""
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser

from . import rt
from .version import REPO, USER_AGENT, VERSION

API_LATEST = f"https://api.github.com/repos/{REPO}/releases/latest"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
CHECK_EVERY_S = 24 * 3600
FIRST_CHECK_S = 30
MAX_DOWNLOAD = 200 * 1024 * 1024
# Release files may only come from GitHub's own hosts.
DOWNLOAD_HOSTS = ("github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com")

latest = None          # parsed release dict when a newer version exists
installing = False
_lock = threading.Lock()


def parse_version(text):
    """"v1.0.3" / "1.0.3" -> (1, 0, 3); None for anything else (e.g. pre-release tags)."""
    m = re.fullmatch(r"[vV]?(\d{1,4})\.(\d{1,4})\.(\d{1,4})", str(text or "").strip())
    return tuple(int(g) for g in m.groups()) if m else None


def is_newer(remote, local=VERSION):
    r, l = parse_version(remote), parse_version(local)
    return bool(r and l and r > l)


def pick_asset(release):
    """The MediaRPC-<ver>.exe asset of a release, with url, size and sha256."""
    ver = parse_version(release.get("tag_name"))
    if ver is None:
        return None
    # The file must carry the release's own version, otherwise the "new" app would
    # report an older version and the update would be offered forever.
    expected = "MediaRPC-" + ".".join(str(n) for n in ver) + ".exe"
    for a in release.get("assets") or []:
        if not isinstance(a, dict) or a.get("name") != expected:
            continue
        digest = str(a.get("digest") or "")
        sha = digest[7:].lower() if digest.startswith("sha256:") else ""
        url = str(a.get("browser_download_url") or "")
        if not re.fullmatch(r"[0-9a-f]{64}", sha) or not url.startswith("https://github.com/"):
            return None
        return {"name": expected, "url": url, "size": int(a.get("size") or 0), "sha256": sha}
    return None


def _headers():
    return {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28"}


def check(manual=False):
    """Query GitHub. Sets `latest` and notifies; manual=True also reports 'up to date'."""
    global latest
    from . import tray
    try:
        r = rt.http.get(API_LATEST, headers=_headers(), timeout=15)
    except Exception as e:
        rt.log(f"[Update] check failed: {e}")
        if manual:
            tray.notify("Couldn't reach GitHub to check for updates.")
        return
    if r.status_code != 200:
        rt.log(f"[Update] check HTTP {r.status_code}")
        if manual:
            tray.notify("No release published yet." if r.status_code == 404
                        else f"Couldn't reach GitHub ({r.status_code}).")
        return
    try:
        data = r.json()
    except ValueError:
        data = {}
    tag = str(data.get("tag_name") or "")
    if data.get("draft") or data.get("prerelease") or not is_newer(tag):
        latest = None
        if manual:
            tray.notify(f"You're up to date (v{VERSION}).")
        return
    ver = ".".join(str(n) for n in parse_version(tag))
    page = str(data.get("html_url") or "")
    latest = {"version": ver, "raw": data,
              "url": page if page.startswith(f"https://github.com/{REPO}/") else RELEASES_PAGE}
    rt.log(f"[Update] v{ver} available (running v{VERSION})")
    tray.refresh_menu()
    tray.notify(f"MediaRPC v{ver} is available. Use \"Install update\" in the tray menu.")


def check_loop():
    """Background thread: first check shortly after start, then daily."""
    if not rt.UPDATE_CHECK_ENABLED:
        return
    time.sleep(FIRST_CHECK_S)
    while rt.running:
        check(manual=False)
        for _ in range(CHECK_EVERY_S // 10):
            if not rt.running:
                return
            time.sleep(10)


def can_install():
    return getattr(sys, "frozen", False) and latest is not None and pick_asset(latest["raw"]) is not None


def install():
    """Download, verify and swap in the latest release. Runs on its own thread."""
    global installing
    from . import tray
    with _lock:
        if installing or latest is None:
            return
        installing = True
    try:
        if not can_install():
            webbrowser.open(latest["url"])
            return
        asset = pick_asset(latest["raw"])
        if asset["size"] > MAX_DOWNLOAD:
            tray.notify("The update download is unexpectedly large; not installing.")
            return
        tray.notify(f"Downloading MediaRPC v{latest['version']}...")
        folder = os.path.join(tempfile.gettempdir(), "MediaRPC-update")
        os.makedirs(folder, exist_ok=True)
        target = os.path.join(folder, asset["name"])
        h = hashlib.sha256()
        size = 0
        with rt.http.get(asset["url"], headers={"User-Agent": USER_AGENT}, stream=True, timeout=60) as r:
            host = (r.url.split("/")[2] if "://" in r.url else "").lower()
            if r.status_code != 200 or not any(host == d or host.endswith("." + d) for d in DOWNLOAD_HOSTS):
                tray.notify("The update download failed; opening the release page.")
                webbrowser.open(latest["url"])
                return
            with open(target, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    size += len(chunk)
                    if size > MAX_DOWNLOAD:
                        raise IOError("download too large")
                    h.update(chunk)
                    f.write(chunk)
        if h.hexdigest() != asset["sha256"]:
            os.remove(target)
            tray.notify("The update doesn't match GitHub's published checksum; not installing.")
            return
        _swap_and_restart(target)
    except Exception as e:
        rt.log(f"[Update] install failed: {e}")
        tray.notify("Update failed; opening the release page.")
        webbrowser.open(latest["url"])
    finally:
        installing = False


def _swap_and_restart(new_exe):
    """Hand over to a helper script that replaces this exe once we've exited."""
    from . import tray
    old = sys.executable
    # One-file builds run as bootloader (parent) + Python (child); both hold the exe.
    pids = {os.getpid(), os.getppid()}
    waits = "\n".join(
        f':w{p}\ntasklist /FI "PID eq {p}" 2>nul | find "{p}" >nul && (timeout /t 1 /nobreak >nul & goto w{p})'
        for p in pids)
    script = os.path.join(os.path.dirname(new_exe), "apply-update.cmd")
    with open(script, "w", encoding="utf-8") as f:
        f.write(f"""@echo off
{waits}
set n=0
:mv
move /y "{new_exe}" "{old}" >nul 2>&1 && goto run
set /a n+=1
if %n% lss 30 (timeout /t 1 /nobreak >nul & goto mv)
:run
start "" "{old}"
del "%~f0"
""")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen(["cmd", "/c", script], creationflags=flags, close_fds=True)
    rt.log(f"[Update] installing v{latest['version']}; restarting")
    tray.quit_app(rt.icon_ref, None)
