/* PathBrain Field Recorder — options page: the allow-list and the local records. */
(function () {
  "use strict";
  const R = globalThis.PathBrainRecord;
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
