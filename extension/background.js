/*
 * PathBrain Field Recorder — service worker. The single writer of the record store, and
 * the keeper of the content-script registration: the recorder runs only on allow-listed
 * hosts the user has granted.
 */
importScripts("lib/record.js", "lib/upload.js");
const R = globalThis.PathBrainRecord;
const U = globalThis.PathBrainUpload;
const SCRIPT_ID = "pathbrain-recorder";

async function granted(sites) {
  const out = [];
  for (const site of sites) {
    // eslint-disable-next-line no-await-in-loop
    if (await chrome.permissions.contains({ origins: R.matchPatterns([site]) })) out.push(site);
  }
  return out;
}

async function syncRegistration() {
  const { sites } = await chrome.storage.local.get({ sites: [] });
  const live = await granted(sites || []);
  try {
    await chrome.scripting.unregisterContentScripts({ ids: [SCRIPT_ID] });
  } catch (_e) { /* not registered yet */ }
  if (!live.length) return;
  await chrome.scripting.registerContentScripts([{
    id: SCRIPT_ID,
    matches: R.matchPatterns(live),
    js: ["lib/record.js", "content.js"],
    runAt: "document_start",
    allFrames: false,
    persistAcrossSessions: true,
  }]);
}

// One storage write at a time, so records from two tabs closing together, or a record landing
// while an upload trims the outbox, are never lost.
let queue = Promise.resolve();
function serial(fn) {
  const run = queue.then(fn);
  queue = run.catch(() => {});
  return run;
}

function store(record) {
  return serial(async () => {
    const { sites, records, outbox } = await chrome.storage.local.get({ sites: [], records: [], outbox: [] });
    if (!R.isValidRecord(record, sites)) return;
    await chrome.storage.local.set({
      records: R.appendCapped(records, record, R.MAX_RECORDS),
      outbox: U.enqueue(outbox, record, U.MAX_OUTBOX),
    });
  }).catch(() => {});
}

// ── Upload to PathBrain (S34) ─────────────────────────────────────────────────────────────
// From here only, never from a page: a page hands its record over and is done. A batch
// leaves the outbox only on a 200; anything else keeps it for the next alarm. The token is
// read from storage per try and never written anywhere else.
const ALARM = "pathbrain-upload";
let uploading = false;

async function setStatus(fields) {
  const { upload_status } = await chrome.storage.local.get({ upload_status: {} });
  await chrome.storage.local.set({ upload_status: Object.assign({}, upload_status, fields) });
}

async function postBatch(upload, batch) {
  const origin = U.normalizeServer(upload.server);
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), U.UPLOAD_TIMEOUT_MS);
  try {
    const res = await fetch(U.ingestUrl(origin), Object.assign(U.requestInit(upload.token, batch), { signal: ctl.signal }));
    return res.status;
  } catch (_e) {
    return 0;
  } finally {
    clearTimeout(timer);
  }
}

async function uploadNow() {
  if (uploading) return;
  uploading = true;
  try {
    const { upload, outbox } = await chrome.storage.local.get({ upload: null, outbox: [] });
    if (!U.configured(upload) || !outbox.length) return;
    const origin = U.normalizeServer(upload.server);
    if (!(await chrome.permissions.contains({ origins: [U.originPattern(origin)] }))) {
      await setStatus({ at: new Date().toISOString(), ok: false, message: "Access to the PathBrain address isn't allowed; save it again on this page." });
      return;
    }
    let pending = outbox.length;
    while (pending > 0) {
      // eslint-disable-next-line no-await-in-loop
      const { outbox: box } = await chrome.storage.local.get({ outbox: [] });
      const batch = U.takeBatch(box, U.BATCH_SIZE);
      if (!batch.length) break;
      // eslint-disable-next-line no-await-in-loop
      const status = await postBatch(upload, batch);
      const verdict = U.classify(status);
      if (!verdict.ok) {
        // eslint-disable-next-line no-await-in-loop
        await setStatus({ at: new Date().toISOString(), ok: false, http: status, message: verdict.message });
        return;
      }
      // Through the store queue, so a record arriving meanwhile is never lost.
      // eslint-disable-next-line no-await-in-loop
      await serial(async () => {
        const { outbox: now } = await chrome.storage.local.get({ outbox: [] });
        await chrome.storage.local.set({ outbox: U.afterAccepted(now, batch.length) });
      });
      pending -= batch.length;
      // eslint-disable-next-line no-await-in-loop
      const { upload_status } = await chrome.storage.local.get({ upload_status: {} });
      // eslint-disable-next-line no-await-in-loop
      await setStatus({ at: new Date().toISOString(), ok: true, http: 200, message: verdict.message,
        sent_total: (upload_status.sent_total || 0) + batch.length });
    }
  } finally {
    uploading = false;
  }
}

async function ensureAlarm() {
  const have = await chrome.alarms.get(ALARM);
  if (!have) chrome.alarms.create(ALARM, { periodInMinutes: U.UPLOAD_EVERY_MIN, delayInMinutes: 1 });
}

chrome.alarms.onAlarm.addListener((a) => { if (a.name === ALARM) uploadNow(); });

chrome.runtime.onMessage.addListener((msg, sender) => {
  if (sender.id !== chrome.runtime.id || !msg) return;
  if (msg.type === "pb-record") store(msg.record);
  else if (msg.type === "pb-upload-now") uploadNow();
});
chrome.runtime.onStartup.addListener(ensureAlarm);
// First install: start from the default list. An update or reload keeps the user's edits
// (including an emptied list), because the key then already exists.
chrome.runtime.onInstalled.addListener(async () => {
  const have = await chrome.storage.local.get("sites");
  if (!Array.isArray(have.sites)) await chrome.storage.local.set({ sites: R.DEFAULT_SITES.slice() });
  await syncRegistration();
  await ensureAlarm();
});
chrome.runtime.onStartup.addListener(syncRegistration);
chrome.permissions.onAdded.addListener(syncRegistration);
chrome.permissions.onRemoved.addListener(syncRegistration);
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.sites) syncRegistration();
});
chrome.action.onClicked.addListener(() => chrome.runtime.openOptionsPage());
