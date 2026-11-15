/*
 * PathBrain Field Recorder — content script. Injected only on allow-listed sites (the
 * service worker registers it for exactly those hosts, after the user grants each one).
 * Observes the page's own performance entries, never its content, and sends ONE record
 * per page load to the service worker when the page is hidden or unloaded.
 *
 * Full page loads only. Single-page apps (YouTube, Reddit, Facebook, Instagram) change
 * route without a new document, and FCP/LCP/TTFB do not fire again for those, so an in-app
 * navigation produces no record of its own; long tasks are counted only in the load window.
 */
(function () {
  "use strict";
  const R = globalThis.PathBrainRecord;
  if (!R || window.top !== window || globalThis.__pathbrainRecorder) return;
  globalThis.__pathbrainRecorder = true;

  const obs = { fcp: null, lcp: null, shifts: [], longTasks: [], hiddenBeforePaint: document.visibilityState === "hidden" };
  let sent = false;

  function observe(type, onEntries) {
    try {
      const po = new PerformanceObserver((list) => onEntries(list.getEntries()));
      po.observe({ type, buffered: true });
      return po;
    } catch (_e) {
      return null; // entry type unsupported: that field stays null
    }
  }

  observe("paint", (entries) => {
    for (const e of entries) if (e.name === "first-contentful-paint") obs.fcp = e.startTime;
  });
  // LCP stops at the first input or the page going hidden (the web-vitals convention).
  let lcpFinal = false;
  observe("largest-contentful-paint", (entries) => {
    if (lcpFinal) return;
    const last = entries[entries.length - 1];
    if (last) obs.lcp = last.renderTime || last.loadTime || last.startTime;
  });
  ["keydown", "pointerdown"].forEach((t) => addEventListener(t, () => { lcpFinal = true; }, { once: true, capture: true, passive: true }));
  observe("layout-shift", (entries) => {
    for (const e of entries) obs.shifts.push({ value: e.value, startTime: e.startTime, hadRecentInput: e.hadRecentInput });
  });
  observe("longtask", (entries) => {
    for (const e of entries) obs.longTasks.push({ duration: e.duration, startTime: e.startTime });
  });

  function send() {
    if (sent) return;
    sent = true;
    lcpFinal = true;
    const nav = performance.getEntriesByType("navigation")[0];
    const payload = {
      url: location.href, // reduced to its hostname by buildRecord, below, before it leaves this page
      navigation: nav ? { type: nav.type, responseStart: nav.responseStart } : null,
      fcp: obs.fcp, lcp: obs.lcp, shifts: obs.shifts, longTasks: obs.longTasks,
      hiddenBeforePaint: obs.hiddenBeforePaint, recordedAt: Date.now(),
    };
    chrome.storage.local.get({ sites: [] }, ({ sites }) => {
      const record = R.buildRecord(payload, sites);
      if (!record) return;
      try {
        chrome.runtime.sendMessage({ type: "pb-record", record });
      } catch (_e) { /* extension reloaded: drop this one */ }
    });
  }

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") {
      if (obs.fcp === null) obs.hiddenBeforePaint = true;
      send();
    }
  }, { capture: true });
  addEventListener("pagehide", send, { capture: true });
})();
