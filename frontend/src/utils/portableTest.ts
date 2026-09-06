// The portable (away) test runner: what a plain browser tab can measure on ANY device.
//
// A page can't load google.com and read its paint timing (same-origin policy), so this
// is a different instrument from the Chromium plugin: a synthetic resource waterfall of
// public CDN objects (Resource Timing gives every fetch's completion time on Safari,
// Chrome and Firefox; origins that send Timing-Allow-Origin also expose DNS/TCP/TLS/TTFB),
// one streamed download read chunk by chunk (byte-arrival cadence inside a transfer), and
// a burst of warm round trips. The recipe comes from the server (`/api/portable/recipe`),
// so the page and the server agree on what was measured (`instrument_version`).
//
// The output is RAW only — every metric is derived server-side (`interpret/portable.py`),
// exactly like the plugins, so a formula change re-derives history without re-measuring.

import type {
  PortableRaw,
  PortableRawEntry,
  PortableRawIteration,
  PortableRawResource,
  PortableRecipe,
  PortableRecipeResource,
} from "../api/types";

export interface PortableProgress {
  stage: string;
  fraction: number; // 0..1
}

export interface RunOptions {
  iterations?: number;
  signal?: AbortSignal;
  onProgress?: (p: PortableProgress) => void;
}

const DEVICE_KEY = "pathbrain.portable.device_id";

/** A stable per-browser id — the "same device" stamp the vs-home comparison keys on. */
export function deviceId(): string {
  try {
    const existing = localStorage.getItem(DEVICE_KEY);
    if (existing) return existing;
  } catch {
    /* storage unavailable: fall through to an ephemeral id */
  }
  const fresh =
    typeof crypto !== "undefined" && "randomUUID" in crypto
      ? crypto.randomUUID()
      : `dev-${Math.random().toString(36).slice(2)}${Date.now().toString(36)}`;
  try {
    localStorage.setItem(DEVICE_KEY, fresh);
  } catch {
    /* ignore */
  }
  return fresh;
}

/** Whatever the browser will say about itself — stored with the run for the reader. */
export function clientInfo(): Record<string, unknown> {
  const nav = navigator as Navigator & {
    userAgentData?: { platform?: string; mobile?: boolean; brands?: { brand: string; version: string }[] };
    connection?: { effectiveType?: string; downlink?: number; rtt?: number; saveData?: boolean; type?: string };
    deviceMemory?: number;
  };
  const conn = nav.connection;
  return {
    user_agent: nav.userAgent,
    platform: nav.userAgentData?.platform ?? nav.platform,
    mobile: nav.userAgentData?.mobile ?? /Mobi|Android|iPhone|iPad/i.test(nav.userAgent),
    brands: nav.userAgentData?.brands ?? null,
    language: nav.language,
    hardware_concurrency: nav.hardwareConcurrency ?? null,
    device_memory: nav.deviceMemory ?? null,
    screen: { width: screen.width, height: screen.height, dpr: window.devicePixelRatio },
    viewport: { width: window.innerWidth, height: window.innerHeight },
    connection: conn
      ? {
          effective_type: conn.effectiveType ?? null,
          downlink_mbps: conn.downlink ?? null,
          rtt_ms: conn.rtt ?? null,
          save_data: conn.saveData ?? null,
          type: conn.type ?? null,
        }
      : null,
    online: nav.onLine,
  };
}

const sleep = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

function throwIfAborted(signal?: AbortSignal) {
  if (signal?.aborted) throw new DOMException("cancelled", "AbortError");
}

function pickEntry(e: PerformanceResourceTiming): PortableRawEntry {
  return {
    startTime: e.startTime,
    fetchStart: e.fetchStart,
    domainLookupStart: e.domainLookupStart,
    domainLookupEnd: e.domainLookupEnd,
    connectStart: e.connectStart,
    secureConnectionStart: e.secureConnectionStart,
    connectEnd: e.connectEnd,
    requestStart: e.requestStart,
    responseStart: e.responseStart,
    responseEnd: e.responseEnd,
    transferSize: e.transferSize,
    encodedBodySize: e.encodedBodySize,
    nextHopProtocol: e.nextHopProtocol,
  };
}

