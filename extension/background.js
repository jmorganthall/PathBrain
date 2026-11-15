/*
 * PathBrain Field Recorder — service worker. The single writer of the record store, and
 * the keeper of the content-script registration: the recorder runs only on allow-listed
 * hosts the user has granted.
 */
importScripts("lib/record.js");
const R = globalThis.PathBrainRecord;
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

// One write at a time, so records from two tabs closing together are never lost.
let queue = Promise.resolve();
function store(record) {
  queue = queue.then(async () => {
    const { sites, records } = await chrome.storage.local.get({ sites: [], records: [] });
    if (!R.isValidRecord(record, sites)) return;
    await chrome.storage.local.set({ records: R.appendCapped(records, record, R.MAX_RECORDS) });
  }).catch(() => {});
  return queue;
}

chrome.runtime.onMessage.addListener((msg, sender) => {
  if (sender.id !== chrome.runtime.id || !msg || msg.type !== "pb-record") return;
  store(msg.record);
});
// First install: start from the default list. An update or reload keeps the user's edits
// (including an emptied list), because the key then already exists.
chrome.runtime.onInstalled.addListener(async () => {
  const have = await chrome.storage.local.get("sites");
  if (!Array.isArray(have.sites)) await chrome.storage.local.set({ sites: R.DEFAULT_SITES.slice() });
  await syncRegistration();
});
chrome.runtime.onStartup.addListener(syncRegistration);
chrome.permissions.onAdded.addListener(syncRegistration);
chrome.permissions.onRemoved.addListener(syncRegistration);
chrome.storage.onChanged.addListener((changes, area) => {
  if (area === "local" && changes.sites) syncRegistration();
});
chrome.action.onClicked.addListener(() => chrome.runtime.openOptionsPage());
