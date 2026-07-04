import { useMemo } from "react";
import Box from "@mui/material/Box";
import Stack from "@mui/material/Stack";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";

// The navigation-timing waterfall: the load broken into additive, non-overlapping
// phases (silver layer, from interpret/waterfall.py). Each phase is an independent
// measurable; together they tile navigationStart → load. The cool→warm colour ramp
// separates the network-confounded prefix (everything up to first byte) from the
// render/paint side — the visual answer to "is this profile's paint edge real, or
// just fast DNS/TLS at that moment?".
//
// Accepts either per-run metrics (source keys like `nav_dns_ms`, from a
// BenchmarkResult) or per-profile medians (logical keys like `nav_dns`), resolving
// whichever is present.

interface Segment {
  key: string;
  src: string;
  label: string;
  color: string;
}

// Wall-clock order. Network prefix (stall→request) in cool blues; the document
// response in amber; the render/paint tail (render→load) in purples/pink.
const SEGMENTS: Segment[] = [
  { key: "nav_stall", src: "nav_stall_ms", label: "Pre-connect stall", color: "#546e7a" },
  { key: "nav_dns", src: "nav_dns_ms", label: "DNS", color: "#4dd0e1" },
  { key: "nav_tcp", src: "nav_tcp_ms", label: "TCP connect", color: "#4fc3f7" },
  { key: "nav_tls", src: "nav_tls_ms", label: "TLS handshake", color: "#64b5f6" },
  { key: "nav_request", src: "nav_request_ms", label: "Request / TTFB wait", color: "#7986cb" },
  // Body delivery: the one SQM-facing phase (amber, between the cool setup prefix and
  // the warm client render), so the eye lands on the phase shaping actually moves.
  { key: "nav_response", src: "nav_response_ms", label: "Body delivery (SQM)", color: "#ffb74d" },
  { key: "nav_render", src: "nav_render_ms", label: "Client render → FCP", color: "#ab47bc" },
  { key: "nav_fcp_lcp", src: "nav_fcp_lcp_ms", label: "First → largest paint", color: "#ce93d8" },
  { key: "nav_lcp_load", src: "nav_lcp_load_ms", label: "Largest paint → load", color: "#f48fb1" },
];

// Phases that make up the network-confounded prefix (everything up to first byte).
const PREFIX_KEYS = new Set(["nav_stall", "nav_dns", "nav_tcp", "nav_tls", "nav_request"]);

type MetricMap = Record<string, number | null | undefined>;

function resolve(metrics: MetricMap, seg: Segment): number | null {
  const v = metrics[seg.src] ?? metrics[seg.key];
  return typeof v === "number" && isFinite(v) ? v : null;
}

function fmtMs(v: number): string {
  if (v >= 1000) return `${(v / 1000).toFixed(2)} s`;
  return `${v < 10 ? v.toFixed(1) : Math.round(v)} ms`;
}

// A milestone tick above the bar at a cumulative offset (% of total).
function Marker({ pct, label }: { pct: number; label: string }) {
  return (
    <Box
      sx={{
        position: "absolute",
        left: `${pct}%`,
        top: 0,
        bottom: 0,
        transform: "translateX(-0.5px)",
        borderLeft: "1px dashed",
        borderColor: "text.disabled",
        pointerEvents: "none",
      }}
    >
      <Typography
        variant="caption"
        sx={{
          position: "absolute",
          top: -16,
          left: pct > 85 ? "auto" : 2,
          right: pct > 85 ? 2 : "auto",
          whiteSpace: "nowrap",
          fontSize: 10,
          color: "text.secondary",
        }}
      >
        {label}
      </Typography>
    </Box>
  );
}

