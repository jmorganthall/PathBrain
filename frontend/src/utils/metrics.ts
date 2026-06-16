// Human-friendly metadata for the raw plugin metrics shown on a run.
//
// Each entry gives a short label, a plain-English description (for hover/tap
// tooltips), the unit, and whether *higher* is better. Almost every PathBrain
// metric is lower-is-better (latency, handshake, render time…); the one
// exception is transfer speed. `higherIsBetter` drives the improved/worse arrow
// so the direction is correct even for the inverted metric.

export interface MetricMeta {
  label: string;
  description: string;
  unit?: string;
  higherIsBetter?: boolean;
}

export const METRIC_META: Record<string, MetricMeta> = {
  // ICMP
  latency_ms: {
    label: "Latency (ping)",
    unit: "ms",
    description:
      "Round-trip time for a ping to reach your targets and come back. The base delay behind everything you do online — lower feels snappier.",
  },
  jitter_ms: {
    label: "Jitter",
    unit: "ms",
    description:
      "How much latency wobbles between pings. High jitter makes calls, video and games feel choppy even when the average ping looks fine. Lower is better.",
  },
  packet_loss_pct: {
    label: "Packet loss",
    unit: "%",
    description:
      "Share of ping packets that never came back. Even ~1% causes stutters, retransmits and dropouts. Lower is better.",
  },
  // DNS
  lookup_ms: {
    label: "DNS lookup",
    unit: "ms",
    description:
      "Time to translate a hostname (e.g. example.com) into an IP address. This happens before a page can even start loading. Lower is better.",
  },
  // TCP
  connect_ms: {
    label: "TCP connect",
    unit: "ms",
    description:
      "Time to open a TCP connection (the SYN/ACK handshake) to a server. The first step of any web request. Lower is better.",
  },
  // TLS
  handshake_ms: {
    label: "TLS handshake",
    unit: "ms",
    description:
      "Time to negotiate the encrypted HTTPS session after the connection opens. Lower is better.",
  },
  // HTTP
  ttfb_ms: {
    label: "Time to first byte",
    unit: "ms",
    description:
      "From sending the request to the first byte of the response arriving — how long until a page begins to appear. Lower is better.",
  },
  download_ms: {
    label: "Download time",
    unit: "ms",
    description:
      "Time spent pulling down the response body after the first byte arrives. Lower is better.",
  },
  transfer_mbps: {
    label: "Transfer speed",
    unit: "Mbps",
    description:
      "Throughput while downloading, in megabits per second. This is the one measure where HIGHER is better.",
    higherIsBetter: true,
  },
  // Browser — paint timing (the core of SOPS, the human-feel score)
  fcp_ms: {
    label: "First Contentful Paint",
    unit: "ms",
    description:
      "When the first real content (text/image) paints — the 'it's responding' moment. Perceptual, not completion: how soon you see *something*. Lower is better.",
  },
  lcp_ms: {
    label: "Largest Contentful Paint",
    unit: "ms",
    description:
      "When the main content becomes visible. Google's core 'is it usefully loaded' signal (good ≤2.5s). Lower is better.",
  },
  inp_ms: {
    label: "Interaction to Next Paint",
    unit: "ms",
    description:
      "How quickly the page paints a response to input — responsiveness to taps/keys (good ≤200ms). Best-effort here (synthetic interaction); may be blank. Lower is better.",
  },
  // Browser (Playwright)
  total_render_ms: {
    label: "Total render",
    unit: "ms",
    description:
      "Wall-clock time for a real headless browser to fetch, parse and fully render the page. The closest measure to how slow a site actually feels. Lower is better.",
  },
  dom_content_loaded_ms: {
    label: "DOM content loaded",
    unit: "ms",
    description:
      "Time until the page's HTML is parsed and the DOM is ready (the DOMContentLoaded event). Lower is better.",
  },
  load_event_ms: {
    label: "Page load",
    unit: "ms",
    description:
      "Time until the browser's load event fires — all initial resources fetched and the page is fully loaded. Lower is better.",
  },
};

export function getMetricMeta(key: string): MetricMeta {
  return (
    METRIC_META[key] ?? {
      label: key,
      description: "Measured value for this run.",
    }
  );
}
