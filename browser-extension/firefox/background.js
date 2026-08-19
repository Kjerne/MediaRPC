const BRIDGE_URL = "http://127.0.0.1:5678/netflix";
const HEARTBEAT_MS = 5000;
const STALE_AFTER_MS = 60 * 60 * 1000;
// How long to wait before forwarding an active:false to Python.
// Prevents a single missed DOM poll from flickering the Discord presence off.
const INACTIVE_DEBOUNCE_MS = 12000;

let lastPayload = null;
let lastContentUpdate = 0;
let inactiveTimer = null;

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
  fetch(BRIDGE_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload || { active: false })
  }).catch(() => {
    // The tray app may not be running yet. The content script sends again shortly.
  });
}

browser.runtime.onMessage.addListener((message) => {
  if (!message || message.type !== "netflix-state") {
    return;
  }

  const payload = message.payload || { active: false };

  if (payload.active) {
    // Cancel any pending inactive notification - we're still active.
    if (inactiveTimer !== null) {
      clearTimeout(inactiveTimer);
      inactiveTimer = null;
    }
    lastPayload = payload;
    lastContentUpdate = Date.now();
    post(lastPayload);
  } else {
    // Don't forward immediately. Wait INACTIVE_DEBOUNCE_MS to confirm it's not
    // a transient DOM flicker (title element momentarily missing, etc.).
    if (inactiveTimer === null) {
      inactiveTimer = setTimeout(() => {
        inactiveTimer = null;
        lastPayload = { active: false };
        post(lastPayload);
      }, INACTIVE_DEBOUNCE_MS);
    }
  }
});

browser.tabs.onRemoved.addListener(() => {
  if (inactiveTimer !== null) {
    clearTimeout(inactiveTimer);
    inactiveTimer = null;
  }
  if (lastPayload && lastPayload.active) {
    lastPayload = { active: false };
    post(lastPayload);
  }
});

setInterval(() => {
  if (!lastPayload || !lastPayload.active) {
    return;
  }

  if (Date.now() - lastContentUpdate > STALE_AFTER_MS) {
    lastPayload = null;
    post({ active: false });
    return;
  }

  post(adjustedPayload(lastPayload));
}, HEARTBEAT_MS);
