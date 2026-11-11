import { Tooltip, Typography } from "@mui/material";
import type { FastestLegs, LegGap, LegGaps } from "../api/types";

// How much slower (or faster) a crown is than the field's fastest LCP and fastest FCP, leg
// by leg — the same line under every crown, wherever one is named, so a verdict that paints
// twice as slowly as the fastest profile can never look the same as one within the noise.
// Each leg is measured against its own record holder (usually two different profiles).
// A gap inside twice the noise of the two medians is called noise, never a cost.

function leg(name: string, g?: LegGap): string | null {
  if (!g) return null;
  if (g.is_fastest) return `the fastest ${name}`;
  if (!g.clear) return `${name} within noise of the fastest`;
  const sign = g.delta_ms > 0 ? "+" : "−";
  const word = g.delta_ms > 0 ? "slower" : "faster";
  return `${name} ${sign}${Math.abs(g.delta_ms).toFixed(0)} ms (${sign}${Math.abs(g.pct).toFixed(0)}%) ${word}`;
}

function tipLeg(name: string, g?: LegGap, holder?: string | null): string {
  if (!g) return `${name}: no median yet.`;
  const who = holder ? ` (${holder})` : "";
  const noise = g.bar_ms == null ? "noise bar unknown" : `noise bar ±${g.bar_ms.toFixed(0)} ms`;
  return (
    `${name} ${g.ms.toFixed(0)} ms against the fastest ${g.fastest_ms.toFixed(0)} ms${who}: ` +
    `${g.delta_ms >= 0 ? "+" : "−"}${Math.abs(g.delta_ms).toFixed(0)} ms ` +
    `(${g.pct >= 0 ? "+" : "−"}${Math.abs(g.pct).toFixed(1)}%), ${noise}` +
    `${g.clear ? "" : " — inside the noise"}.`
  );
}

export default function FastestGapLine({
  gaps,
  fastest,
}: {
  gaps?: LegGaps | null;
  fastest?: FastestLegs | null;
}) {
  if (!gaps || (!gaps.lcp && !gaps.fcp)) return null;
  const parts = [leg("LCP", gaps.lcp), leg("FCP", gaps.fcp)].filter(Boolean) as string[];
  const slowest = Math.max(
    gaps.lcp?.clear ? gaps.lcp.pct : 0,
    gaps.fcp?.clear ? gaps.fcp.pct : 0,
  );
  const color = slowest >= 25 ? "error.main" : slowest > 0 ? "warning.main" : "text.secondary";
  const tip =
    `${tipLeg("LCP (main content)", gaps.lcp, fastest?.lcp?.name ?? fastest?.lcp?.label)} ` +
    `${tipLeg("FCP (first paint)", gaps.fcp, fastest?.fcp?.name ?? fastest?.fcp?.label)} ` +
    "Each leg is compared with the field's fastest median on that leg among confident profiles. " +
    "A gap counts only when it clears twice the noise of the two medians.";
  return (
    <Tooltip title={tip}>
      <Typography variant="caption" color={color} sx={{ display: "block" }}>
        vs fastest: {parts.join(" · ")}
      </Typography>
    </Tooltip>
  );
}
