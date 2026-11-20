"""First-run setup and the Settings window (tkinter).

Runs in its own process (MediaRPC.exe --setup / --settings) so Tk never shares a
thread with the pystray tray loop. Imports config only, never rt, so it works even
when the current .env is incomplete. Exit code RESTART asks the caller to (re)start.
"""
import os
import subprocess
import sys
import tkinter as tk
from tkinter import ttk, messagebox

from . import config
from . import autostart
from .version import APP_TITLE, VERSION

RESTART = 3
EXTENSION_URL = "https://addons.mozilla.org/en-US/firefox/addon/mediarpc-browser-bridge/"

# (key, label, kind, default). kind: str | secret | bool | int
SECTIONS = [
    ("General", [
        ("DISCORD_CLIENT_ID", "Discord application ID", "str", ""),
        ("UPDATE_INTERVAL", "HTTP poll interval (s)", "int", "15"),
        ("BROWSING_ENABLED", "Show \"browsing\" presence", "bool", "true"),
        ("UPDATE_CHECK", "Check GitHub for updates daily", "bool", "true"),
        ("DEBUG", "Verbose debug logging", "bool", "false"),
    ]),
    ("Emby", [
        ("EMBY_ENABLED", "Enable Emby source", "bool", "true"),
        ("EMBY_SERVER", "Server URL", "str", ""),
        ("TOKEN", "API token", "secret", ""),
        ("EMBY_USER_ID", "User ID", "str", ""),
        ("AUTO_PAUSE_WHEN_CLOSED", "Pause when the Emby app is closed", "bool", "true"),
    ]),
    ("Browser", [
        ("BRIDGE_ENABLED", "Browser extension bridge", "bool", "true"),
        ("NETFLIX_ENABLED", "  Netflix", "bool", "true"),
        ("DISNEY_ENABLED", "  Disney+", "bool", "true"),
        ("TV2_ENABLED", "  TV 2 Play", "bool", "true"),
        ("SILO_ENABLED", "  Silo (web)", "bool", "true"),
        ("BRIDGE_HOST", "  Bridge host", "str", "127.0.0.1"),
        ("BRIDGE_PORT", "  Bridge port", "int", "5678"),
        ("BRIDGE_ACTIVITY_TIMEOUT", "  Clear stale session after (s)", "int", "3700"),
    ]),
    ("Players", [
        ("PLEZY_ENABLED", "Plezy (mpv)", "bool", "true"),
        ("PLEZY_PLEX_ENABLED", "  Plex backend", "bool", "true"),
        ("PLEZY_EMBY_ENABLED", "  Emby backend", "bool", "true"),
        ("PLEZY_POLL_INTERVAL", "  Poll interval (s)", "int", "3"),
        ("PLEZY_MPV_PIPE", "  mpv IPC pipe", "str", r"\\.\pipe\plezympv"),
        ("SILAGE_ENABLED", "Silage desktop app (Silo)", "bool", "true"),
        ("SILAGE_SERVER", "  Silo server (blank = auto)", "str", ""),
        ("SILAGE_API_KEY", "  Silo API key (blank = sign-in code)", "secret", ""),
        ("SILAGE_POLL_INTERVAL", "  Poll interval (s)", "int", "5"),
        ("SILAGE_HOME", "  Silage folder (blank = auto)", "str", ""),
    ]),
    ("Images & ratings", [
        ("TMDB_KEY", "TMDB API key", "secret", ""),
        ("OMDB_KEY", "OMDB API key", "secret", ""),
        ("RATING_ORDER", "Rating order", "str", "emby,tmdb,omdb,critic"),
        ("ZIPLINE_URL", "Zipline URL", "str", ""),
        ("ZIPLINE_TOKEN", "Zipline token", "secret", ""),
        ("ZIPLINE_EXPIRES", "Zipline expiry", "str", "7d"),
        ("ZIPLINE_FOLDER", "Zipline folder", "str", "MediaRPC"),
        ("IMGBB_KEY", "ImgBB key (fallback)", "secret", ""),
    ]),
    ("Buttons", [
        ("LETTERBOXD_URL", "Letterboxd profile", "str", ""),
        ("SERIALIZD_URL", "Serializd profile", "str", ""),
        ("TRAKT_URL", "Trakt profile", "str", ""),
        ("RPC_BUTTONS", "Buttons (e.g. imdb,letterboxd)", "str", "letterboxd,serializd"),
    ]),
    ("Advanced", [
        ("RPC_HEARTBEAT", "Force presence refresh every (s)", "int", "15"),
        ("PAUSED_SESSION_HOLD", "Hold vanished paused Emby session (s)", "int", "8"),
        ("CLEAR_SETTLE", "Wait after clearing presence (s)", "float", "0.5"),
        ("RECONCILE_TIMEOUT", "Emby re-check timeout (s)", "int", "4"),
        ("POSTER_NULL_TTL", "Retry failed poster after (s)", "int", "300"),
        ("CACHE_MAX_SIZE", "Max entries per cache", "int", "300"),
        ("IMG_CACHE_SAVE_INTERVAL", "Image cache save interval (s)", "int", "15"),
    ]),
]


