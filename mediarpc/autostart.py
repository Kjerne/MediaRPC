"""Start with Windows via the per-user HKCU Run registry key (no admin rights)."""
import os
import sys

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE = "MediaRPC"


def command():
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}"'
    exe = sys.executable
    pyw = os.path.join(os.path.dirname(exe), "pythonw.exe")
    launcher = os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_mediarpc.py")
    return f'"{pyw if os.path.exists(pyw) else exe}" "{launcher}"'


def is_enabled():
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, VALUE)
            return True
    except OSError:
        return False


def set_enabled(on):
    if sys.platform != "win32":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if on:
                winreg.SetValueEx(k, VALUE, 0, winreg.REG_SZ, command())
            else:
                try:
                    winreg.DeleteValue(k, VALUE)
                except FileNotFoundError:
                    pass
        return True
    except OSError:
        return False


def refresh():
    """Re-point an existing entry at this exe (it may have moved or been renamed).
    Frozen builds only, so a dev run from source doesn't hijack the entry."""
    if getattr(sys, "frozen", False) and is_enabled():
        set_enabled(True)