/** The Resource Timing entry for a fetch we started at `tStart` (first one at/after it). */
function findEntry(url: string, tStart: number, tEnd: number | null): PortableRawEntry | null {
  const entries = performance
    .getEntriesByName(url, "resource")
    .filter((e): e is PerformanceResourceTiming => e.startTime >= tStart - 2 && (tEnd == null || e.startTime <= tEnd))
    .sort((a, b) => a.startTime - b.startTime);
  return entries.length ? pickEntry(entries[0]) : null;
}

async function fetchResource(res: PortableRecipeResource, signal?: AbortSignal): Promise<PortableRawResource> {
  const t_start = performance.now();
  try {
    const r = await fetch(res.url, {
      cache: "no-store",
      mode: res.mode === "no-cors" ? "no-cors" : "cors",
      credentials: "omit",
      redirect: "follow",
      signal,
    });
    if (r.type !== "opaque" && !r.ok) throw new Error(`HTTP ${r.status}`);
    // Drain the body so the entry's responseEnd is the whole object, not the headers.
    await r.arrayBuffer();
    const t_end = performance.now();
    return { id: res.id, url: res.url, bytes: res.bytes, ok: true, error: null, t_start, t_end, entry: null };
  } catch (e) {
    if (signal?.aborted) throw e;
    return {
      id: res.id,
      url: res.url,
      bytes: res.bytes,
      ok: false,
      error: e instanceof Error ? `${e.name}: ${e.message}` : String(e),
      t_start,
      t_end: null,
      entry: null,
    };
  }
}

/** Run the recipe's waterfall: roots start together, each dependent starts the moment its
 * parent completes (a real page's discovery chain). A failed parent fails its subtree. */
async function runWaterfall(recipe: PortableRecipe, signal?: AbortSignal): Promise<PortableRawResource[]> {
  const resources = recipe.resources;
  const ids = new Set(resources.map((r) => r.id));
  const out = new Map<string, PortableRawResource>();

  const failSubtree = (parentId: string, reason: string) => {
    for (const d of resources.filter((x) => x.after === parentId)) {
      if (out.has(d.id)) continue;
      out.set(d.id, {
        id: d.id, url: d.url, bytes: d.bytes, ok: false, error: reason,
        t_start: performance.now(), t_end: null, entry: null,
      });
      failSubtree(d.id, reason);
    }
  };

  const launch = async (res: PortableRecipeResource): Promise<void> => {
    const result = await fetchResource(res, signal);
    out.set(res.id, result);
    if (!result.ok) {
      failSubtree(res.id, `dependency ${res.id} failed`);
      return;
    }
    await Promise.all(resources.filter((x) => x.after === res.id).map(launch));
  };

  const roots = resources.filter((r) => r.after == null || !ids.has(r.after));
  await Promise.all(roots.map(launch));
  // Entries land on the timeline a task after the fetch resolves; give them a beat.
  await sleep(60);
  for (const r of out.values()) {
    if (r.ok) r.entry = findEntry(r.url, r.t_start, r.t_end);
  }
  return resources.map((r) => out.get(r.id)!).filter(Boolean);
}

async function runStream(recipe: PortableRecipe, signal?: AbortSignal): Promise<PortableRawIteration["stream"]> {
  const url = recipe.stream.url;
  const maxMs = (recipe.stream.max_seconds ?? 15) * 1000;
  const start = performance.now();
  const chunks: { t: number; bytes: number }[] = [];
  let bytes = 0;
  let partial = false;
  try {
    const r = await fetch(url, { cache: "no-store", mode: "cors", credentials: "omit", signal });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    if (!r.body) {
      const buf = await r.arrayBuffer();
      bytes = buf.byteLength;
      chunks.push({ t: performance.now() - start, bytes });
    } else {
      const reader = r.body.getReader();
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        const n = value?.byteLength ?? 0;
        bytes += n;
        chunks.push({ t: performance.now() - start, bytes: n });
        if (performance.now() - start > maxMs) {
          partial = true;
          try {
            await reader.cancel();
          } catch {
            /* ignore */
          }
          break;
        }
      }
    }
    return { url, ok: true, partial, start, end: performance.now(), bytes, chunks };
  } catch (e) {
    if (signal?.aborted) throw e;
    return {
      url, ok: false, partial: bytes > 0, start, end: bytes > 0 ? performance.now() : null, bytes, chunks,
      error: e instanceof Error ? `${e.name}: ${e.message}` : String(e),
    };
  }
}

