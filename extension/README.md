# PathBrain Field Recorder (Microsoft Edge)

A small home-built browser extension that records how pages actually load in your own
browser, on sites you choose, so PathBrain can compare its lab runs with real browsing.
Edge runs Chrome Manifest V3 extensions, so this loads straight into Edge. It isn't in any
store and needs no build step.

## What it records

One record per **full page load**, on sites on the list only:

| Field | Meaning |
|---|---|
| `host` | The site's **hostname only** (e.g. `www.example.com`). Never the path, query string, fragment, port or scheme. |
| `ttfb_ms` | Time to first byte (navigation timing `responseStart`). |
| `fcp_ms` | First Contentful Paint. |
| `lcp_ms` | Largest Contentful Paint (final value before your first input or the tab going hidden). |
| `cls` | Cumulative Layout Shift (largest session window, ignoring shifts right after input). |
| `long_tasks` | Count, total duration and total blocking time (time past 50 ms) of long tasks that start in the first 10 s of the page (`window_ms`). |
| `nav_type` | `navigate`, `reload`, `back_forward` or `prerender`. |
| `hidden_before_paint` | The tab was hidden before first paint (a background-tab load). |
| `recorded_at`, `v` | When, and the record format version. |

Nothing else: no page content, titles, cookies or full addresses.

### Single-page apps (YouTube, Reddit, Facebook, Instagram)

These sites change "page" inside one document. TTFB, FCP and LCP fire only when the browser
loads a whole new document, so **only full loads are recorded**: opening the site, a reload,
or a link that loads a new document. Clicking from video to video on YouTube, or post to post on
Reddit, makes no new record. Long tasks are counted only in each page's first 10 seconds, so a
tab left open for hours doesn't pile its later work onto the load. CLS keeps the standard
definition (the worst burst of shifts over the page's life), which on these sites can include
shifts after in-app navigation. There's no honest, cheap way to give an in-app navigation its
own FCP/LCP, so this version doesn't try.

## Privacy defaults

- **Only sites on the list are recorded.** The list starts with seven sites, each covering its
  subdomains: `news.google.com`, `youtube.com`, `reddit.com`, `slickdeals.net`, `facebook.com`,
  `instagram.com` and `wikipedia.org`. Google News is `news.google.com` only, not the rest of
  Google (Search, Gmail and the like are not recorded).
- At install the extension is granted access to **those seven sites only**. Any site you add
  later gets its own Edge prompt for that site and its subdomains. Removing a site from the list
  stops recording it straight away; **Restore default sites** puts the seven back.
- Records stay in this browser (`chrome.storage.local`, newest 5,000 kept). This version
  uploads nothing. Sending records to PathBrain comes in a later version, and will be off
  until you set an address and token.

## Install in Edge (load unpacked)

1. Copy this `extension/` folder to the Windows desktop, e.g. `C:\PathBrain\extension`.
2. In Edge, go to `edge://extensions`.
3. Turn on **Developer mode** (toggle on the left side of the page, or at the bottom).
4. Click **Load unpacked** and pick the `extension` folder (the one containing `manifest.json`).
5. Click the extension's toolbar button (or **Details → Extension options**) to open its options page.
6. Check the list. The seven default sites are already on it; remove any you don't want, or add
   one (e.g. `example.com`) and click **Allow** in Edge's prompt.
7. Reload any open tabs on those sites. From then on each full page load on them is recorded.

If Edge's per-extension **Site access** is set to "On click", recording won't run; leave it at
"On specific sites" (the default).

To update after changing files, click **Reload** on the extension's card in `edge://extensions`.
Edge may show a "developer mode extensions" notice at startup; that is expected for unpacked
extensions.

## Checking it works

The options page shows how many records are stored. **Export JSON** downloads them, and
**Clear records** deletes them.

## Tests

The record-shaping and privacy logic (`lib/record.js`) has Node tests, with no dependencies:

```bash
cd extension && node --test test/*.test.js
```

The backend suite also runs them when `node` is on the path (`backend/tests/test_extension_js.py`).