export default function Waterfall({ metrics, height = 40 }: { metrics: MetricMap; height?: number }) {
  const parts = useMemo(
    () => SEGMENTS.map((s) => ({ ...s, ms: resolve(metrics, s) })).filter((p) => p.ms !== null),
    [metrics],
  );
  const total = useMemo(() => parts.reduce((a, p) => a + (p.ms as number), 0), [parts]);

  if (parts.length === 0 || total <= 0) {
    return (
      <Typography variant="body2" color="text.secondary">
        No navigation-timing waterfall for this data yet (needs a browser run with page-nav timing).
      </Typography>
    );
  }

  // Cumulative offsets for the milestone markers (first byte / FCP / LCP).
  let acc = 0;
  const offset: Record<string, number> = {};
  for (const p of parts) {
    acc += p.ms as number;
    offset[p.key] = acc;
  }
  const pct = (ms: number) => (ms / total) * 100;
  // The three regimes, split at the two boundaries that matter (first byte, responseEnd):
  //   setup    = stall+dns+tcp+tls+request  (weather-dominated, up to first byte)
  //   delivery = nav_response               (SQM-facing — the phase shaping moves)
  //   client   = nav_render                 (client CPU — shaping-immune health check)
  const setupMs = parts.filter((p) => PREFIX_KEYS.has(p.key)).reduce((a, p) => a + (p.ms as number), 0);
  const deliveryMs = parts.find((p) => p.key === "nav_response")?.ms ?? null;
  const clientMs = parts.find((p) => p.key === "nav_render")?.ms ?? null;

  const markers: { key: string; label: string }[] = [];
  if (offset["nav_request"] != null) markers.push({ key: "nav_request", label: "first byte" });
  if (offset["nav_response"] != null) markers.push({ key: "nav_response", label: "response done" });
  if (offset["nav_render"] != null) markers.push({ key: "nav_render", label: "FCP" });
  if (offset["nav_fcp_lcp"] != null) markers.push({ key: "nav_fcp_lcp", label: "LCP" });

  return (
    <Box>
      {/* Milestone ticks + the stacked phase bar. */}
      <Box sx={{ position: "relative", pt: 2.5 }}>
        <Box sx={{ position: "relative" }}>
          <Box
            sx={{
              display: "flex",
              width: "100%",
              height,
              borderRadius: 1,
              overflow: "hidden",
              border: "1px solid",
              borderColor: "divider",
            }}
          >
            {parts.map((p) => {
              const w = pct(p.ms as number);
              // Delivery is the SQM-facing phase — the whole point of the view — so it's
              // always labelled, never clipped, even when it's a thin sliver on an idle run.
              const isDelivery = p.key === "nav_response";
              const showLabel = w > 11 || isDelivery;
              return (
                <Tooltip
                  key={p.key}
                  arrow
                  title={`${p.label}: ${fmtMs(p.ms as number)} · ${w.toFixed(1)}% of load`}
                >
                  <Box
                    sx={{
                      width: `${w}%`,
                      minWidth: w > 0 ? 2 : 0,
                      bgcolor: p.color,
                      display: "flex",
                      alignItems: "center",
                      justifyContent: "center",
                      overflow: isDelivery ? "visible" : "hidden",
                      cursor: "help",
                      borderRight: "1px solid rgba(0,0,0,0.25)",
                    }}
                  >
                    {showLabel && (
                      <Typography
                        sx={{
                          fontSize: 10,
                          fontWeight: isDelivery ? 700 : 600,
                          color: "rgba(0,0,0,0.82)",
                          px: 0.5,
                          whiteSpace: "nowrap",
                          overflow: isDelivery ? "visible" : "hidden",
                          textOverflow: isDelivery ? "clip" : "ellipsis",
                        }}
                      >
                        {fmtMs(p.ms as number)}
                      </Typography>
                    )}
                  </Box>
                </Tooltip>
              );
            })}
          </Box>
          {markers.map((m) => (
            <Marker key={m.key} pct={pct(offset[m.key])} label={m.label} />
          ))}
        </Box>
      </Box>

      {/* Crux summary: the three regimes — setup (weather) · delivery (shapeable) ·
          client render (shaping-immune). Delivery is the phase to judge a profile on. */}
      <Stack direction="row" spacing={2} flexWrap="wrap" useFlexGap sx={{ mt: 1.5 }}>
        <Typography variant="caption" color="text.secondary">
          Total <b>{fmtMs(total)}</b>
        </Typography>
        <Tooltip arrow title="DNS/TCP/TLS/TTFB up to the first byte. Weather-dominated — driven by path/server conditions, not your shaper.">
          <Typography variant="caption" sx={{ color: "#7986cb", cursor: "help" }}>
            Setup (to first byte) <b>{fmtMs(setupMs)}</b>
          </Typography>
        </Tooltip>
        {typeof deliveryMs === "number" && (
          <Tooltip arrow title="responseStart→responseEnd: body delivery through your queue. The one SQM-facing phase — judge a profile on this.">
            <Typography variant="caption" sx={{ color: "#ffa726", cursor: "help", fontWeight: 500 }}>
              Delivery (SQM) <b>{fmtMs(deliveryMs)}</b>
            </Typography>
          </Tooltip>
        )}
        {typeof clientMs === "number" && (
          <Tooltip arrow title="responseEnd→FCP: parse/style/layout/paint. Pure client CPU — shaping can't move it. Should be near-constant across profiles.">
            <Typography variant="caption" sx={{ color: "#ab47bc", cursor: "help" }}>
              Client render <b>{fmtMs(clientMs)}</b>
            </Typography>
          </Tooltip>
        )}
      </Stack>

      {/* Legend. */}
      <Box
        sx={{
          mt: 1,
          display: "grid",
          gap: 0.5,
          gridTemplateColumns: { xs: "repeat(2, 1fr)", sm: "repeat(3, 1fr)", md: "repeat(5, 1fr)" },
        }}
      >
        {parts.map((p) => (
          <Stack key={p.key} direction="row" spacing={0.75} alignItems="center">
            <Box sx={{ width: 10, height: 10, borderRadius: 0.5, bgcolor: p.color, flex: "0 0 auto" }} />
            <Typography variant="caption" color="text.secondary" noWrap title={p.label}>
              {p.label}
            </Typography>
          </Stack>
        ))}
      </Box>
    </Box>
  );
}
