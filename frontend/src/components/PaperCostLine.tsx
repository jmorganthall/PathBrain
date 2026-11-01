import { Tooltip, Typography } from "@mui/material";
import type { PaperCost } from "../api/types";

// What a verdict costs in raw time against the fastest profile on paper (lowest median
// FCP × LCP). The experience-graded crowns may name a profile that is slower to paint;
// whether that trade is worth it is the reader's call, and this is the price. A gap inside
// the measurement noise is said to be noise, never shown as a cost.

function signed(ms: number, pct: number): string {
  const sign = ms > 0 ? "+" : ms < 0 ? "−" : "±";
  const pctSign = pct > 0 ? "+" : pct < 0 ? "−" : "";
  return `${sign}${Math.abs(ms).toFixed(0)} ms (${pctSign}${Math.abs(pct).toFixed(1)}%)`;
}

export default function PaperCostLine({ cost }: { cost?: PaperCost | null }) {
  if (!cost) return null;
  if (cost.is_math) {
    return (
      <Typography variant="caption" color="success.main" sx={{ display: "block" }}>
        is also the fastest on paper
      </Typography>
    );
  }
  const lcp = signed(cost.lcp_delta_ms, cost.lcp_pct);
  const fcp = signed(cost.fcp_delta_ms, cost.fcp_pct);
  const tip =
    `Against the fastest on paper (lowest median FCP × LCP): first paint ${fcp}` +
    `${cost.fcp_clear ? "" : " — inside the noise"}, main content ${lcp}` +
    `${cost.lcp_clear ? "" : " — inside the noise"}; FCP × LCP ` +
    `${cost.product_pct >= 0 ? "+" : "−"}${Math.abs(cost.product_pct).toFixed(1)}%. ` +
    "Positive is slower. A gap counts only when it clears twice the noise of the two medians.";
  const slower = cost.lcp_clear && cost.lcp_delta_ms > 0;
  return (
    <Tooltip title={tip}>
      <Typography
        variant="caption"
        color={cost.within_noise ? "text.secondary" : slower ? "warning.main" : "text.secondary"}
        sx={{ display: "block" }}
      >
        {cost.within_noise
          ? `vs fastest on paper: within noise (main content ${lcp})`
          : `vs fastest on paper: main content ${lcp} · first paint ${fcp}`}
      </Typography>
    </Tooltip>
  );
}
