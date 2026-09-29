// Toolbar popup: edits settings in storage (the background page reacts via
// storage.onChanged) and shows live status fetched from the background page.

const REFRESH_MS = 2000;

const HIDDEN_REASONS = {
  "paused": "Hidden: presence paused",
  "service-off": "Hidden: service turned off",
  "browsing-off": "Hidden: browsing is off",
  "auto-cleared": "Hidden: paused too long"
};

let settings = mergeSettings(null);

const $ = (id) => document.getElementById(id);

function serviceLabel(id) {
  const s = SERVICES.find((x) => x.id === id);
  return s ? s.label : id;
}

function buildServiceRows() {
  const box = $("services");
  for (const svc of SERVICES) {
    const row = document.createElement("label");
    row.className = "row";
    const name = document.createElement("span");
    name.className = "row-title";
    name.textContent = svc.label;
    const input = document.createElement("input");
    input.type = "checkbox";
    input.className = "switch";
    input.id = `svc-${svc.id}`;
    input.addEventListener("change", () => {
      update({ services: { ...settings.services, [svc.id]: input.checked } });
    });
    row.append(name, input);
    box.append(row);
  }
}

function renderSettings() {
  $("enabled").checked = !settings.paused;
  for (const svc of SERVICES) {
    $(`svc-${svc.id}`).checked = settings.services[svc.id] !== false;
  }
  $("showBrowsing").checked = Boolean(settings.showBrowsing);
  $("autoClear").value = String(settings.autoClearPausedMin || 0);
  // Don't clobber a field the user is typing in.
  if (document.activeElement !== $("bridgePort")) {
    $("bridgePort").value = settings.bridgePort;
  }
  if (document.activeElement !== $("siloHosts")) {
    $("siloHosts").value = settings.siloHosts.join(", ");
  }
}

function renderStatus(status) {
  const pill = $("app-status");
  const text = $("app-status-text");
  pill.className = "pill";
  if (settings.paused) {
    pill.classList.add("paused");
    text.textContent = "Paused";
  } else if (status.app.reachable) {
    pill.classList.add("ok");
    text.textContent = "Connected";
  } else {
    pill.classList.add("bad");
    text.textContent = "App not running";
  }

  const cur = status.current;
  const title = $("now-title");
  const sub = $("now-sub");
  sub.className = "now-sub";
  if (!cur) {
    title.textContent = "Nothing playing";
    sub.textContent = "";
    return;
  }
  const svc = serviceLabel(cur.service);
  if (cur.mode === "browsing") {
    title.textContent = `Browsing ${svc}`;
    sub.textContent = "";
  } else {
    title.textContent = cur.title || svc;
    sub.textContent = [cur.subtitle, svc, cur.paused ? "Paused" : ""].filter(Boolean).join(" · ");
  }
  if (cur.hidden) {
    sub.className = "now-sub hidden-reason";
    sub.textContent = HIDDEN_REASONS[cur.hidden] || "Hidden";
  }
}

async function refresh() {
  try {
    const status = await browser.runtime.sendMessage({ type: "get-status" });
    if (status) {
      settings = mergeSettings(status.settings);
      renderSettings();
      renderStatus(status);
    }
  } catch (_) {
    // Background page restarting - next refresh will catch up.
  }
}

async function update(patch) {
  settings = mergeSettings({ ...settings, ...patch });
  renderSettings();
  await saveSettings(settings);
  refresh();
}

// "https://Media.Example.com:8443/watch" -> "media.example.com"
function normalizeHost(value) {
  let v = String(value || "").trim().toLowerCase();
  if (!v) return "";
  v = v.replace(/^[a-z]+:\/\//, "");
  v = v.split("/")[0];
  v = v.replace(/:\d+$/, "");
  return v;
}

function wireInputs() {
  $("enabled").addEventListener("change", (e) => update({ paused: !e.target.checked }));
  $("showBrowsing").addEventListener("change", (e) => update({ showBrowsing: e.target.checked }));
  $("autoClear").addEventListener("change", (e) => update({ autoClearPausedMin: Number(e.target.value) || 0 }));

  $("bridgePort").addEventListener("change", (e) => {
    const port = Math.trunc(Number(e.target.value));
    if (port >= 1 && port <= 65535) {
      update({ bridgePort: port });
    } else {
      e.target.value = settings.bridgePort;
    }
  });

  $("siloHosts").addEventListener("change", (e) => {
    const hosts = [...new Set(e.target.value.split(",").map(normalizeHost).filter(Boolean))];
    update({ siloHosts: hosts });
  });
}

buildServiceRows();
wireInputs();
$("version").textContent = `v${browser.runtime.getManifest().version}`;
loadSettings().then((s) => {
  settings = s;
  renderSettings();
});
refresh();
setInterval(refresh, REFRESH_MS);
