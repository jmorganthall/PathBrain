// node --test test/*.test.js  (no dependencies)
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const R = require("../lib/record.js");

const SITES = ["example.com"];

test("only the hostname of a URL is kept", () => {
  assert.equal(R.hostnameOf("https://WWW.Example.com:8443/a/b?q=secret#frag"), "www.example.com");
  assert.equal(R.hostnameOf("file:///C:/x.html"), null);
  assert.equal(R.hostnameOf("not a url"), null);
});

test("the options page normalises what is typed to a bare host", () => {
  assert.equal(R.normalizeSite(" Example.COM "), "example.com");
  assert.equal(R.normalizeSite("https://www.example.com/path?q=1"), "www.example.com");
  assert.equal(R.normalizeSite("example.com:8443"), "example.com");
  assert.equal(R.normalizeSite("localhost"), "localhost");
  for (const bad of ["", "   ", "not a site", "ftp://example.com", "intranet"]) {
    assert.equal(R.normalizeSite(bad), null, bad);
  }
});

test("nothing is allowed until a site is added; a site covers its subdomains only", () => {
  assert.equal(R.isAllowed("example.com", []), false);
  assert.equal(R.isAllowed("example.com", undefined), false);
  assert.equal(R.isAllowed("example.com", SITES), true);
  assert.equal(R.isAllowed("news.example.com", SITES), true);
  assert.equal(R.isAllowed("badexample.com", SITES), false);
  assert.equal(R.isAllowed("example.com.evil.net", SITES), false);
});

test("match patterns cover the host and its subdomains", () => {
  assert.deepEqual(R.matchPatterns(["example.com"]), ["*://example.com/*", "*://*.example.com/*"]);
  assert.deepEqual(R.matchPatterns(["localhost"]), ["*://localhost/*"]);
});

test("CLS is the largest session window, ignoring shifts after input", () => {
  const shifts = [
    { value: 0.05, startTime: 100 }, { value: 0.05, startTime: 600 },  // window 1: 0.10
    { value: 0.30, startTime: 3000, hadRecentInput: true },              // ignored
    { value: 0.08, startTime: 3200 }, { value: 0.07, startTime: 3900 },  // window 2: 0.15
  ];
  assert.equal(R.cumulativeLayoutShift(shifts), 0.15);
  assert.equal(R.cumulativeLayoutShift([]), 0);
});

test("long tasks: count, total and blocking time past 50 ms", () => {
  assert.deepEqual(R.longTaskSummary([{ duration: 120 }, { duration: 60 }, { duration: 50 }]),
    { count: 3, total_ms: 230, blocking_ms: 80, window_ms: R.LOAD_WINDOW_MS });
});

test("a record holds the hostname and timings only, and only for an allowed site", () => {
  const obs = {
    url: "https://www.example.com/account?id=12345&token=abc#x",
    navigation: { type: "navigate", responseStart: 182.44 },
    fcp: 410.06, lcp: 905.5, shifts: [{ value: 0.02, startTime: 500 }],
    longTasks: [{ duration: 75 }], hiddenBeforePaint: false, recordedAt: Date.UTC(2026, 10, 15, 12),
  };
  const rec = R.buildRecord(obs, SITES);
  assert.deepEqual(rec, {
    v: 1, host: "www.example.com", recorded_at: "2026-11-15T12:00:00.000Z", nav_type: "navigate",
    ttfb_ms: 182.4, fcp_ms: 410.1, lcp_ms: 905.5, cls: 0.02,
    long_tasks: { count: 1, total_ms: 75, blocking_ms: 25, window_ms: R.LOAD_WINDOW_MS }, hidden_before_paint: false,
  });
  const text = JSON.stringify(rec);
  for (const leak of ["account", "12345", "token", "abc", "https"]) assert.ok(!text.includes(leak), leak);
  assert.equal(R.buildRecord(obs, []), null);
  assert.equal(R.buildRecord({ ...obs, url: "https://other.net/" }, SITES), null);
});

test("missing timings stay null rather than being invented", () => {
  const rec = R.buildRecord({ url: "https://example.com/", recordedAt: 0 }, SITES);
  assert.equal(rec.ttfb_ms, null);
  assert.equal(rec.fcp_ms, null);
  assert.equal(rec.lcp_ms, null);
  assert.equal(rec.nav_type, null);
});

test("the service worker refuses anything but the exact record shape", () => {
  const rec = R.buildRecord({ url: "https://example.com/", recordedAt: 0 }, SITES);
  assert.equal(R.isValidRecord(rec, SITES), true);
  assert.equal(R.isValidRecord(rec, []), false);
  assert.equal(R.isValidRecord({ ...rec, url: "https://example.com/x" }, SITES), false);
  assert.equal(R.isValidRecord({ ...rec, host: "https://example.com/x" }, SITES), false);
  assert.equal(R.isValidRecord(null, SITES), false);
});

test("the store keeps only the newest records past the cap", () => {
  let rs = [];
  for (let i = 0; i < 7; i += 1) rs = R.appendCapped(rs, { i }, 5);
  assert.deepEqual(rs.map((r) => r.i), [2, 3, 4, 5, 6]);
});

test("the manifest grants exactly the default sites up front, nothing wider", () => {
  const m = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "manifest.json"), "utf8"));
  assert.equal(m.manifest_version, 3);
  assert.deepEqual(m.permissions.slice().sort(), ["scripting", "storage"]);
  assert.deepEqual(m.host_permissions.slice().sort(), R.matchPatterns(R.DEFAULT_SITES).sort());
  assert.equal(m.content_scripts, undefined, "the recorder is registered per granted site, not declared for all");
});

test("the default list is Josh's seven sites, each covering its subdomains", () => {
  assert.deepEqual(R.DEFAULT_SITES.slice().sort(), ["facebook.com", "instagram.com", "news.google.com",
    "reddit.com", "slickdeals.net", "wikipedia.org", "youtube.com"]);
  for (const s of R.DEFAULT_SITES) assert.equal(R.normalizeSite(s), s);
  const D = R.DEFAULT_SITES;
  assert.equal(R.isAllowed("www.youtube.com", D), true);
  assert.equal(R.isAllowed("old.reddit.com", D), true);
  assert.equal(R.isAllowed("en.wikipedia.org", D), true);
  assert.equal(R.isAllowed("m.facebook.com", D), true);
  assert.equal(R.isAllowed("news.google.com", D), true);
  assert.equal(R.isAllowed("www.google.com", D), false, "Google News only, not all of Google");
  assert.equal(R.isAllowed("mail.google.com", D), false);
  assert.equal(R.isAllowed("youtube.com.evil.net", D), false);
  const rec = R.buildRecord({ url: "https://www.youtube.com/watch?v=abc123&t=42s", navigation: { type: "navigate", responseStart: 80 } }, D);
  assert.equal(rec.host, "www.youtube.com");
  assert.ok(!JSON.stringify(rec).includes("abc123"));
});

test("long tasks count only inside the load window (SPA tabs stay open for hours)", () => {
  const s = R.longTaskSummary([
    { duration: 120, startTime: 900 },
    { duration: 80, startTime: R.LOAD_WINDOW_MS - 1 },
    { duration: 400, startTime: R.LOAD_WINDOW_MS + 1 },
    { duration: 300, startTime: 3600000 },
  ]);
  assert.deepEqual(s, { count: 2, total_ms: 200, blocking_ms: 100, window_ms: R.LOAD_WINDOW_MS });
});
