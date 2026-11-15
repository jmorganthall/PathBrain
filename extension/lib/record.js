/*
 * PathBrain Field Recorder — the pure record-shaping logic, shared by the content script,
 * the service worker, the options page and the node tests. No browser APIs in here.
 *
 * Privacy rules live here so they are tested:
 *  - only a HOSTNAME is ever kept (never a path, query string, fragment, port or scheme);
 *  - nothing is recorded for a host that is not on the user's allow-list;
 *  - a record carries timings and counts only.
 */
(function (root) {
  "use strict";

  const RECORD_VERSION = 1;
  /** Records kept in chrome.storage.local; the oldest are dropped past this. */
  const MAX_RECORDS = 5000;
  /** A long task's blocking time is what it runs past this (the TBT convention). */
  const BLOCKING_THRESHOLD_MS = 50;
  /** CLS session windows: a gap longer than this, or a window longer than the cap, starts a new one. */
  const CLS_GAP_MS = 1000;
  const CLS_WINDOW_MS = 5000;
  /**
   * Long tasks count only inside the load window: the first this-many ms of the page. On a
   * single-page app (YouTube, Reddit, Facebook, Instagram) a tab can stay open for hours of
   * in-app navigation, and tasks from all of that are not part of the load being measured.
   */
  const LOAD_WINDOW_MS = 10000;
  /**
   * Josh's sites, recorded out of the box (each covers its subdomains). Editable on the
   * options page; the manifest grants exactly these at install.
   */
  const DEFAULT_SITES = Object.freeze(["facebook.com", "instagram.com", "news.google.com", "reddit.com",
    "slickdeals.net", "wikipedia.org", "youtube.com"]);

  const HOST_RE = /^(?=.{1,253}$)([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)(\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*$/;

  /** The lower-case hostname of a URL, or null. Nothing else of the URL survives. */
  function hostnameOf(url) {
    try {
      const u = new URL(String(url));
      if (u.protocol !== "http:" && u.protocol !== "https:") return null;
      const h = u.hostname.toLowerCase().replace(/\.$/, "");
      return HOST_RE.test(h) ? h : null;
    } catch (_e) {
      return null;
    }
  }

  /**
   * What the user typed on the options page, as an allow-list entry: "Example.com",
   * "https://www.example.com/path?q=1" and "example.com:8443" all become "example.com" /
   * "www.example.com". Returns null for anything that is not a plain host name.
   */
  function normalizeSite(input) {
    let s = String(input == null ? "" : input).trim().toLowerCase();
    if (!s) return null;
    if (!/^[a-z][a-z0-9+.-]*:\/\//.test(s)) s = "https://" + s;
    const h = hostnameOf(s);
    if (!h || !h.includes(".")) return h === "localhost" ? h : null;
    return h;
  }

  /** True when ``host`` is an allow-listed site or a subdomain of one. Empty list: never. */
  function isAllowed(host, sites) {
    if (!host || !Array.isArray(sites) || sites.length === 0) return false;
    return sites.some((site) => host === site || host.endsWith("." + site));
  }

  /** Match patterns for the content-script registration and the permission request. */
  function matchPatterns(sites) {
    const out = [];
    for (const site of sites || []) {
      out.push(`*://${site}/*`);
      if (site !== "localhost") out.push(`*://*.${site}/*`);
    }
    return out;
  }

  function roundMs(v) {
    return typeof v === "number" && isFinite(v) && v >= 0 ? Math.round(v * 10) / 10 : null;
  }

  /** CLS: the largest session window of layout shifts not caused by recent input. */
  function cumulativeLayoutShift(shifts) {
    let best = 0;
    let current = 0;
    let first = null;
    let last = null;
    for (const s of shifts || []) {
      if (!s || s.hadRecentInput || typeof s.value !== "number") continue;
      const t = s.startTime;
      if (first !== null && (t - last > CLS_GAP_MS || t - first > CLS_WINDOW_MS)) {
        current = 0;
        first = null;
      }
      if (first === null) first = t;
      current += s.value;
      last = t;
      if (current > best) best = current;
    }
    return Math.round(best * 10000) / 10000;
  }

  /**
   * Long tasks that start inside the load window: how many, their total duration, and the
   * total blocking time. A task without a startTime is counted (older callers, tests).
   */
  function longTaskSummary(tasks, windowMs) {
    const limit = typeof windowMs === "number" ? windowMs : LOAD_WINDOW_MS;
    let count = 0;
    let total = 0;
    let blocking = 0;
    for (const t of tasks || []) {
      if (!t || typeof t.duration !== "number") continue;
      if (typeof t.startTime === "number" && t.startTime > limit) continue;
      count += 1;
      total += t.duration;
      blocking += Math.max(0, t.duration - BLOCKING_THRESHOLD_MS);
    }
    return { count, total_ms: roundMs(total), blocking_ms: roundMs(blocking), window_ms: limit };
  }

  /**
   * One page load's record from what the observers collected. ``obs`` holds
   * ``{url, navigation, fcp, lcp, shifts, longTasks, hiddenBeforePaint, recordedAt}``;
   * only the hostname of ``url`` is kept. Returns null when the host is not allowed.
   */
  function buildRecord(obs, sites) {
    const host = hostnameOf(obs && obs.url);
    if (!isAllowed(host, sites)) return null;
    const nav = (obs && obs.navigation) || {};
    return {
      v: RECORD_VERSION,
      host,
      recorded_at: new Date(obs.recordedAt || Date.now()).toISOString(),
      nav_type: typeof nav.type === "string" ? nav.type : null,
      ttfb_ms: roundMs(nav.responseStart),
      fcp_ms: roundMs(obs.fcp),
      lcp_ms: roundMs(obs.lcp),
      cls: cumulativeLayoutShift(obs.shifts),
      long_tasks: longTaskSummary(obs.longTasks),
      hidden_before_paint: !!obs.hiddenBeforePaint,
    };
  }

  const KEYS = ["v", "host", "recorded_at", "nav_type", "ttfb_ms", "fcp_ms", "lcp_ms", "cls",
    "long_tasks", "hidden_before_paint"];

  /**
   * The service worker's gate before storing: the exact record shape, a hostname-only host
   * on the allow-list. Anything else (an extra field, a URL in ``host``) is refused.
   */
  function isValidRecord(rec, sites) {
    if (!rec || typeof rec !== "object") return false;
    const keys = Object.keys(rec).sort();
    if (keys.join(",") !== KEYS.slice().sort().join(",")) return false;
    if (rec.v !== RECORD_VERSION || typeof rec.host !== "string" || !HOST_RE.test(rec.host)) return false;
    return isAllowed(rec.host, sites);
  }

  /** Append, keeping at most ``max`` newest records. Pure: returns a new array. */
  function appendCapped(records, rec, max) {
    const out = (records || []).concat([rec]);
    const cap = max || MAX_RECORDS;
    return out.length > cap ? out.slice(out.length - cap) : out;
  }

  const api = {
    RECORD_VERSION, MAX_RECORDS, BLOCKING_THRESHOLD_MS, LOAD_WINDOW_MS, DEFAULT_SITES,
    hostnameOf, normalizeSite, isAllowed, matchPatterns,
    cumulativeLayoutShift, longTaskSummary, buildRecord, isValidRecord, appendCapped,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.PathBrainRecord = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
