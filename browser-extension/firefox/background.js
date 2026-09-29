// settings.js is loaded first (see manifest background.scripts) and provides
// SETTINGS_KEY, SERVICES, mergeSettings, loadSettings, saveSettings, bridgeBase.

const HEARTBEAT_MS = 5000;
const STALE_AFTER_MS = 60 * 60 * 1000;
// How long to wait before forwarding an active:false to Python.
// Prevents a single missed DOM poll from flickering the Discord presence off.
const INACTIVE_DEBOUNCE_MS = 12000;
// An owner tab that hasn't reported in this long can be replaced by any other tab.
const OWNER_STALE_MS = 15000;
// Position drift (seconds) from the extrapolated value that counts as a seek.
const SEEK_THRESHOLD_S = 5;
const STATUS_TIMEOUT_MS = 1500;

let settings = mergeSettings(null);

let lastPayload = null;
let lastContentUpdate = 0;
let inactiveTimer = null;
// Tab whose playback currently owns the presence. Messages and tab closes from
// other tabs are ignored, so closing an unrelated tab (or a second Netflix tab
// sitting on /browse) no longer clears or flip-flops the presence.
let ownerTabId = null;
// What Python last received, for change detection (see shouldPost).
let lastPosted = null;
let lastPostedAt = 0;
// When the owner's video was first seen paused (for the auto-clear setting).
let pausedSince = null;
// Result of the last POST: null = unknown, true = app answered, false = unreachable.
let bridgeOk = null;

// ---------------------------------------------------------------------------
// Settings rules
// ---------------------------------------------------------------------------

function serviceOf(payload) {
  return (payload && payload.service) || "netflix";
}

// Why a payload must not be shown, or "" if it may. Static rules only; the
// paused-too-long rule depends on history and lives in autoClearExpired().
function blockReason(payload) {
  if (settings.paused) return "paused";
  if (settings.services[serviceOf(payload)] === false) return "service-off";
  if (payload.mode === "browsing" && !settings.showBrowsing) return "browsing-off";
  return "";
}

function autoClearExpired() {
  const mins = Number(settings.autoClearPausedMin) || 0;
  return mins > 0 && pausedSince !== null && Date.now() - pausedSince > mins * 60000;
}

function presenceShown() {
  return Boolean(lastPosted && lastPosted.active);
}

// ---------------------------------------------------------------------------
// Posting to the MediaRPC app
// ---------------------------------------------------------------------------

function adjustedPayload(payload) {
  if (!payload || !payload.active) {
    return payload || { active: false };
  }

  const adjusted = { ...payload };
  const ageSeconds = Math.min(60, Math.max(0, (Date.now() - lastContentUpdate) / 1000));
  if (adjusted.mode !== "browsing" && !adjusted.paused && adjusted.duration > 0) {
    adjusted.position = Math.min(adjusted.duration, Number(adjusted.position || 0) + ageSeconds);
  }
  adjusted.backgroundHeartbeat = true;
  return adjusted;
}

function setBridgeOk(ok) {
  if (bridgeOk !== ok) {
    bridgeOk = ok;
    updateBadge();
  }
}

function post(payload) {
  const body = payload || { active: false };
  lastPosted = body;
  lastPostedAt = Date.now();
  fetch(`${bridgeBase(settings)}/bridge`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  }).then((r) => setBridgeOk(r.ok)).catch(() => {
    // The tray app may not be running yet. The heartbeat sends again shortly.
    setBridgeOk(false);
  });
}

// Tell the app to drop the presence, but only if it is currently showing ours.
function clearIfShown() {
  if (presenceShown()) {
    post({ active: false });
  }
}

// Priority when two tabs report at once: real playback beats paused beats browsing.
function rank(payload) {
  if (!payload || !payload.active) return -1;
  if (payload.mode === "browsing") return 0;
  return payload.paused ? 1 : 2;
}

function signature(p) {
  return [p.service, p.mode, p.title, p.subtitle, p.paused, p.live, Math.round(p.duration || 0)].join("|");
}

// Content scripts report every second; only forward when something Discord
// shows actually changed, or the position jumped (seek). The 5 s heartbeat
// carries routine position updates and Python extrapolates in between.
function shouldPost(payload) {
  if (!presenceShown()) return true;
  if (signature(payload) !== signature(lastPosted)) return true;
  if (!payload.paused) {
    const expected = Number(lastPosted.position || 0) + (Date.now() - lastPostedAt) / 1000;
    if (Math.abs(Number(payload.position || 0) - expected) > SEEK_THRESHOLD_S) return true;
  }
  return false;
}

function releaseOwner() {
  if (inactiveTimer !== null) {
    clearTimeout(inactiveTimer);
    inactiveTimer = null;
  }
  ownerTabId = null;
  pausedSince = null;
  lastPayload = { active: false };
}

function clearPresence() {
  releaseOwner();
  post({ active: false });
}

// Re-evaluate the current payload against the rules (after a settings change
// or on the heartbeat): clear it if now blocked, re-send it if now allowed.
function reconcile() {
  if (!lastPayload || !lastPayload.active) {
    return;
  }
  if (blockReason(lastPayload) || autoClearExpired()) {
    clearIfShown();
  } else if (!presenceShown()) {
    post(adjustedPayload(lastPayload));
  }
}

// ---------------------------------------------------------------------------
// Toolbar badge
// ---------------------------------------------------------------------------

