// node --test test/*.test.js  (no dependencies)
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const U = require("../lib/upload.js");

test("the PathBrain address is saved as a bare origin", () => {
  assert.equal(U.normalizeServer("192.168.2.6:8000"), "http://192.168.2.6:8000");
  assert.equal(U.normalizeServer(" http://pathbrain.lan:8000/ "), "http://pathbrain.lan:8000");
  assert.equal(U.normalizeServer("https://pb.example.com"), "https://pb.example.com");
  for (const bad of ["", "ftp://x.lan", "http://x.lan/api", "http://x.lan/?t=1", "http://u:p@x.lan", "http://", "not a url at all"]) {
    assert.equal(U.normalizeServer(bad), null, bad);
  }
  assert.equal(U.ingestUrl("http://192.168.2.6:8000"), "http://192.168.2.6:8000/api/field/records");
  assert.equal(U.originPattern("http://192.168.2.6:8000"), "http://192.168.2.6:8000/*");
});

test("uploading needs both an address and a token", () => {
  assert.equal(U.configured(undefined), false);
  assert.equal(U.configured({ server: "http://x.lan:8000", token: "" }), false);
  assert.equal(U.configured({ server: "", token: "t" }), false);
  assert.equal(U.configured({ server: "http://x.lan:8000", token: "  t  " }), true);
});

test("a batch leaves the outbox only once accepted, oldest first", () => {
  let box = [];
  for (let i = 0; i < 450; i++) box = U.enqueue(box, { i });
  const batch = U.takeBatch(box);
  assert.equal(batch.length, U.BATCH_SIZE);
  assert.deepEqual(batch[0], { i: 0 });
  box = U.enqueue(box, { i: 450 }); // a record arriving mid-upload stays queued
  box = U.afterAccepted(box, batch.length);
  assert.equal(box.length, 251);
  assert.deepEqual(box[0], { i: 200 });
  assert.deepEqual(box[box.length - 1], { i: 450 });
});

test("the outbox is capped, dropping the oldest", () => {
  let box = [];
  for (let i = 0; i < 8; i++) box = U.enqueue(box, { i }, 5);
  assert.deepEqual(box.map((r) => r.i), [3, 4, 5, 6, 7]);
});

test("only a 200 empties a batch; every failure keeps it for the next try", () => {
  assert.deepEqual([U.classify(200).ok, U.classify(200).keep], [true, false]);
  for (const s of [0, 401, 403, 404, 413, 422, 500, 503]) {
    const c = U.classify(s);
    assert.equal(c.ok, false, String(s));
    assert.equal(c.keep, true, String(s));
    assert.ok(c.message.length > 0);
  }
});

test("the request carries the token as a bearer header and no cookies", () => {
  const init = U.requestInit(" tok-123 ", [{ v: 1, host: "en.wikipedia.org" }]);
  assert.equal(init.method, "POST");
  assert.equal(init.headers.Authorization, "Bearer tok-123");
  assert.equal(init.credentials, "omit");
  assert.deepEqual(JSON.parse(init.body), { records: [{ v: 1, host: "en.wikipedia.org" }] });
  for (const s of [0, 401, 403, 404, 500]) assert.ok(!U.classify(s).message.includes("tok-123"));
});

test("the service worker never logs, and the content script never uploads", () => {
  const dir = path.join(__dirname, "..");
  const bg = fs.readFileSync(path.join(dir, "background.js"), "utf8");
  const content = fs.readFileSync(path.join(dir, "content.js"), "utf8");
  assert.ok(!/console\./.test(bg), "no console logging in the service worker");
  assert.ok(!/fetch\(|XMLHttpRequest|sendBeacon/.test(content), "pages never wait on an upload");
  assert.ok(/fetch\(/.test(bg));
});
