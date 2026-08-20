(function () {
  const UPDATE_MS = 1000;

  let cachedTitle = "";
  let cachedSubtitle = "";

  function cleanTitle(value) {
    if (!value) {
      return "";
    }
    return value
      .replace(/\s+/g, " ")
      .replace(/\s*[|\-]\s*TV\s*2\s*Play\s*$/i, "")
      .replace(/\s*[|\-]\s*TV\s*2\s*$/i, "")
      .trim();
  }

  function isGenericTitle(value) {
    const n = cleanTitle(value).toLowerCase();
    return !n || n === "tv 2 play" || n === "tv2 play" || n === "tv 2" || n === "play";
  }

  // TV2 renders several <video> elements (dummies + the real MSE player). The
  // real one has a blob: src and a duration. Score and pick the best.
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

  // The player title overlay renders "S1:E1" in a <span> inside an open shadow
  // root, but only while controls are visible; the caller caches it.
  const SE_RE = /S(\d+)\s*:?\s*E(\d+)/i;

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

  // Returns {season, episode} from the shadow overlay, or null.
  function getSeasonEpisode() {
    const all = [];
    collectDeep(document, all);
    let best = null;
    let bestTop = Infinity;
    for (const el of all) {
      if (el.children.length) {
        continue;
      }
      const text = (el.textContent || "").trim();
      if (!text || text.length > 40) {
        continue;
      }
      const m = text.match(SE_RE);
      if (!m) {
        continue;
      }
      let top = 0;
      if (!document.hidden) {
        const rect = el.getBoundingClientRect();
        if (!rect.width || !rect.height) {
          continue;
        }
        top = rect.top;
      }
      if (top < bestTop) {
        bestTop = top;
        best = { season: m[1], episode: m[2] };
      }
    }
    return best;
  }

  // Split "Klovn: 5-årsdagen" -> { series: "Klovn", episode: "5-årsdagen" }.
  function splitDocTitle(raw) {
    const clean = cleanTitle(raw);
    const idx = clean.indexOf(":");
    if (idx > 0) {
      return { series: clean.slice(0, idx).trim(), episode: clean.slice(idx + 1).trim() };
    }
    return { series: clean, episode: "" };
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

    const se = getSeasonEpisode();
    const parts = splitDocTitle(document.title);

    // With a season/episode marker this is a series episode: series is the title,
    // the doc-title tail is the episode name. Without it, treat as a movie.
    let title = "";
    let subtitle = "";
    if (se) {
      title = parts.series;
      const marker = `S${se.season} E${se.episode}`;
      subtitle = parts.episode ? `${marker} - ${parts.episode}` : marker;
    } else {
      title = cleanTitle(document.title);
    }

    const active = Boolean(
      isRealPlayer && title && !isGenericTitle(title) && location.hostname.endsWith("play.tv2.dk")
    );

    if (!active) {
      send({ active: false });
      return;
    }

    if (title !== cachedTitle) {
      cachedTitle = title;
      cachedSubtitle = "";
    }
    if (subtitle) {
      cachedSubtitle = subtitle;
    }
    const outSubtitle = subtitle || cachedSubtitle;

    const dur = Number.isFinite(video.duration) && video.duration > 0 ? video.duration : 0;

    send({
      active: true,
      service: "tv2",
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