def open_folder(path):
    os.makedirs(path, exist_ok=True)
    os.startfile(path)  # noqa: S606 - opens Explorer on our own folder


BG, FG, FIELD, MUTED, ACCENT, BORDER = "#1e1f22", "#e6e6e6", "#2b2d31", "#9a9ca3", "#5865f2", "#3a3c42"


def _dark(root):
    """Dark ttk theme plus a dark Windows title bar."""
    root.configure(bg=BG)
    st = ttk.Style(root)
    st.theme_use("clam")
    st.configure(".", background=BG, foreground=FG, fieldbackground=FIELD, bordercolor=BORDER,
                 darkcolor=BG, lightcolor=BG, troughcolor=FIELD, focuscolor=ACCENT,
                 selectbackground=ACCENT, selectforeground="#ffffff", insertcolor=FG,
                 font=("Segoe UI", 10))
    st.configure("TFrame", background=BG)
    st.configure("TLabel", background=BG, foreground=FG)
    st.configure("Muted.TLabel", foreground=MUTED)
    st.configure("Error.TLabel", foreground="#ff6b6b")
    st.configure("Title.TLabel", font=("Segoe UI", 13, "bold"))
    st.configure("TEntry", fieldbackground=FIELD, foreground=FG, bordercolor=BORDER, padding=4)
    st.map("TEntry", bordercolor=[("focus", ACCENT)])
    st.configure("TCombobox", fieldbackground=FIELD, background=FIELD, foreground=FG, arrowcolor=FG, padding=3)
    st.map("TCombobox", fieldbackground=[("readonly", FIELD)], foreground=[("readonly", FG)],
           selectbackground=[("readonly", FIELD)], selectforeground=[("readonly", FG)])
    root.option_add("*TCombobox*Listbox.background", FIELD)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    st.configure("TCheckbutton", background=BG, foreground=FG, indicatorbackground=FIELD,
                 indicatorforeground="#ffffff")
    st.map("TCheckbutton", background=[("active", BG)], indicatorbackground=[("selected", ACCENT), ("active", BORDER)])
    st.configure("TButton", background=FIELD, foreground=FG, bordercolor=BORDER, padding=(12, 5))
    st.map("TButton", background=[("pressed", BORDER), ("active", "#35373c")])
    st.configure("Accent.TButton", background=ACCENT, foreground="#ffffff", bordercolor=ACCENT)
    st.map("Accent.TButton", background=[("pressed", "#3c45a5"), ("active", "#4752c4")])
    st.configure("TNotebook", background=BG, bordercolor=BORDER, tabmargins=(0, 4, 0, 0))
    st.configure("TNotebook.Tab", background=BG, foreground=MUTED, bordercolor=BORDER, padding=(12, 5))
    st.map("TNotebook.Tab", background=[("selected", FIELD)], foreground=[("selected", FG)])
    try:
        import ctypes
        root.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        on = ctypes.c_int(1)
        for attr in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE (new, old builds)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(on), ctypes.sizeof(on)) == 0:
                break
    except Exception:
        pass


