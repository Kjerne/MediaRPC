"""System-tray icon, menu, console toggle."""
import os
import sys
import time
import ctypes
import ctypes.wintypes
from ctypes import wintypes
import webbrowser
import pystray
from PIL import Image

from . import rt
from . import discord_rpc
from . import images


class LeftClickIcon(pystray.Icon):
    """pystray Icon that opens the context menu on left-click as well as right-click."""

    def _on_notify(self, wparam, lparam):
        from pystray._util import win32
        # Show the menu on either mouse button release
        if self._menu_handle and lparam in (win32.WM_LBUTTONUP, win32.WM_RBUTTONUP):
            # pystray builds the native menu once and only rebuilds it after a
            # menu item is clicked, so callable labels (the Status submenu) froze
            # at their startup values ("Source: idle"). Rebuild right before
            # showing so they reflect live state. Cheap: a handful of items.
            self._update_menu()
            if not self._menu_handle:
                return
            # TrackPopupMenuEx does not behave unless our systray window is the
            # foreground window
            win32.SetForegroundWindow(self._hwnd)

            point = wintypes.POINT()
            win32.GetCursorPos(ctypes.byref(point))

            hmenu, descriptors = self._menu_handle
            index = win32.TrackPopupMenuEx(
                hmenu,
                win32.TPM_RIGHTALIGN | win32.TPM_BOTTOMALIGN
                | win32.TPM_RETURNCMD,
                point.x,
                point.y,
                self._menu_hwnd,
                None)
            if index > 0:
                descriptors[index - 1](self)


def show_console():
    if not rt.console_created:
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
        rt.console_created = True


def toggle_console(icon, item):
    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if hwnd == 0:
        show_console()
        rt.log("Console opened")
    else:
        # Toggle visibility: hide if shown, show if hidden
        GWL_STYLE  = -16
        WS_VISIBLE = 0x10000000
        style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_STYLE)
        if style & WS_VISIBLE:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
        else:
            ctypes.windll.user32.ShowWindow(hwnd, 5)  # SW_SHOW


# Renderers call set_icon/set_tooltip every loop tick. Load each .ico once and
# skip no-op updates, so we don't hit the disk and re-register the tray icon
# with the shell every 2 s.
_icon_images = {}
_current_icon = None
_current_tooltip = None


def _load_icon(paused):
    img = _icon_images.get(paused)
    if img is None:
        icon_file = "MediaRPC_Inactive.ico" if paused else "MediaRPC_Active.ico"
        path = rt.resource_path(os.path.join("Images", icon_file))
        if not os.path.exists(path):
            rt.log(f"ERROR: Icon file not found: {path}")
            return None
        with Image.open(path) as f:
            img = f.copy()
        _icon_images[paused] = img
    return img


def set_icon(paused):
    global _current_icon
    paused = bool(paused)
    if rt.icon_ref is None or _current_icon == paused:
        return
    img = _load_icon(paused)
    if img is not None:
        rt.icon_ref.icon = img
        _current_icon = paused


def set_tooltip(text):
    global _current_tooltip
    if rt.icon_ref is None or text == _current_tooltip:
        return
    try:
        rt.icon_ref.title = text
        _current_tooltip = text
    except Exception:
        pass


def toggle_rpc(icon, item):
    rt.paused_rpc = not rt.paused_rpc

    if rt.paused_rpc:
        rt.log("RPC manually PAUSED by user")
        discord_rpc.safe_clear()
        # Resume must re-push even if the content is unchanged.
        rt.last_pushed_payload = None
        set_icon(True)
        set_tooltip("MediaRPC - Paused")
    else:
        rt.log("RPC manually RESUMED by user")
        set_icon(False)
        set_tooltip("MediaRPC")


def open_letterboxd(icon, item):
    webbrowser.open(rt.LETTERBOXD_URL)


def open_serializd(icon, item):
    webbrowser.open(rt.SERIALIZD_URL)


def quit_app(icon, item):
    rt.running = False

    discord_rpc.safe_clear()
    # Throttled saves may be holding the newest uploads in memory only.
    if rt._img_cache_loaded:
        images._img_cache_save(force=True)

    icon.stop()


_SOURCE_LABELS = {
    "playing":  "Emby (playing)",
    "browsing": "Emby (browsing)",
    "plezy":    "Plezy",
    "netflix":  "Browser (Netflix/Disney+/TV 2)",
}


def _status_source(item):
    if rt.paused_rpc:
        return "Source: paused by user"
    if rt.last_mode == "netflix":
        # Name the actual streaming service instead of the generic bridge label.
        with rt.netflix_lock:
            svc = ((rt.netflix_activity or {}).get("service") or "netflix").lower()
        return f"Source: {rt.SERVICE_LABELS.get(svc, 'Browser')} (browser)"
    return f"Source: {_SOURCE_LABELS.get(rt.last_mode, 'idle')}"


def _status_conn(item):
    discord = "connected" if rt.RPC else "off"
    ws      = "up" if rt.ws_connected else "down"
    return f"Discord: {discord}   ·   WebSocket: {ws}"


def create_menu():
    # pystray re-evaluates callable labels each time the menu opens, so the Status
    # submenu always reflects live state. Items are display-only (enabled=False).
    status_menu = pystray.Menu(
        pystray.MenuItem(_status_source, None, enabled=False),
        pystray.MenuItem(_status_conn,   None, enabled=False),
    )
    return pystray.Menu(
        pystray.MenuItem(
            lambda item: "▶ Resume RPC" if rt.paused_rpc else "⏸ Pause RPC",
            toggle_rpc
        ),
        pystray.MenuItem("🔄 Refresh RPC", discord_rpc.refresh_rpc),
        pystray.MenuItem("ℹ Status",       status_menu),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("📽 Open Letterboxd",  open_letterboxd),
        pystray.MenuItem("📺 Open Serializd",   open_serializd),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("🖥 Toggle Console",   toggle_console),
        pystray.MenuItem("❌ Quit",             quit_app)
    )


def tray():

    global _current_icon, _current_tooltip
    image = _load_icon(True)
    menu  = create_menu()
    _current_icon    = True
    _current_tooltip = "MediaRPC"

    rt.icon_ref = LeftClickIcon(
        "MediaRPC",
        image,
        "MediaRPC",
        menu
    )

    rt.icon_ref.run()
