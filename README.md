<p align="center">
  <img src="MediaRPC-Banner.png" alt="MediaRPC" width="100%" />
</p>

# MediaRPC

Discord Rich Presence for your media - **Emby**, **Netflix** (browser), and **Plex/Plezy** - from a single lightweight Windows tray app. Shows what you're watching with title, episode, live progress bar, poster art, ratings, and buttons.

- **Emby** - real-time via WebSocket, your session only.
- **Netflix** - a Firefox extension scrapes playback and forwards it locally.
- **Plex / Plezy** - reads live playback straight from Plezy's mpv player (works even when you are **not** the Plex server owner).

Priority when several are active: **Emby (playing) → Plex → Netflix → Emby (browsing) → idle**.

---

## Features

- `Watching <title>` header with episode `SxEy - Title`, live progress bar, play/pause.
- Poster art hosted on your **Zipline** instance (7-day expiry) or **ImgBB**, with a persistent cache so the same poster only uploads once per week.
- Ratings + posters enriched from **TMDB** (with ImgBB/Zipline fallback).
- Tray icon reflects play/paused state; auto-pause when the Emby app is closed.

---

## Requirements

- Windows, Python 3.10+
- A Discord application ID ([developer portal](https://discord.com/developers/applications))
- Emby server + API token (for the Emby source)
- Optional: TMDB & OMDB API keys, a Zipline instance or ImgBB key

## Setup

1. **Clone** and enter the folder.
2. **Configure**: copy `.env.example` → `.env` and fill it in.
3. **Build**: run `setup.bat` (installs deps + builds `dist\MediaRPC.exe`).
   - Quick rebuilds afterwards: `build.bat`.
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

## Netflix extension (Firefox)

The extension scrapes Netflix playback and posts it to the app at
`http://127.0.0.1:5678/netflix` (matches `NETFLIX_RPC_PORT`). If you change the
port, update `background.js` (`BRIDGE_URL`) and the manifest permission to match.

It is **not signed**, and release/beta Firefox will not install an unsigned
add-on permanently. Pick one of the options below.

### Option A - Temporary (quick test, resets on restart)

1. `about:debugging#/runtime/this-firefox` → **Load Temporary Add-on**.
2. Select `browser-extension/firefox/manifest.json`.

Works in any Firefox but is removed when you close the browser.

### Option B - Permanent (sign it yourself via Mozilla, free)

Mozilla signs add-ons for free, including private ones with no public listing:

1. Create an account at [addons.mozilla.org](https://addons.mozilla.org/developers/).
2. **Submit a New Add-on** → choose **On your own site** (unlisted / self-distribution).
3. Upload `browser-extension/mediarpc-netflix-bridge.xpi` (or zip the
   `browser-extension/firefox/` folder yourself).
4. Download the **signed `.xpi`** Mozilla returns.
5. Install it: Firefox → `about:addons` → gear ⚙ → **Install Add-on From File** → pick the signed `.xpi`.

This install survives restarts. (Prefer a public listing instead? Choose **On this site** in step 2 and people install straight from your add-on page.)

### Option C - Developer/ESR Firefox

Firefox Developer Edition, Nightly, or ESR let you set
`xpinstall.signatures.required = false` in `about:config` and install the
unsigned `.xpi` directly. Not available on regular release Firefox.

### Rebuilding the .xpi

After editing files in `browser-extension/firefox/`, repack:

```bash
cd browser-extension/firefox
zip -r ../mediarpc-netflix-bridge.xpi manifest.json background.js content.js icons
```

## Plex / Plezy

Plex only exposes live sessions to the **server owner**, so MediaRPC reads playback directly from Plezy's mpv player instead. In **Plezy → Settings → mpv config**, add:

```
input-ipc-server=\\.\pipe\plezympv
```

Restart Plezy. MediaRPC then reads live position/pause from mpv and resolves title/episode/art from Plezy's local metadata cache + TMDB. No owner token required.

## Configuration

All settings live in `.env` - see [`.env.example`](.env.example) for the full list (Emby, Discord, image host, ratings, Netflix bridge, Plex).

---

## Credits

Made by **Kjerne**. Licensed under the [MIT License](LICENSE).
