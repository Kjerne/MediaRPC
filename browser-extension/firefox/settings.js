// Shared settings for the background page and the toolbar popup.
// Stored in browser.storage.local under one key so a change fires a single
// storage.onChanged event; every reader merges over DEFAULTS so new fields added
// in later versions get sane values on existing installs.

const SETTINGS_KEY = "settings";

const SERVICES = [
  { id: "netflix", label: "Netflix" },
  { id: "disney", label: "Disney+" },
  { id: "tv2", label: "TV 2 Play" },
  { id: "silo", label: "Silo" }
];

const DEFAULT_SETTINGS = {
  paused: false,
  services: { netflix: true, disney: true, tv2: true, silo: true },
  showBrowsing: true,
  // Clear presence after the video has been paused this many minutes (0 = never).
  autoClearPausedMin: 0,
  // Must match BRIDGE_PORT in the MediaRPC .env.
  bridgePort: 5678,
  // Extra Silo hostnames (exact match). Hosts starting with "silo." always work.
  siloHosts: []
};

function mergeSettings(stored) {
  const s = stored || {};
  return {
    ...DEFAULT_SETTINGS,
    ...s,
    services: { ...DEFAULT_SETTINGS.services, ...(s.services || {}) },
    siloHosts: Array.isArray(s.siloHosts) ? s.siloHosts : []
  };
}

async function loadSettings() {
  try {
    const got = await browser.storage.local.get(SETTINGS_KEY);
    return mergeSettings(got[SETTINGS_KEY]);
  } catch (_) {
    return mergeSettings(null);
  }
}

async function saveSettings(settings) {
  await browser.storage.local.set({ [SETTINGS_KEY]: mergeSettings(settings) });
}

function bridgeBase(settings) {
  const port = Number(settings.bridgePort) || DEFAULT_SETTINGS.bridgePort;
  return `http://127.0.0.1:${port}`;
}
