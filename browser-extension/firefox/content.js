(function () {
  const UPDATE_MS = 1000;

  // Last known good title/subtitle - used when the DOM is hidden and
  // getBoundingClientRect() returns zero for all elements.
  let cachedTitle = "";
  let cachedSubtitle = "";

  function cleanTitle(value) {
    if (!value) {
      return "";
    }

    return value
      .replace(/\s+/g, " ")
      .replace(/([^\s])((?:S\d+)?E\d+)/i, "$1 $2")
      .replace(/((?:S\d+)?E\d+)([^\s\d])/i, "$1 $2")
      .replace(/([^\s])(Episode\s+\d+)/i, "$1 $2")
      .replace(/\s*\|\s*Netflix\s*$/i, "")
      .replace(/\s*-\s*Netflix\s*$/i, "")
      .replace(/^Watch\s+/i, "")
      .trim();
  }

  function isGenericTitle(value) {
    const normalized = cleanTitle(value).toLowerCase();
    return (
      !normalized ||
      normalized === "netflix" ||
      normalized === "watching netflix" ||
      normalized === "startside" ||
      normalized === "startside netflix" ||
      normalized === "startside - netflix" ||
      normalized === "startside - netflix"
    );
  }

  function textParts(selector) {
    const nodes = Array.from(document.querySelectorAll(selector));
    for (const node of nodes) {
      // When the document is hidden (tab backgrounded) all elements report
      // zero dimensions, so skip the visibility check in that case.
      if (!document.hidden) {
        const rect = node.getBoundingClientRect();
        if (!rect.width || !rect.height) {
          continue;
        }
      }

      const parts = [];
      const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT);
      while (walker.nextNode()) {
        const text = cleanTitle(walker.currentNode.textContent || "");
        if (text && !parts.includes(text)) {
          parts.push(text);
        }
      }

      if (parts.length) {
        return parts;
      }
    }
    return [];
  }

  function visibleText(selector) {
    return textParts(selector).join(" ");
  }

  function jsonLdTitle() {
    const scripts = Array.from(document.querySelectorAll("script[type='application/ld+json']"));
    for (const script of scripts) {
      try {
        const data = JSON.parse(script.textContent || "{}");
        const items = Array.isArray(data) ? data : [data];
        for (const item of items) {
          const name = cleanTitle(item?.name || item?.headline);
          if (!isGenericTitle(name)) {
            return name;
          }
        }
      } catch (_) {
        // Ignore unrelated structured data.
      }
    }
    return "";
  }

  function splitTitleParts(rawParts) {
    if (rawParts.length > 1) {
      return {
        title: rawParts[0],
        subtitle: rawParts.slice(1).join(" - ")
      };
    }

    const combined = cleanTitle(rawParts[0] || "");
    const match = combined.match(/^(.+?)\s+((?:S\d+)?E\d+)(?:\s+(.+))?$/i);
    if (match) {
      return {
        title: cleanTitle(match[1]),
        subtitle: cleanTitle([match[2], match[3]].filter(Boolean).join(" - "))
      };
    }

    return { title: combined, subtitle: "" };
  }

  function getTitleInfo() {
    const rawParts = textParts(
      "[data-uia='video-title'], [data-uia='title'], .video-title, .title-title"
    ).filter((part) => !isGenericTitle(part));
    const parsed = splitTitleParts(rawParts);
    const fallbackTitle = cleanTitle(
      jsonLdTitle() ||
      document.querySelector("meta[property='og:title']")?.content ||
      document.querySelector("meta[name='twitter:title']")?.content ||
      document.title
    );

    return {
      title: !isGenericTitle(parsed.title) ? parsed.title : (!isGenericTitle(fallbackTitle) ? fallbackTitle : ""),
      subtitle: parsed.subtitle
    };
  }

  function getSubtitle(title) {
    const text = visibleText(
      "[data-uia='video-subtitle'], .video-subtitle, .ellipsize-text, .titleCard-synopsis"
    );
    if (!text || text === title || text.length > 90) {
      return "";
    }
    return text;
  }

  // Netflix's player overlay only renders the episode number (e.g. "E21") and
  // never the season. The season lives in Netflix's internal player state, which
  // a Firefox content script can reach through window.wrappedJSObject (page world,
  // CSP-exempt). Resolve the current episode id to its {season, episode}.
  // Cache: resolving season only changes when the episode changes.
  let seCacheId = null;
  let seCacheVal = null;

  function getNetflixSeasonEpisode() {
    try {
      const w = window.wrappedJSObject;
      if (!w || !w.netflix) {
        return null;
      }
      const vp = w.netflix.appContext.state.playerApp.getState().videoPlayer;
      const vm = vp.videoMetadata;
      // Use the page's own Object/JSON so enumeration + serialization run in the
      // page compartment. A Firefox content script can read primitives across the
      // Xray membrane, but iterating page sub-objects (the episodes arrays) throws
      // "Permission denied to access object". Serializing page-side sidesteps it -
      // the resulting string crosses the membrane freely, then we parse it here.
      const key = w.Object.keys(vm)[0];
      if (!key) {
        return null;
      }
      const video = vm[key]._metadataObject.video;
      if (video.type !== "show") {
        return null;
      }
      const curId = video.currentEpisode;
      if (curId === seCacheId) {
        return seCacheVal;
      }
      const seasons = JSON.parse(w.JSON.stringify(video.seasons));
      let result = null;
      for (const s of seasons) {
        const idx = (s.episodes || []).findIndex((e) => e.id === curId);
        if (idx >= 0) {
          result = { season: s.seq, episode: idx + 1 };
          break;
        }
      }
      seCacheId = curId;
      seCacheVal = result;
      return result;
    } catch (_) {
      // Netflix internals moved or unavailable - fall back to the DOM subtitle.
    }
    return null;
  }

  // Prepend the real season to an episode-style subtitle. "E21 - Red and Itchy"
  // becomes "S5 E21 - Red and Itchy"; a bare "E21" becomes "S5 E21".
  function withSeason(subtitle) {
    const se = getNetflixSeasonEpisode();
    if (!se) {
      return subtitle;
    }
    const marker = `S${se.season} E${se.episode}`;
    const name = (subtitle || "").replace(/^\s*S?\d*\s*E\d+\s*-?\s*/i, "").trim();
    return name ? `${marker} - ${name}` : marker;
  }

  function send(payload) {
    try {
      browser.runtime.sendMessage({ type: "netflix-state", payload });
    } catch (_) {
      // The extension context may be unloading. Keep Netflix playback untouched.
    }
  }

  function tick() {
    if (location.pathname === "/browse" || location.pathname.startsWith("/browse/")) {
      cachedTitle = "";
      cachedSubtitle = "";
      send({
        active: true,
        service: "netflix",
        mode: "browsing",
        title: "Browsing Netflix",
        subtitle: "",
        paused: true,
        position: 0,
        duration: 0,
        focused: document.hasFocus(),
        visible: !document.hidden,
        url: location.href
      });
      return;
    }

    const video = document.querySelector("video");
    const info = getTitleInfo();

    // Keep the last good title so detection survives backgrounded tabs where
    // the DOM is hidden and title elements can't always be found.
    if (info.title) {
      cachedTitle = info.title;
      cachedSubtitle = info.subtitle || getSubtitle(info.title);
    }

    const title = info.title || cachedTitle;
    const rawSubtitle = info.subtitle || (info.title ? getSubtitle(title) : cachedSubtitle);
    const subtitle = withSeason(rawSubtitle);
    const active = Boolean(video && title && location.hostname.endsWith("netflix.com"));

    if (!active) {
      send({ active: false });
      return;
    }

    send({
      active: true,
      service: "netflix",
      title,
      subtitle,
      paused: video.paused,
      position: Number.isFinite(video.currentTime) ? video.currentTime : 0,
      duration: Number.isFinite(video.duration) ? video.duration : 0,
      focused: document.hasFocus(),
      visible: !document.hidden,
      url: location.href
    });
  }

  setInterval(tick, UPDATE_MS);
  window.addEventListener("focus", tick);
  window.addEventListener("blur", tick);
  document.addEventListener("visibilitychange", tick);
  // Clear presence when the tab closes or navigates away. pagehide is more
  // reliable than beforeunload for tab close (beforeunload often doesn't deliver
  // the async message in time), so send on both.
  const clearOnLeave = () => send({ active: false });
  window.addEventListener("pagehide", clearOnLeave);
  window.addEventListener("beforeunload", clearOnLeave);
  tick();
})();