function updateBadge() {
  let text = "";
  let color = "#6b7280";
  let title = "MediaRPC";
  if (settings.paused) {
    text = "II";
    title = "MediaRPC - paused (Alt+Shift+P to resume)";
  } else if (bridgeOk === false) {
    text = "!";
    color = "#dc2626";
    title = "MediaRPC - app not reachable";
  }
  try {
    browser.browserAction.setBadgeText({ text });
    browser.browserAction.setBadgeBackgroundColor({ color });
    browser.browserAction.setTitle({ title });
  } catch (_) {
    // browserAction unavailable (e.g. Android) - badge is cosmetic.
  }
}

// ---------------------------------------------------------------------------
// Status for the popup
// ---------------------------------------------------------------------------

async function fetchAppStatus() {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), STATUS_TIMEOUT_MS);
  try {
    const r = await fetch(`${bridgeBase(settings)}/status`, { signal: ctrl.signal });
    setBridgeOk(r.ok);
    return r.ok ? { reachable: true } : { reachable: false, error: `HTTP ${r.status}` };
  } catch (_) {
    setBridgeOk(false);
    return { reachable: false, error: "not running" };
  } finally {
    clearTimeout(timer);
  }
}

async function getStatus() {
  const app = await fetchAppStatus();
  let current = null;
  if (lastPayload && lastPayload.active) {
    const reason = blockReason(lastPayload) || (autoClearExpired() ? "auto-cleared" : "");
    current = {
      service: serviceOf(lastPayload),
      mode: lastPayload.mode || "playing",
      title: lastPayload.title || "",
      subtitle: lastPayload.subtitle || "",
      paused: Boolean(lastPayload.paused),
      hidden: reason
    };
  }
  return { settings, app, current };
}

// ---------------------------------------------------------------------------
// Messages from content scripts + popup
// ---------------------------------------------------------------------------

function onState(payload, tabId) {
  if (payload.active) {
    // Blocked content never takes ownership; if the owner itself became
    // blocked (service switched off, browsing hidden) drop it and clear.
    if (blockReason(payload)) {
      if (tabId === ownerTabId) {
        releaseOwner();
        clearIfShown();
      }
      return;
    }

    const isOwner = ownerTabId === null || tabId === ownerTabId;
    if (!isOwner) {
      const ownerStale = Date.now() - lastContentUpdate > OWNER_STALE_MS;
      const ownerInactive = !lastPayload || !lastPayload.active;
      if (!ownerStale && !ownerInactive && rank(payload) <= rank(lastPayload)) {
        return; // another tab is showing something at least as relevant
      }
      pausedSince = null; // new owner: its pause clock starts fresh
    }
    ownerTabId = tabId;
    // Cancel any pending inactive notification - we're still active.
    if (inactiveTimer !== null) {
      clearTimeout(inactiveTimer);
      inactiveTimer = null;
    }

    if (payload.paused && payload.mode !== "browsing") {
      if (pausedSince === null) pausedSince = Date.now();
    } else {
      pausedSince = null;
    }

    lastPayload = payload;
    lastContentUpdate = Date.now();

    if (autoClearExpired()) {
      clearIfShown(); // keep ownership so resuming re-posts immediately
      return;
    }
    if (shouldPost(payload)) {
      post(payload);
    }
    return;
  }

  // active:false from a tab that doesn't own the presence is irrelevant.
  const isOwner = ownerTabId === null || tabId === ownerTabId;
  if (!isOwner || !lastPayload || !lastPayload.active) {
    return;
  }
  // Don't forward immediately. Wait INACTIVE_DEBOUNCE_MS to confirm it's not
  // a transient DOM flicker (title element momentarily missing, etc.).
  if (inactiveTimer === null) {
    inactiveTimer = setTimeout(() => {
      inactiveTimer = null;
      clearPresence();
    }, INACTIVE_DEBOUNCE_MS);
  }
}

browser.runtime.onMessage.addListener((message, sender) => {
  if (!message) {
    return;
  }
  if (message.type === "get-status") {
    return getStatus();
  }
  if (message.type === "netflix-state") {
    const tabId = sender && sender.tab ? sender.tab.id : null;
    onState(message.payload || { active: false }, tabId);
  }
});

browser.tabs.onRemoved.addListener((tabId) => {
  if (tabId === ownerTabId && lastPayload && lastPayload.active) {
    clearPresence();
  }
});

// ---------------------------------------------------------------------------
// Settings + shortcut
// ---------------------------------------------------------------------------

browser.storage.onChanged.addListener((changes, area) => {
  if (area !== "local" || !changes[SETTINGS_KEY]) {
    return;
  }
  const oldBase = bridgeBase(settings);
  settings = mergeSettings(changes[SETTINGS_KEY].newValue);
  if (bridgeBase(settings) !== oldBase) {
    // Port changed: the app on the new port hasn't seen anything yet.
    lastPosted = null;
    bridgeOk = null;
  }
  updateBadge();
  reconcile();
});

browser.commands.onCommand.addListener(async (command) => {
  if (command === "toggle-pause") {
    const current = await loadSettings();
    await saveSettings({ ...current, paused: !current.paused });
  }
});

loadSettings().then((loaded) => {
  settings = loaded;
  updateBadge();
});

// ---------------------------------------------------------------------------
// Heartbeat
// ---------------------------------------------------------------------------

setInterval(() => {
  if (!lastPayload || !lastPayload.active) {
    return;
  }

  if (Date.now() - lastContentUpdate > STALE_AFTER_MS) {
    releaseOwner();
    lastPayload = null;
    post({ active: false });
    return;
  }

  if (blockReason(lastPayload) || autoClearExpired()) {
    clearIfShown();
    return;
  }

  post(adjustedPayload(lastPayload));
}, HEARTBEAT_MS);