async function runRtt(recipe: PortableRecipe, signal?: AbortSignal): Promise<PortableRawIteration["rtt"]> {
  const url = recipe.rtt.url;
  const n = Math.max(1, recipe.rtt.samples ?? 8);
  const samples_ms: number[] = [];
  for (let i = 0; i < n; i++) {
    throwIfAborted(signal);
    const t0 = performance.now();
    let t1: number | null = null;
    try {
      const r = await fetch(url, { cache: "no-store", mode: "cors", credentials: "omit", signal });
      await r.arrayBuffer();
      t1 = performance.now();
    } catch (e) {
      if (signal?.aborted) throw e;
      continue;
    }
    await sleep(40);
    // TTFB off a warm connection ≈ one round trip + server think; fall back to the
    // wall-clock fetch time where Timing-Allow-Origin isn't sent.
    const entry = findEntry(url, t0, t1);
    if (entry && entry.responseStart > 0 && entry.requestStart > 0) {
      samples_ms.push(entry.responseStart - entry.requestStart);
    } else if (t1 != null) {
      samples_ms.push(t1 - t0);
    }
    await sleep(80);
  }
  return { url, samples_ms };
}

export async function runPortableTest(recipe: PortableRecipe, opts: RunOptions = {}): Promise<PortableRaw> {
  const iterations = Math.max(1, opts.iterations ?? recipe.iterations ?? 2);
  const { signal, onProgress } = opts;
  const steps = 1 + iterations * 3;
  let step = 0;
  const report = (stage: string) => onProgress?.({ stage, fraction: Math.min(0.99, step / steps) });

  // Keep the screen on for the ~45 s the test takes (best-effort; Safari 16.4+ / Chrome).
  let lock: { release: () => Promise<void> } | null = null;
  try {
    const wl = (navigator as Navigator & { wakeLock?: { request: (t: "screen") => Promise<{ release: () => Promise<void> }> } }).wakeLock;
    lock = wl ? await wl.request("screen") : null;
  } catch {
    lock = null;
  }

  try {
    performance.setResourceTimingBufferSize(2000);
  } catch {
    /* ignore */
  }

  try {
    // Warm-up: wake an idle cellular radio and prime DNS for the page's own origin so the
    // first sample doesn't pay for the device being asleep.
    report("warming up");
    try {
      await fetch(recipe.rtt.url, { cache: "no-store", mode: "cors", credentials: "omit", signal });
    } catch {
      /* the real samples will report it */
    }
    step += 1;

    const out: PortableRawIteration[] = [];
    for (let i = 1; i <= iterations; i++) {
      throwIfAborted(signal);
      try {
        performance.clearResourceTimings();
      } catch {
        /* ignore */
      }
      report(`waterfall ${i}/${iterations}`);
      const resources = await runWaterfall(recipe, signal);
      step += 1;
      report(`stream ${i}/${iterations}`);
      const stream = await runStream(recipe, signal);
      step += 1;
      report(`round trips ${i}/${iterations}`);
      const rtt = await runRtt(recipe, signal);
      step += 1;
      out.push({ waterfall: { resources }, stream, rtt });
    }
    onProgress?.({ stage: "uploading", fraction: 0.99 });
    return { iterations: out };
  } finally {
    try {
      await lock?.release();
    } catch {
      /* ignore */
    }
  }
}

/** The device's public egress address, asked of the configured lookup service from the
 * browser itself — so it is the address the test's traffic actually leaves from, tunnel or
 * not. Null when the service is unreachable or answers with a non-address. */
export async function egressIp(lookupUrl: string, timeoutMs = 5000): Promise<string | null> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    const r = await fetch(lookupUrl, { cache: "no-store", mode: "cors", credentials: "omit", signal: ctrl.signal });
    if (!r.ok) return null;
    const text = (await r.text()).trim();
    let ip = text;
    try {
      const j = JSON.parse(text) as { ip?: unknown };
      if (typeof j.ip === "string") ip = j.ip.trim();
    } catch {
      /* plain-text service */
    }
    return /^[0-9a-fA-F:.]{3,45}$/.test(ip) ? ip : null;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}
