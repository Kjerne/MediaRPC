const BRIDGE_URL = "http://127.0.0.1:5678/bridge";
const HEARTBEAT_MS = 5000;
const STALE_AFTER_MS = 60 * 60 * 1000;
// How long to wait before forwarding an active:false to Python.
// Prevents a single missed DOM poll from flickering the Discord presence off.
const INACTIVE_DEBOUNCE_MS = 12000;
// An owner tab that hasn't reported in this long can be replaced by any other tab.
const OWNER_STALE_MS = 15000;
// Position drift (seconds) from the extrapolated value that counts as a seek.
const SEEK_THRESHOLD_S = 5;

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

function post(payload) {
  const body = payload || { active: false };
  lastPosted = body;
  lastPostedAt = Date.now();
  fetch(BRIDGE_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body)
  }).catch(() => {
    // The tray app may not be running yet. The heartbeat sends again shortly.
  });
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
  if (!lastPosted || !lastPosted.active) return true;
  if (signature(payload) !== signature(lastPosted)) return true;
  if (!payload.paused) {
    const expected = Number(lastPosted.position || 0) + (Date.now() - lastPostedAt) / 1000;
    if (Math.abs(Number(payload.position || 0) - expected) > SEEK_THRESHOLD_S) return true;
  }
  return false;
}

function clearPresence() {
  if (inactiveTimer !== null) {
    clearTimeout(inactiveTimer);
    inactiveTimer = null;
  }
  ownerTabId = null;
  lastPayload = { active: false };
  post(lastPayload);
}

browser.runtime.onMessage.addListener((message, sender) => {
  if (!message || message.type !== "netflix-state") {
    return;
  }

  const payload = message.payload || { active: false };
  const tabId = sender && sender.tab ? sender.tab.id : null;
  const isOwner = ownerTabId === null || tabId === ownerTabId;

  if (payload.active) {
    if (!isOwner) {
      const ownerStale = Date.now() - lastContentUpdate > OWNER_STALE_MS;
      const ownerInactive = !lastPayload || !lastPayload.active;
      if (!ownerStale && !ownerInactive && rank(payload) <= rank(lastPayload)) {
        return; // another tab is showing something at least as relevant
      }
    }
    ownerTabId = tabId;
    // Cancel any pending inactive notification - we're still active.
    if (inactiveTimer !== null) {
      clearTimeout(inactiveTimer);
      inactiveTimer = null;
    }
    lastPayload = payload;
    lastContentUpdate = Date.now();
    if (shouldPost(payload)) {
      post(payload);
    }
    return;
  }

  // active:false from a tab that doesn't own the presence is irrelevant.
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
});

browser.tabs.onRemoved.addListener((tabId) => {
  if (tabId === ownerTabId && lastPayload && lastPayload.active) {
    clearPresence();
  }
});

setInterval(() => {
  if (!lastPayload || !lastPayload.active) {
    return;
  }

  if (Date.now() - lastContentUpdate > STALE_AFTER_MS) {
    lastPayload = null;
    ownerTabId = null;
    post({ active: false });
    return;
  }

  post(adjustedPayload(lastPayload));
}, HEARTBEAT_MS);
