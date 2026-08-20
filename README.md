<p align="center">
  <img src="MediaRPC-Banner.png" alt="MediaRPC" width="100%" />
</p>

# MediaRPC

Discord Rich Presence for your media - **Emby**, **browser streaming** (Netflix, Disney+, TV 2 Play), and **Plezy** - from a single lightweight Windows tray app. Shows what you're watching with title, episode, live progress bar, poster art, ratings, and buttons.

- **Emby** - real-time via WebSocket, your session only.
- **Browser bridge** - a Firefox extension scrapes Netflix / Disney+ / TV 2 Play playback and forwards it locally.
- **Plezy** - reads live playback straight from Plezy's mpv player (works even when you are **not** the Plex server owner). Handles both Plex- and Emby-backed streams.

Priority when several are active: **Emby (playing) → Plezy → Browser → Emby (browsing) → idle**.

---

## Features

- `Watching <title>` header with episode `SxEy - Title`, live progress bar, play/pause.
- Poster art hosted on your **Zipline** instance or **ImgBB**, with a persistent cache so the same poster only uploads once per expiry window.
- Ratings from **Emby**, **TMDB**, and **OMDB/IMDb** - with a configurable preference order (`RATING_ORDER`).
- **Per-source toggles**: enable/disable Emby, Plezy (per backend), and each browser service independently.
- **Configurable profile buttons** (`RPC_BUTTONS`): Letterboxd, Serializd, Trakt, or per-item IMDb/TMDB deep links.
- Tray icon reflects play/paused state; live **Status** submenu; auto-pause when the Emby app is closed.

---

## Requirements

- Windows, Python 3.10+
- A Discord application ID ([developer portal](https://discord.com/developers/applications))
- Emby server + API token (for the Emby source)
- Optional: TMDB & OMDB API keys, a Zipline instance or ImgBB key

## Setup

1. **Clone** and enter the folder.
2. **Configure**: copy `.env.example` → `.env` and fill it in.
3. **Build**: run `mediarpc\setup.bat` (installs deps + builds `dist\MediaRPC.exe`).
   - Quick rebuilds afterwards: `mediarpc\build.bat`.
4. **Run**: `dist\MediaRPC.exe` (lives in the system tray).

### Getting your Emby token and user ID

Open the Emby web client, log in, then press `F12` to open the browser console
and run:

```js
({
    userId: ApiClient._serverInfo.UserId,
    token: ApiClient.accessToken()
})
```

Copy `token` into `TOKEN` and `userId` into `EMBY_USER_ID` in your `.env`.

## Browser bridge extension (Firefox)

The extension scrapes Netflix / Disney+ / TV 2 Play playback and posts it to the
app at `http://127.0.0.1:5678/bridge` (matches `BRIDGE_PORT`). If you change the
port, update `background.js` and the manifest permission to match.

### Option A - Install from Mozilla Add-ons (recommended, easiest)

Download the signed, always-up-to-date build straight from Mozilla - no building,
no `about:config`, survives restarts:

**→ [addons.mozilla.org/firefox/addon/mediarpc-browser-bridge](https://addons.mozilla.org/en-US/firefox/addon/mediarpc-browser-bridge/)**

Click **Add to Firefox** and you're done.

### Option B - Temporary (quick test, resets on restart)

1. `about:debugging#/runtime/this-firefox` → **Load Temporary Add-on**.
2. Select `browser-extension/firefox/manifest.json`.

Works in any Firefox but is removed when you close the browser.

### Option C - Sign it yourself via Mozilla (free)

Prefer to build/sign your own instead of using the listing:

1. Create an account at [addons.mozilla.org](https://addons.mozilla.org/developers/).
2. **Submit a New Add-on** → choose **On your own site** (unlisted / self-distribution).
3. Upload `browser-extension/mediarpc-browser-bridge.xpi` (or zip the
   `browser-extension/firefox/` folder yourself).
4. Download the **signed `.xpi`** Mozilla returns.
5. Install it: Firefox → `about:addons` → gear ⚙ → **Install Add-on From File** → pick the signed `.xpi`.

### Option D - Developer/ESR Firefox

Firefox Developer Edition, Nightly, or ESR let you set
`xpinstall.signatures.required = false` in `about:config` and install the
unsigned `.xpi` directly. Not available on regular release Firefox.

### Rebuilding the .xpi

After editing files in `browser-extension/firefox/`, repack:

```bash
cd browser-extension/firefox
zip -r ../mediarpc-browser-bridge.xpi manifest.json background.js netflix.js disneyplus.js tv2.js icons
```

## Plezy

Plex only exposes live sessions to the **server owner**, so MediaRPC reads playback directly from Plezy's mpv player instead. In **Plezy → Settings → mpv config**, add:

```
input-ipc-server=\\.\pipe\plezympv
```

Restart Plezy. MediaRPC then reads live position/pause from mpv and resolves title/episode/art from Plezy's local metadata cache + TMDB. No owner token required. Works whether Plezy is streaming from a Plex or an Emby backend (both shown as **Plezy**; toggle each via `PLEZY_PLEX_ENABLED` / `PLEZY_EMBY_ENABLED`).

## Configuration

All settings live in `.env` - see [`.env.example`](.env.example) for the full list. Highlights:

- **Sources**: `EMBY_ENABLED`, `PLEZY_ENABLED` (+ `PLEZY_PLEX_ENABLED` / `PLEZY_EMBY_ENABLED`), `BRIDGE_ENABLED` (+ `NETFLIX_ENABLED` / `DISNEY_ENABLED` / `TV2_ENABLED`).
- **Ratings**: `RATING_ORDER=emby,tmdb,omdb,critic` - reorder or drop sources.
- **Buttons**: `RPC_BUTTONS=letterboxd,serializd` - also supports `trakt`, `imdb`, `tmdb`, or `Label|https://url`.
- **Behaviour**: `UPDATE_INTERVAL`, `AUTO_PAUSE_WHEN_CLOSED`, `BROWSING_ENABLED`, `DEBUG`, plus advanced timing knobs.

---

## Credits

Made by **Kjerne**. Licensed under the [MIT License](LICENSE).