def _icon(root):
    try:
        base = getattr(sys, "_MEIPASS", config.PKG_DIR)
        root.iconbitmap(os.path.join(base, "Images", "MediaRPC_Active.ico"))
    except Exception:
        pass


def run_setup():
    """First run (no settings yet): import old settings and/or enable autostart."""
    found = config.legacy_candidates()
    root = tk.Tk()
    root.title(f"{APP_TITLE} setup")
    _icon(root)
    _dark(root)
    root.resizable(False, False)
    frm = ttk.Frame(root, padding=16)
    frm.grid()
    ttk.Label(frm, text=f"Welcome to {APP_TITLE} v{VERSION}", style="Title.TLabel").grid(sticky="w")
    ttk.Label(frm, text=f"Settings will be stored in:\n{config.settings_dir()}").grid(sticky="w", pady=(6, 10))

    imp = tk.BooleanVar(value=bool(found))
    src = tk.StringVar(value=found[0] if found else "")
    ttk.Checkbutton(frm, text="Import settings from an earlier MediaRPC", variable=imp).grid(sticky="w")
    row = ttk.Frame(frm)
    row.grid(sticky="we", padx=(22, 0), pady=(2, 0))
    row.columnconfigure(0, weight=1)
    cb = ttk.Combobox(row, textvariable=src, values=found, state="readonly", width=56)
    cb.grid(row=0, column=0, sticky="we")
    hint = ttk.Label(frm, text="" if found else "No earlier settings found automatically. Use Browse to pick a folder.",
                     style="Muted.TLabel")
    hint.grid(sticky="w", padx=(22, 0), pady=(2, 8))

    def browse():
        from tkinter import filedialog
        d = filedialog.askdirectory(parent=root, title="Folder with your old MediaRPC .env",
                                    initialdir=src.get() or os.path.expanduser("~"), mustexist=True)
        if not d:
            return
        d = os.path.abspath(d)
        if not os.path.isfile(os.path.join(d, ".env")):
            messagebox.showwarning(APP_TITLE, "That folder has no .env file.", parent=root)
            return
        if d not in found:
            found.insert(0, d)
            cb.configure(values=found)
        src.set(d)
        imp.set(True)
        hint.configure(text="")

    ttk.Button(row, text="Browse...", command=browse).grid(row=0, column=1, padx=(6, 0))

    auto = tk.BooleanVar(value=True)
    ttk.Checkbutton(frm, text="Start with Windows", variable=auto).grid(sticky="w")

    result = {"code": 0}

    def go():
        try:
            if imp.get() and src.get():
                copied = config.import_from(src.get())
                if not copied:
                    messagebox.showinfo(APP_TITLE, "Nothing new to import; existing files were kept.")
            config.create_default()
            autostart.set_enabled(auto.get())
        except OSError as e:
            messagebox.showerror(APP_TITLE, f"Setup failed: {e}")
            return
        result["code"] = RESTART
        root.destroy()

    btns = ttk.Frame(frm)
    btns.grid(sticky="e", pady=(14, 0))
    ttk.Button(btns, text="Cancel", command=root.destroy).grid(row=0, column=0, padx=4)
    ttk.Button(btns, text="Continue", command=go, style="Accent.TButton").grid(row=0, column=1)
    root.mainloop()
    return result["code"]


