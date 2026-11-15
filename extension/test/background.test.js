// node --test test/*.test.js  (no dependencies)
// Loads the real service worker in a sandbox with a fake chrome.* and fetch, to check the
// outbox: records queue, a 200 empties a batch, a failure keeps it, and the token goes only
// in the Authorization header to the configured address.
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const DIR = path.join(__dirname, "..");

function sandbox({ status = 200, granted = true } = {}) {
  const store = {};
  const listeners = {};
  const on = (name) => ({ addListener: (fn) => { listeners[name] = fn; } });
  const calls = [];
  const get = async (q) => {
    if (typeof q === "string") return q in store ? { [q]: store[q] } : {};
    const out = {};
    for (const [k, d] of Object.entries(q)) out[k] = k in store ? JSON.parse(JSON.stringify(store[k])) : d;
    return out;
  };
  const chrome = {
    runtime: { id: "me", onMessage: on("message"), onInstalled: on("installed"), onStartup: on("startup"), openOptionsPage() {} },
    storage: { local: { get, set: async (o) => Object.assign(store, JSON.parse(JSON.stringify(o))), remove: async () => {} }, onChanged: on("changed") },
    permissions: { contains: async () => granted, onAdded: on("padded"), onRemoved: on("premoved") },
    scripting: { unregisterContentScripts: async () => {}, registerContentScripts: async () => {} },
    alarms: { get: async () => null, create: () => {}, onAlarm: on("alarm") },
    action: { onClicked: on("action") },
  };
  const ctx = {
    chrome, URL, AbortController, setTimeout, clearTimeout, console: { log() { throw new Error("logged"); } },
    fetch: async (url, init) => { calls.push({ url, init }); if (status === 0) throw new Error("down"); return { status }; },
  };
  ctx.globalThis = ctx;
  ctx.importScripts = (...files) => { for (const f of files) vm.runInContext(fs.readFileSync(path.join(DIR, f), "utf8"), ctx); };
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(DIR, "background.js"), "utf8"), ctx);
  const send = (msg) => listeners.message(msg, { id: "me" });
  const settle = () => new Promise((r) => setTimeout(r, 30));
  return { store, calls, send, settle, listeners };
}

const rec = (host, i) => ({ v: 1, host, recorded_at: new Date(i * 1000).toISOString(), nav_type: "navigate",
  ttfb_ms: 1, fcp_ms: 2, lcp_ms: 3, cls: 0, long_tasks: { count: 0, total_ms: 0, blocking_ms: 0, window_ms: 10000 },
  hidden_before_paint: false });

test("records queue for upload; a 200 empties the outbox in batches", async () => {
  const s = sandbox({ status: 200 });
  s.store.sites = ["wikipedia.org"];
  for (let i = 0; i < 250; i++) s.send({ type: "pb-record", record: rec("en.wikipedia.org", i) });
  s.send({ type: "pb-record", record: rec("evil.example", 0) }); // not on the list: dropped
  await s.settle();
  assert.equal(s.store.records.length, 250);
  assert.equal(s.store.outbox.length, 250);
  s.store.upload = { server: "http://192.168.2.6:8000", token: "tok-abc" };
  s.send({ type: "pb-upload-now" });
  await s.settle();
  assert.equal(s.calls.length, 2);
  assert.equal(s.calls[0].url, "http://192.168.2.6:8000/api/field/records");
  assert.equal(s.calls[0].init.headers.Authorization, "Bearer tok-abc");
  assert.equal(JSON.parse(s.calls[0].init.body).records.length, 200);
  assert.ok(!s.calls[0].init.body.includes("tok-abc"));
  assert.equal(s.store.outbox.length, 0);
  assert.equal(s.store.records.length, 250, "local records stay");
  assert.equal(s.store.upload_status.sent_total, 250);
});

for (const status of [0, 401, 403, 404, 500]) {
  test(`a failed upload (${status || "network"}) keeps the batch for the next try`, async () => {
    const s = sandbox({ status });
    s.store.sites = ["reddit.com"];
    s.store.upload = { server: "http://pb.lan:8000", token: "tok-xyz" };
    for (let i = 0; i < 3; i++) s.send({ type: "pb-record", record: rec("www.reddit.com", i) });
    await s.settle();
    s.listeners.alarm({ name: "pathbrain-upload" });
    await s.settle();
    assert.equal(s.calls.length, 1);
    assert.equal(s.store.outbox.length, 3);
    assert.equal(s.store.upload_status.ok, false);
    assert.ok(!JSON.stringify(s.store.upload_status).includes("tok-xyz"));
  });
}

test("nothing is sent without an address, a token and access to that address", async () => {
  for (const [upload, granted] of [[null, true], [{ server: "http://pb.lan:8000", token: "" }, true],
    [{ server: "http://pb.lan:8000", token: "t" }, false]]) {
    const s = sandbox({ granted });
    s.store.sites = ["youtube.com"];
    if (upload) s.store.upload = upload;
    s.send({ type: "pb-record", record: rec("www.youtube.com", 1) });
    await s.settle();
    s.send({ type: "pb-upload-now" });
    await s.settle();
    assert.equal(s.calls.length, 0);
    assert.equal(s.store.outbox.length, 1);
  }
});
