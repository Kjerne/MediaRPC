(async function () {
  // Silo is a *self-hosted* media server web UI. Its hostname is server-dependent
  // and usually starts with "silo." (silo.batsy.eu, silo.<whatever>). Hosts that
  // don't can be added in the extension popup (Advanced > Silo hosts).
  // Firefox match patterns can't wildcard a TLD, so this script is declared against
  // a broad match in the manifest and hard-gates on the hostname here. On any
  // non-silo host it installs nothing and never posts a message, so it can't race
  // the per-site scripts (netflix.js / disneyplus.js / tv2.js).
  const host = location.hostname.toLowerCase();
  if (!host.startsWith("silo.")) {
    let extra = [];
    try {
      const got = await browser.storage.local.get("settings");
      extra = ((got.settings || {}).siloHosts || []).map((h) => String(h).trim().toLowerCase());
    } catch (_) {
      // Storage unavailable - only the silo.* default applies.
    }
    if (!extra.includes(host)) {
      return;
    }
  }

  const UPDATE_MS = 1000;

  let cachedSeries = "";
  let cachedSubtitle = "";

  // getEpisodeInfo walks the whole DOM + shadow roots. The .sr-only label it
  // reads is always mounted, so cache the result per document.title and rescan
  // every 5 s (2 s while nothing found) instead of every tick.
  let cachedEp = null;
  let epDocTitle = null;
  let lastScanAt = 0;

  function episodeInfo() {
    const now = Date.now();
    if (document.title !== epDocTitle) {
      epDocTitle = document.title;
      cachedEp = null;
      lastScanAt = 0;
    }
    if (now - lastScanAt >= (cachedEp ? 5000 : 2000)) {
      lastScanAt = now;
      cachedEp = getEpisodeInfo();
    }
    return cachedEp;
  }

  // Silo's player renders one real MSE <video> (blob: src, real duration). Score
  // and pick it in case poster-preview/dummy videos are also on the page.
  function pickVideo() {
    const vids = Array.from(document.querySelectorAll("video"));
    if (!vids.length) {
      return null;
    }
    let best = null;
    let bestScore = -1;
    for (const v of vids) {
      const src = v.currentSrc || v.src || "";
      let score = 0;
      if (src.startsWith("blob:")) score += 3;
      if (Number.isFinite(v.duration) && v.duration > 0) score += 2;
      if (!v.paused) score += 1;
      if (v.currentTime > 0) score += 1;
      score += Math.min(1, v.currentTime / 1e6); // tie-break on progress
      if (score > bestScore) {
        bestScore = score;
        best = v;
      }
    }
    return best;
  }

  // Walk the DOM including open shadow roots.
  function collectDeep(root, out) {
    let nodes;
    try {
      nodes = root.querySelectorAll("*");
    } catch (_) {
      return;
    }
    for (const el of nodes) {
      if (el.shadowRoot) {
        collectDeep(el.shadowRoot, out);
      }
      out.push(el);
    }
  }

  // Episode form: "S2:E2 · Dogs to a Gunfight" (with title) or bare "S2:E2".
  const SE_TITLE_RE = /^S(\d+)\s*:\s*E(\d+)\s*[·:\-–—]\s*(.+)$/i;
  const SE_BARE_RE = /^S(\d+)\s*:\s*E(\d+)$/i;

  // Strip a trailing " (2015)" year off a series name (TMDB matches better without
  // it, and the Python side re-appends the year from TMDB).
  function stripYear(name) {
    return (name || "").replace(/\s*\(\d{4}\)\s*$/, "").trim();
  }

  // docTitle is "<title> · <servername>" (server name is arbitrary - "BatCave" for
  // silo.batsy.eu). Drop the last " · X" (or " | X") segment to get the raw title.
  function stripBrand(docTitle) {
    const s = (docTitle || "").replace(/\s+/g, " ").trim();
    return s.replace(/\s*[·|]\s*[^·|]*$/, "").trim() || s;
  }

  // Find the episode header. Silo mounts an .sr-only accessibility label
  // ("<series> (<year>)S#:E# · <episode>") that stays in the DOM regardless of
  // whether the on-screen controls are visible, so this is reliable even when the
  // player chrome is hidden. Returns {season, episode, epTitle, series} or null.
  function getEpisodeInfo() {
    const all = [];
    collectDeep(document, all);
    for (const el of all) {
      if (el.children.length) {
        continue;
      }
      const t = (el.textContent || "").trim();
      if (!t || t.length > 120) {
        continue;
      }
      const m = t.match(SE_TITLE_RE) || t.match(SE_BARE_RE);
      if (!m) {
        continue;
      }
      // Series = the parent wrapper's text with this S:E chunk removed.
      let series = "";
      const wrap = el.parentElement;
      if (wrap) {
        const wt = (wrap.textContent || "").replace(/\s+/g, " ").trim();
        if (wt.endsWith(t)) {
          series = wt.slice(0, wt.length - t.length).trim();
        }
      }
      return {
        season: m[1],
        episode: m[2],
        epTitle: (m[3] || "").trim(),
        series: stripYear(series),
      };
    }
    return null;
  }

  function isGenericTitle(value) {
    const n = (value || "").trim().toLowerCase();
    return !n || n === "silo" || n === "home" || n === "login" || n === "watch";
  }

  function send(payload) {
    try {
      browser.runtime.sendMessage({ type: "netflix-state", payload });
    } catch (_) {
      // Extension context may be unloading.
    }
  }

  function tick() {
    const video = pickVideo();
    const src = video ? (video.currentSrc || video.src || "") : "";
    const isRealPlayer = Boolean(
      video && (src.startsWith("blob:") || (Number.isFinite(video.duration) && video.duration > 0) || video.currentTime > 0)
    );

    if (!isRealPlayer) {
      send({ active: false });
      return;
    }

    const ep = episodeInfo();

    let title = "";
    let subtitle = "";
    if (ep && ep.series) {
      // Series episode: series is the main title, "S# E# - <episode>" the subtitle.
      title = ep.series;
      const marker = `S${ep.season} E${ep.episode}`;
      subtitle = ep.epTitle ? `${marker} - ${ep.epTitle}` : marker;
    } else {
      // Movie (no S:E header): document.title minus the " · <server>" brand tail.
      title = stripBrand(document.title);
    }

    const active = Boolean(isRealPlayer && title && !isGenericTitle(title));

    if (!active) {
      send({ active: false });
      return;
    }

    if (title !== cachedSeries) {
      cachedSeries = title;
      cachedSubtitle = "";
    }
    if (subtitle) {
      cachedSubtitle = subtitle;
    }
    const outSubtitle = subtitle || cachedSubtitle;

    const dur = Number.isFinite(video.duration) && video.duration > 0 ? video.duration : 0;

    send({
      active: true,
      service: "silo",
      title,
      subtitle: outSubtitle,
      paused: video.paused,
      position: Number.isFinite(video.currentTime) ? video.currentTime : 0,
      duration: dur,
      focused: document.hasFocus(),
      visible: !document.hidden,
      url: location.href
    });
  }

  setInterval(tick, UPDATE_MS);
  window.addEventListener("focus", tick);
  window.addEventListener("blur", tick);
  document.addEventListener("visibilitychange", tick);
  const clearOnLeave = () => send({ active: false });
  window.addEventListener("pagehide", clearOnLeave);
  window.addEventListener("beforeunload", clearOnLeave);
  tick();
})();
