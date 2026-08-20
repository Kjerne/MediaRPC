(function () {
  const UPDATE_MS = 1000;

  // Last known good title/subtitle - used when the DOM is briefly hidden
  // (backgrounded tab) and elements report zero size / go missing.
  let cachedTitle = "";
  let cachedSubtitle = "";

  function cleanTitle(value) {
    if (!value) {
      return "";
    }
    return value
      .replace(/\s+/g, " ")
      .replace(/\s*\|\s*Disney\s*\+?\s*$/i, "")
      .replace(/\s*-\s*Disney\s*\+?\s*$/i, "")
      .replace(/^Watch\s+/i, "")
      .trim();
  }

  function isGenericTitle(value) {
    const normalized = cleanTitle(value).toLowerCase();
    return (
      !normalized ||
      normalized === "disney" ||
      normalized === "disney+" ||
      normalized === "disney plus" ||
      normalized === "watching disney+" ||
      normalized === "home"
    );
  }

  // Disney+ renders several <video> elements (a muted preview/placeholder plus
  // the real MSE player). The real one carries the title in aria-label and a
  // blob: src. Score candidates and pick the best; never assume the first.
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
      if (v.getAttribute("aria-label")) score += 4;
      if (src.startsWith("blob:")) score += 2;
      if (!v.paused) score += 1;
      if (v.currentTime > 0) score += 1;
      // Tie-break on progress so a live player beats an idle one.
      score += Math.min(1, v.currentTime / 1e6);
      if (score > bestScore) {
        bestScore = score;
        best = v;
      }
    }
    return best;
  }

  function metaTitle() {
    return (
      document.querySelector("meta[property='og:title']")?.content ||
      document.querySelector("meta[name='twitter:title']")?.content ||
      document.title ||
      ""
    );
  }

  function getTitle(video) {
    const aria = cleanTitle(video && video.getAttribute("aria-label"));
    if (!isGenericTitle(aria)) {
      return aria;
    }
    const fallback = cleanTitle(metaTitle());
    return isGenericTitle(fallback) ? "" : fallback;
  }

  // Disney renders the current episode as "S1:E5 The Iron Ceiling" in a <span>
  // inside an OPEN shadow root (the player's title overlay). It only exists
  // while the controls/title are visible, so the caller caches the last value.
  //
  // The overlay can show both the current episode and an "up next" episode; the
  // current one sits top-left, so pick the match highest on screen (min top).
  const EP_RE = /S\d+\s*:?\s*E\d+/i;

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

  // Turn Disney's "S1:E5 The Iron Ceiling" into "S1 E5 - The Iron Ceiling" so the
  // tray app's season/episode regex (expects `S<n> E<n>`) resolves the season.
  function normalizeEpisode(text) {
    const m = text.match(/S(\d+)\s*:?\s*E(\d+)\s*(.*)$/i);
    if (!m) {
      return text;
    }
    const marker = `S${m[1]} E${m[2]}`;
    const name = (m[3] || "").trim();
    return name ? `${marker} - ${name}` : marker;
  }

  function getSubtitle() {
    const all = [];
    collectDeep(document, all);
    let best = null;
    let bestTop = Infinity;
    for (const el of all) {
      if (el.children.length) {
        continue; // leaf nodes carry the text
      }
      const text = (el.textContent || "").trim();
      if (!text || text.length > 90 || !EP_RE.test(text)) {
        continue;
      }
      let top = 0;
      if (!document.hidden) {
        const rect = el.getBoundingClientRect();
        if (!rect.width || !rect.height) {
          continue; // not currently rendered
        }
        top = rect.top;
      }
      if (top < bestTop) {
        bestTop = top;
        best = text;
      }
    }
    return best ? normalizeEpisode(cleanTitle(best)) : "";
  }

  function send(payload) {
    try {
      // Reuse the Netflix bridge channel; the tray app branches on "service".
      browser.runtime.sendMessage({ type: "netflix-state", payload });
    } catch (_) {
      // Extension context may be unloading; leave playback untouched.
    }
  }

  function tick() {
    // Disney+ browsing is intentionally not reported - only active playback.
    const video = pickVideo();
    const title = video ? getTitle(video) : "";

    // "Playing" evidence without relying on duration (Disney reports null):
    // the picked video must be a real player (aria/blob/has advanced).
    const src = video ? (video.currentSrc || video.src || "") : "";
    const isRealPlayer = Boolean(
      video &&
      (video.getAttribute("aria-label") || src.startsWith("blob:") || video.currentTime > 0)
    );
    const active = Boolean(
      isRealPlayer && title && location.hostname.endsWith("disneyplus.com")
    );

    if (!active) {
      send({ active: false });
      return;
    }

    if (title && title !== cachedTitle) {
      // New title - drop any cached episode line from the previous show.
      cachedTitle = title;
      cachedSubtitle = "";
    }
    const found = getSubtitle();
    if (found) {
      cachedSubtitle = found;
    }
    const subtitle = found || cachedSubtitle;

    const dur = Number.isFinite(video.duration) && video.duration > 0 ? video.duration : 0;

    send({
      active: true,
      service: "disney",
      title: title || cachedTitle,
      subtitle,
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
