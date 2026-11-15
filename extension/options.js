/* PathBrain Field Recorder — options page: the allow-list and the local records. */
(function () {
  "use strict";
  const R = globalThis.PathBrainRecord;
  const U = globalThis.PathBrainUpload;
  const $ = (id) => document.getElementById(id);
  const say = (text) => { $("status").textContent = text; };

  async function render() {
    const { sites, records } = await chrome.storage.local.get({ sites: [], records: [] });
    const list = $("sites");
    list.replaceChildren();
    if (!sites.length) {
      const li = document.createElement("li");
      li.className = "empty";
      li.textContent = "No sites on the list, so nothing is recorded.";
      list.append(li);
    }
    for (const site of sites) {
      const li = document.createElement("li");
      const name = document.createElement("span");
      name.textContent = site;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "Remove";
      remove.addEventListener("click", () => removeSite(site));
      li.append(name, remove);
      list.append(li);
    }
    const { upload, upload_status, outbox } = await chrome.storage.local.get({ upload: null, upload_status: null, outbox: [] });
    if (upload && upload.server && document.activeElement !== $("server")) $("server").value = upload.server;
    const on = U.configured(upload);
    const parts = [on ? `Uploading to ${upload.server}.` : "Not uploading.", `${outbox.length} waiting to send.`];
    if (upload_status && upload_status.at) {
      parts.push(`Last try ${new Date(upload_status.at).toLocaleString()}: ${upload_status.message}`);
      if (upload_status.sent_total) parts.push(`${upload_status.sent_total} sent in all.`);
    }
    $("upstate").textContent = parts.join(" ");
    $("count").textContent = `${records.length} record${records.length === 1 ? "" : "s"} stored (newest ${R.MAX_RECORDS} kept).`;
  }

  async function addSite(raw) {
    const site = R.normalizeSite(raw);
    if (!site) { say("That isn't a site name. Try something like example.com."); return; }
    const { sites } = await chrome.storage.local.get({ sites: [] });
    if (sites.includes(site)) { say(`${site} is already on the list.`); return; }
    // Must run inside the click: Edge shows its own prompt for exactly these hosts.
    const ok = await chrome.permissions.request({ origins: R.matchPatterns([site]) });
    if (!ok) { say(`Not added: access to ${site} wasn't allowed.`); return; }
    await chrome.storage.local.set({ sites: sites.concat([site]).sort() });
    say(`Recording ${site}. Reload its open tabs to start.`);
  }

  async function removeSite(site) {
    const { sites } = await chrome.storage.local.get({ sites: [] });
    await chrome.storage.local.set({ sites: sites.filter((s) => s !== site) });
    try { await chrome.permissions.remove({ origins: R.matchPatterns([site]) }); } catch (_e) { /* still covered by another entry */ }
    say(`Stopped recording ${site}. Its earlier records are kept until you clear them.`);
  }

  $("add").addEventListener("submit", (e) => {
    e.preventDefault();
    addSite($("site").value).then(() => { $("site").value = ""; });
  });
  $("upload").addEventListener("submit", async (e) => {
    e.preventDefault();
    const server = U.normalizeServer($("server").value);
    const typed = $("token").value.trim();
    const { upload } = await chrome.storage.local.get({ upload: null });
    const token = typed || (upload && upload.token) || "";
    if (!server) { say("That isn't a PathBrain address. Try something like http://192.168.2.6:8000."); return; }
    if (!token) { say("Enter the token set on the PathBrain server."); return; }
    // Inside the click: Edge asks to allow this one address, which the upload needs.
    const ok = await chrome.permissions.request({ origins: [U.originPattern(server)] });
    if (!ok) { say(`Not saved: access to ${server} wasn't allowed.`); return; }
    await chrome.storage.local.set({ upload: { server, token } });
    $("token").value = "";
    say(`Saved. Uploading to ${server}.`);
    chrome.runtime.sendMessage({ type: "pb-upload-now" });
  });
  $("send").addEventListener("click", () => {
    chrome.runtime.sendMessage({ type: "pb-upload-now" });
    say("Sending in the background.");
  });
  $("stop").addEventListener("click", async () => {
    const { upload } = await chrome.storage.local.get({ upload: null });
    await chrome.storage.local.remove(["upload", "upload_status"]);
    if (upload && upload.server) {
      try { await chrome.permissions.remove({ origins: [U.originPattern(upload.server)] }); } catch (_e) { /* not granted */ }
    }
    say("Stopped uploading. The address and token are removed; records still wait in the outbox.");
  });
  $("defaults").addEventListener("click", async () => {
    // Inside the click, so Edge can prompt for any default whose access was removed.
    const ok = await chrome.permissions.request({ origins: R.matchPatterns(R.DEFAULT_SITES) });
    const { sites } = await chrome.storage.local.get({ sites: [] });
    const merged = Array.from(new Set(sites.concat(R.DEFAULT_SITES))).sort();
    await chrome.storage.local.set({ sites: merged });
    say(ok ? "Default sites restored." : "Defaults put back on the list, but Edge access wasn't allowed, so they won't record until it is.");
  });
  $("clear").addEventListener("click", async () => {
    if (!confirm("Delete every stored record? This can't be undone.")) return;
    await chrome.storage.local.set({ records: [] });
    say("Records cleared.");
  });
  $("export").addEventListener("click", async () => {
    const { records } = await chrome.storage.local.get({ records: [] });
    const blob = new Blob([JSON.stringify(records, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `pathbrain-field-records-${new Date().toISOString().slice(0, 10)}.json`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  });
  chrome.storage.onChanged.addListener((changes, area) => { if (area === "local") render(); });
  render();
})();
