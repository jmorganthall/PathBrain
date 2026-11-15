/*
 * PathBrain Field Recorder — the pure upload logic (S34), shared by the service worker, the
 * options page and the node tests. No browser APIs in here.
 *
 * Records wait in an outbox in chrome.storage.local. The service worker posts them to
 * PathBrain in batches, from the background only, so a page never waits on an upload. A
 * batch leaves the outbox only once PathBrain has answered 200; anything else keeps it for
 * the next try.
 */
(function (root) {
  "use strict";

  /** Records per POST (the server takes up to 500). */
  const BATCH_SIZE = 200;
  /** Minutes between upload tries (chrome.alarms' floor is 1 minute in packed builds). */
  const UPLOAD_EVERY_MIN = 5;
  /** One POST gives up after this long; the batch stays for the next try. */
  const UPLOAD_TIMEOUT_MS = 15000;
  /** Outbox records kept while PathBrain is unreachable; the oldest go first past this. */
  const MAX_OUTBOX = 5000;
  const INGEST_PATH = "/api/field/records";

  /**
   * The PathBrain address typed on the options page, as an origin ("http://192.168.2.6:8000"),
   * or null. http or https only; any path, query or fragment typed is refused rather than
   * silently dropped, so the address saved is the address shown.
   */
  function normalizeServer(input) {
    let s = String(input == null ? "" : input).trim();
    if (!s) return null;
    if (!/^[a-z][a-z0-9+.-]*:\/\//i.test(s)) s = "http://" + s;
    let u;
    try {
      u = new URL(s);
    } catch (_e) {
      return null;
    }
    if (u.protocol !== "http:" && u.protocol !== "https:") return null;
    if (u.username || u.password || u.search || u.hash) return null;
    if (u.pathname !== "/" && u.pathname !== "") return null;
    if (!u.hostname) return null;
    return u.origin;
  }

  function ingestUrl(origin) {
    return origin + INGEST_PATH;
  }

  /** The host permission pattern the upload needs (requested on the options page). */
  function originPattern(origin) {
    return origin + "/*";
  }

  /** Is uploading set up? Both an address and a token. */
  function configured(upload) {
    return !!(upload && normalizeServer(upload.server) && typeof upload.token === "string" && upload.token.trim());
  }

  /** The next batch: the oldest ``size`` records. */
  function takeBatch(outbox, size) {
    return (outbox || []).slice(0, size || BATCH_SIZE);
  }

  /** The outbox after ``sent`` records from its front were accepted. Pure. */
  function afterAccepted(outbox, sent) {
    return (outbox || []).slice(sent);
  }

  /** Append, keeping at most ``max`` newest. Pure. */
  function enqueue(outbox, rec, max) {
    const out = (outbox || []).concat([rec]);
    const cap = max || MAX_OUTBOX;
    return out.length > cap ? out.slice(out.length - cap) : out;
  }

  /**
   * What an answer means for the outbox and for the status line. ``status`` is the HTTP
   * status, or 0 when the request failed (network, timeout). The token is never part of
   * the message.
   */
  function classify(status) {
    if (status === 200) return { ok: true, keep: false, message: "Uploaded." };
    if (status === 0) return { ok: false, keep: true, message: "PathBrain didn't answer; will try again." };
    if (status === 401 || status === 403) return { ok: false, keep: true, message: "PathBrain refused the token; check it on this page." };
    if (status === 404) return { ok: false, keep: true, message: "Ingest is off on PathBrain (no token set there), or the address is wrong." };
    if (status === 413 || status === 422) return { ok: false, keep: true, message: `PathBrain refused the batch (HTTP ${status}).` };
    return { ok: false, keep: true, message: `PathBrain answered HTTP ${status}; will try again.` };
  }

  /** The fetch options for one batch. */
  function requestInit(token, records) {
    return {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer " + String(token).trim() },
      body: JSON.stringify({ records }),
      credentials: "omit",
      cache: "no-store",
    };
  }

  const api = {
    BATCH_SIZE, UPLOAD_EVERY_MIN, UPLOAD_TIMEOUT_MS, MAX_OUTBOX, INGEST_PATH,
    normalizeServer, ingestUrl, originPattern, configured, takeBatch, afterAccepted, enqueue,
    classify, requestInit,
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  root.PathBrainUpload = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