def run_settings(message=None):
    """Edit every option. Returns RESTART when the user chose Save & restart."""
    current = config.read_env()  # nothing is written until Save
    root = tk.Tk()
    root.title(f"{APP_TITLE} settings (v{VERSION})")
    _icon(root)
    _dark(root)
    root.minsize(560, 420)
    outer = ttk.Frame(root, padding=10)
    outer.pack(fill="both", expand=True)
    if message:
        ttk.Label(outer, text=message, style="Error.TLabel", wraplength=520).pack(anchor="w", pady=(0, 6))
    nb = ttk.Notebook(outer)
    nb.pack(fill="both", expand=True)

    vars_ = {}
    for title, fields in SECTIONS:
        page = ttk.Frame(nb, padding=10)
        page.columnconfigure(1, weight=1)
        nb.add(page, text=title)
        for row, (key, label, kind, default) in enumerate(fields):
            val = current.get(key, default)
            if kind == "bool":
                v = tk.BooleanVar(value=str(val).lower() == "true")
                ttk.Checkbutton(page, text=label.strip(), variable=v).grid(
                    row=row, column=0, columnspan=2, sticky="w", padx=(18 if label.startswith("  ") else 0, 0), pady=2)
            else:
                v = tk.StringVar(value=val)
                ttk.Label(page, text=label.strip()).grid(
                    row=row, column=0, sticky="w", padx=(18 if label.startswith("  ") else 0, 8), pady=2)
                e = ttk.Entry(page, textvariable=v, show="•" if kind == "secret" else "")
                e.grid(row=row, column=1, sticky="we", pady=2)
                if kind == "secret":
                    def toggle(ev, e=e):
                        e.configure(show="" if e.cget("show") else "•")
                    e.bind("<Double-Button-1>", toggle)
            vars_[key] = (v, kind, default)
        if title == "General":
            r = len(fields)
            auto = tk.BooleanVar(value=autostart.is_enabled())
            ttk.Checkbutton(page, text="Start with Windows", variable=auto).grid(
                row=r, column=0, columnspan=2, sticky="w", pady=(8, 2))
            ttk.Label(page, text="Tip: double-click a hidden key field to show it.",
                      style="Muted.TLabel").grid(row=r + 1, column=0, columnspan=2, sticky="w", pady=(10, 0))

    result = {"code": 0}

    def save(restart):
        updates = {}
        for key, (v, kind, default) in vars_.items():
            if kind == "bool":
                val = "true" if v.get() else "false"
            else:
                val = v.get().strip()
                if kind == "int" and val and not val.isdigit():
                    messagebox.showerror(APP_TITLE, f"{key} must be a whole number.")
                    return
                if kind == "float" and val:
                    try:
                        float(val)
                    except ValueError:
                        messagebox.showerror(APP_TITLE, f"{key} must be a number.")
                        return
            if val == current.get(key, default) and key not in current:
                continue  # untouched default: leave the file alone
            updates[key] = val if (val or key in current) else None
        try:
            config.write_env(updates)
        except OSError as e:
            messagebox.showerror(APP_TITLE, f"Couldn't save settings: {e}")
            return
        autostart.set_enabled(auto.get())
        result["code"] = RESTART if restart else 0
        if not restart:
            messagebox.showinfo(APP_TITLE, "Saved. Changes apply after MediaRPC restarts.")
        root.destroy()

    bar = ttk.Frame(outer)
    bar.pack(fill="x", pady=(10, 0))
    ttk.Button(bar, text="Open settings folder", command=lambda: open_folder(config.settings_dir())).pack(side="left")
    ttk.Button(bar, text="Get browser extension",
               command=lambda: __import__("webbrowser").open(EXTENSION_URL)).pack(side="left", padx=6)
    ttk.Button(bar, text="Save & restart", command=lambda: save(True), style="Accent.TButton").pack(side="right")
    ttk.Button(bar, text="Save", command=lambda: save(False)).pack(side="right", padx=6)
    ttk.Button(bar, text="Cancel", command=root.destroy).pack(side="right")
    root.mainloop()
    return result["code"]


def launch(*args):
    """Start a UI process of this same app (exe or source) with the given flags."""
    if config.FROZEN:
        cmd = [sys.executable, *args]
    else:
        cmd = [sys.executable, os.path.join(config.PKG_DIR, "run_mediarpc.py"), *args]
    return subprocess.Popen(cmd, close_fds=True)
