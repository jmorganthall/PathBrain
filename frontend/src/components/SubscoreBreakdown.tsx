import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import LinearProgress from "@mui/material/LinearProgress";
import Stack from "@mui/material/Stack";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import { sopsColor } from "../theme";
import { fmtScore } from "../utils/format";
import { useMetricMeta, useMetricOrder } from "../utils/metrics";
import type { StallAttribution } from "../api/types";

interface ScoreLike {
  subscores: Record<string, number>;
  weights_used: Record<string, number>;
  metric_values?: Record<string, number>;
}

// network = tunable (FQ-CoDel/quantum); render = main-thread, network config won't
// move it; mixed/unknown sit between. Colors signal "is this mine to tune?".
const ATTRIBUTION_META: Record<
  StallAttribution["dominant"],
  { color: "info" | "warning" | "default"; tip: string }
> = {
  network: {
    color: "info",
    tip: "Most stall time was network-attributed — no bytes arriving and no long main-thread task. This is the tunable layer (FQ-CoDel / quantum / target).",
  },
  render: {
    color: "warning",
    tip: "Most stall time overlapped a long main-thread task — render-bound. Network shaping won't move this; it's a page/CPU problem.",
  },
  mixed: {
    color: "warning",
    tip: "Stall time split between network and render — partly tunable at the network layer, partly main-thread.",
  },
  unknown: {
    color: "default",
    tip: "Stall couldn't be attributed — the browser exposed no Long Animation Frame / long-task data.",
  },
};

function fmtWeight(w: number | undefined): string {
  if (w == null) return "";
  // weights_used are fractions that sum to 1 — show as a percentage.
  return `${Math.round(w * 100)}%`;
}

export default function SubscoreBreakdown({
  score,
  attribution,
}: {
  score: ScoreLike;
  attribution?: StallAttribution | null;
}) {
  const metricMeta = useMetricMeta();
  const metricOrder = useMetricOrder();

  // Units come from the metric registry (single source of truth).
  const fmtValue = (metric: string, v: number | undefined): string => {
    if (v == null) return "";
    const unit = metricMeta(metric).unit ?? "";
    const n = Number.isInteger(v) ? v.toString() : v.toFixed(1);
    return `${n}${unit ? " " + unit : ""}`;
  };

  // Order by when each metric happens in a page load (chronological), not weight.
  const keys = Object.keys(score.subscores).sort((a, b) => metricOrder(a) - metricOrder(b));

  if (keys.length === 0) {
    return (
      <Typography variant="body2" color="text.secondary">
        No subscores available.
      </Typography>
    );
  }

  return (
    <Stack spacing={1.5}>
      {keys.map((k) => {
        const value = score.subscores[k];
        const weight = score.weights_used[k];
        const raw = score.metric_values?.[k];
        return (
          <Box key={k}>
            <Box sx={{ display: "flex", justifyContent: "space-between", mb: 0.5, alignItems: "baseline" }}>
              <Typography variant="body2" sx={{ textTransform: "uppercase", letterSpacing: 0.5 }}>
                {k}
                {weight != null && (
                  <Typography component="span" variant="caption" color="text.secondary" sx={{ ml: 1 }}>
                    weight {fmtWeight(weight)}
                  </Typography>
                )}
                {k === "longest_stall" && attribution && (
                  <Tooltip title={ATTRIBUTION_META[attribution.dominant].tip} arrow>
                    <Chip
                      size="small"
                      label={attribution.dominant}
                      color={ATTRIBUTION_META[attribution.dominant].color}
                      variant="outlined"
                      sx={{ ml: 1, height: 18, fontSize: "0.65rem", textTransform: "lowercase" }}
                    />
                  </Tooltip>
                )}
              </Typography>
              <Box sx={{ display: "flex", alignItems: "baseline", gap: 1 }}>
                {raw != null && (
                  <Typography component="span" variant="caption" color="text.secondary">
                    {fmtValue(k, raw)}
                  </Typography>
                )}
                <Typography variant="body2" sx={{ fontWeight: 600, color: sopsColor(value) }}>
                  {fmtScore(value)}
                </Typography>
              </Box>
            </Box>
            <LinearProgress
              variant="determinate"
              value={Math.max(0, Math.min(100, value))}
              sx={{
                height: 8,
                borderRadius: 4,
                bgcolor: "rgba(255,255,255,0.06)",
                "& .MuiLinearProgress-bar": { backgroundColor: sopsColor(value) },
              }}
            />
          </Box>
        );
      })}
    </Stack>
  );
}
