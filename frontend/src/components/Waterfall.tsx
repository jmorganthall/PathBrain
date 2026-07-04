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
  { key: "nav_response", src: "nav_response_ms", label: "Response download", color: "#ffb74d" },
  { key: "nav_render", src: "nav_render_ms", label: "Render → first paint", color: "#ab47bc" },
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
  const prefixMs = parts.filter((p) => PREFIX_KEYS.has(p.key)).reduce((a, p) => a + (p.ms as number), 0);
  const fcpIndependent =
    metrics["nav_fcp_independent_ms"] ?? metrics["nav_fcp_independent"] ?? null;
  const lcpIndependent =
    metrics["nav_lcp_independent_ms"] ?? metrics["nav_lcp_independent"] ?? null;

  const markers: { key: string; label: string }[] = [];
  if (offset["nav_request"] != null) markers.push({ key: "nav_request", label: "first byte" });
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
              const showLabel = w > 11;
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
                      overflow: "hidden",
                      cursor: "help",
                      borderRight: "1px solid rgba(0,0,0,0.25)",
                    }}
                  >
                    {showLabel && (
                      <Typography
                        sx={{
                          fontSize: 10,
                          fontWeight: 600,
                          color: "rgba(0,0,0,0.82)",
                          px: 0.5,
                          whiteSpace: "nowrap",
                          overflow: "hidden",
                          textOverflow: "ellipsis",
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

      {/* Crux summary: network prefix vs. network-independent paint. */}
      <Stack direction="row" spacing={2} flexWrap="wrap" useFlexGap sx={{ mt: 1.5 }}>
        <Typography variant="caption" color="text.secondary">
          Total <b>{fmtMs(total)}</b>
        </Typography>
        <Typography variant="caption" sx={{ color: "#7986cb" }}>
          Network prefix (to first byte) <b>{fmtMs(prefixMs)}</b>
        </Typography>
        {typeof fcpIndependent === "number" && (
          <Typography variant="caption" sx={{ color: "#ab47bc" }}>
            FCP after first byte <b>{fmtMs(fcpIndependent)}</b>
          </Typography>
        )}
        {typeof lcpIndependent === "number" && (
          <Typography variant="caption" sx={{ color: "#f48fb1" }}>
            LCP after first byte <b>{fmtMs(lcpIndependent)}</b>
          </Typography>
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
