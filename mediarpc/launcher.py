"""Process entry: command-line modes, first-run setup, then the tray app.

  (no args)            normal start; first run shows the setup window
  --settings           only the Settings window
  --start-after        with --settings: start MediaRPC after "Save & restart"
  --message TEXT       with --settings: note shown at the top (e.g. missing keys)
  --wait-pid PID       wait for an old instance to exit first (restart / update)

rt loads .env when it is imported, so setup runs before anything imports it.
"""
import argparse
import sys


def wait_for_pid(pid, timeout_s=20.0):
    if sys.platform != "win32" or pid <= 0:
        return
    import ctypes
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
    if h:
        k32.WaitForSingleObject(h, int(timeout_s * 1000))
        k32.CloseHandle(h)


_mutex = None  # held for the whole process lifetime


def _claim_single_instance():
    """True if this is the only running MediaRPC tray app (per Windows user)."""
    global _mutex
    if sys.platform != "win32":
        return True
    import ctypes
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW.restype = ctypes.c_void_p
    _mutex = k32.CreateMutexW(None, False, r"Local\MediaRPC-instance")
    return k32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def _stop_other_instances():
    """Used when a second launch's Settings asks for a restart."""
    try:
        import os
        import psutil
        me = {os.getpid(), os.getppid()}
        name = os.path.basename(sys.executable).lower()
        for p in psutil.process_iter(["pid", "name"]):
            if p.info["pid"] not in me and (p.info.get("name") or "").lower() == name:
                p.terminate()
        psutil.wait_procs([p for p in psutil.process_iter(["name"])
                           if (p.info.get("name") or "").lower() == name and p.pid not in me], timeout=5)
    except Exception:
        pass


def main(argv=None):
    p = argparse.ArgumentParser(prog="MediaRPC")
    p.add_argument("--settings", action="store_true")
    p.add_argument("--start-after", action="store_true")
    p.add_argument("--message")
    p.add_argument("--wait-pid", type=int, default=0)
    args, _ = p.parse_known_args(argv)

    if args.wait_pid:
        wait_for_pid(args.wait_pid)

    from . import config, ui_settings
    if args.settings:
        code = ui_settings.run_settings(args.message)
        if code == ui_settings.RESTART and args.start_after:
            ui_settings.launch()
            return
        sys.exit(code)  # the tray waits for RESTART to restart itself

    if not _claim_single_instance():
        # Already running: a second launch opens Settings instead of a duplicate.
        if ui_settings.run_settings() == ui_settings.RESTART and config.FROZEN:
            _stop_other_instances()
            ui_settings.launch()
        return

    if not config.has_settings():
        if ui_settings.run_setup() != ui_settings.RESTART:
            return  # setup cancelled
        # Review what was imported (or fill it in) before the first start.
        msg = "Check your settings, then press Save & restart to start MediaRPC."
        if ui_settings.run_settings(msg) != ui_settings.RESTART:
            return

    from .app import main as app_main
    app_main()
