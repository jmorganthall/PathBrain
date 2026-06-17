import Box from "@mui/material/Box";
import LinearProgress from "@mui/material/LinearProgress";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";
import { sopsColor } from "../theme";
import { fmtScore } from "../utils/format";
import { useMetricMeta, useMetricOrder } from "../utils/metrics";

interface ScoreLike {
  subscores: Record<string, number>;
  weights_used: Record<string, number>;
  metric_values?: Record<string, number>;
}

function fmtWeight(w: number | undefined): string {
  if (w == null) return "";
  // weights_used are fractions that sum to 1 — show as a percentage.
  return `${Math.round(w * 100)}%`;
}

export default function SubscoreBreakdown({ score }: { score: ScoreLike }) {
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
