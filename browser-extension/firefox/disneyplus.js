(function () {
  const UPDATE_MS = 1000;

  // Last known good title/subtitle - used when the DOM is briefly hidden
  // (backgrounded tab) and elements report zero size / go missing.
  let cachedTitle = "";
  let cachedSubtitle = "";

  // Live progress (ESPN on Disney+). The <video> element has no usable duration
  // for live, but the player's timeline slider exposes the real program position
  // and length via aria-value* (pos = valuenow-valuemin, total = valuemax-valuemin).
  // The slider only exists while the controls overlay is visible, so cache the
  // last reading and extrapolate position from wall-clock time between reads.
  let cachedLiveDur = 0;
  let cachedLivePos = 0;
  let cachedLiveAt = 0;

  // The deep DOM walk (every element + open shadow roots) is the expensive part
  // of a tick. Once an episode line is cached, rescan only every SCAN_EVERY_MS;
  // a new title resets the cache and forces an immediate scan.
  const SCAN_EVERY_MS = 5000;
  let lastScanAt = 0;

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

  function getSubtitle(all) {
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

  // Read the live timeline slider (deep, through shadow roots). Returns program
  // position/length in seconds, or null when the slider isn't currently rendered.
  // The volume control is also role=slider, so require a progress-bar class and a
  // total larger than a volume range (0-100).
  function getLiveProgress(all) {
    for (const el of all) {
      if (!el.getAttribute || el.getAttribute("role") !== "slider") {
        continue;
      }
      const vmin = parseFloat(el.getAttribute("aria-valuemin"));
      const vnow = parseFloat(el.getAttribute("aria-valuenow"));
      const vmax = parseFloat(el.getAttribute("aria-valuemax"));
      if (![vmin, vnow, vmax].every(Number.isFinite)) {
        continue;
      }
      // Exclude the volume slider (0-100, valuetext "52 of 100"). The timeline
      // range is in seconds and reads like "1:05:44 of 2:29:51".
      const vtext = el.getAttribute("aria-valuetext") || "";
      const cls = (el.className || "").toString();
      if (!/\d+:\d\d/.test(vtext) && !cls.includes("progress-bar")) {
        continue;
      }
      const dur = vmax - vmin;
      const pos = vnow - vmin;
      if (dur > 60 && pos >= 0) {
        return { pos, dur };
      }
    }
    return null;
  }

  // The timeline slider is only mounted while the controls overlay is visible.
  // Nudge the player to reveal controls so getLiveProgress can read the program
  // total at least once; once cached we stop nudging and let them hide normally.
  function coaxControls(video) {
    try {
      const targets = [
        document,
        document.body,
        video,
        video.parentElement,
        video.parentElement && video.parentElement.parentElement
      ].filter(Boolean);
      const opts = {
        bubbles: true, cancelable: true, view: window,
        clientX: Math.round(window.innerWidth / 2),
        clientY: Math.round(window.innerHeight / 2)
      };
      for (const t of targets) {
        for (const type of ["mousemove", "mouseover", "pointermove"]) {
          t.dispatchEvent(new MouseEvent(type, opts));
        }
      }
    } catch (_) {
      // Best-effort only.
    }
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
      // New title - drop any cached episode line and live progress from the
      // previous show.
      cachedTitle = title;
      cachedSubtitle = "";
      cachedLiveDur = 0;
      cachedLivePos = 0;
      cachedLiveAt = 0;
      lastScanAt = 0;
    }

    const live = !Number.isFinite(video.duration) && video.currentTime > 0;
    const now = Date.now();
    const needScan = !cachedSubtitle || (live && cachedLiveDur === 0) ||
      now - lastScanAt >= SCAN_EVERY_MS;
    // One walk per scan, shared by the subtitle and live-slider lookups.
    let all = null;
    if (needScan) {
      all = [];
      collectDeep(document, all);
      lastScanAt = now;
      const found = getSubtitle(all);
      if (found) {
        cachedSubtitle = found;
      }
    }
    const subtitle = cachedSubtitle;

    let dur = Number.isFinite(video.duration) && video.duration > 0 ? video.duration : 0;
    let position = Number.isFinite(video.currentTime) ? video.currentTime : 0;

    // Live streams (ESPN on Disney+) report duration null/Infinity/NaN. The real
    // program position/length live on the timeline slider instead - read it so
    // Discord can draw a proper predicting bar. When the slider is hidden, reuse
    // the last reading and advance position by elapsed wall-clock (unless paused).
    if (live) {
      const lp = all ? getLiveProgress(all) : null;
      if (lp) {
        cachedLiveDur = lp.dur;
        cachedLivePos = lp.pos;
        cachedLiveAt = Date.now();
      } else if (cachedLiveDur === 0) {
        coaxControls(video); // reveal controls to read the total on a later tick
      }
      if (cachedLiveDur > 0) {
        const age = video.paused ? 0 : (Date.now() - cachedLiveAt) / 1000;
        dur = cachedLiveDur;
        position = Math.min(cachedLiveDur, cachedLivePos + age);
      } else {
        dur = 0; // no reading yet - fall back to elapsed count-up
      }
    }

    send({
      active: true,
      service: "disney",
      live,
      title: title || cachedTitle,
      subtitle,
      paused: video.paused,
      position,
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
