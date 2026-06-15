import Box from "@mui/material/Box";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import TrendingUpIcon from "@mui/icons-material/TrendingUp";
import TrendingDownIcon from "@mui/icons-material/TrendingDown";
import TrendingFlatIcon from "@mui/icons-material/TrendingFlat";

// Relative change smaller than this counts as "about the same" — we show a flat,
// muted marker rather than a misleading up/down arrow for noise.
const FLAT_THRESHOLD = 0.02; // 2%

interface Props {
  current: number | null | undefined;
  baseline: number | null | undefined;
  /** True only for transfer speed; everything else is lower-is-better. */
  higherIsBetter?: boolean;
  unit?: string;
  /** Number of runs that fed the baseline average. */
  runCount: number;
  /** e.g. "profile average" or "recent average". */
  scopeLabel: string;
}

function fmt(value: number, unit?: string): string {
  const n = Number.isInteger(value) ? value.toString() : value.toFixed(1);
  return unit ? `${n} ${unit}` : n;
}

/**
 * A direction-aware comparison marker for one metric: a green up-arrow when this
 * run is better than the baseline, a red down-arrow when it's worse, and a muted
 * flat marker when it's about the same. "Better" accounts for inverted metrics —
 * for latency/render/etc. a *lower* value is an improvement (green up-arrow).
 */
export default function MetricDelta({
  current,
  baseline,
  higherIsBetter = false,
  unit,
  runCount,
  scopeLabel,
}: Props) {
  if (
    current == null ||
    baseline == null ||
    Number.isNaN(current) ||
    Number.isNaN(baseline)
  ) {
    return null;
  }

  const delta = current - baseline;
  const rel = baseline !== 0 ? Math.abs(delta) / Math.abs(baseline) : delta === 0 ? 0 : Infinity;
  const flat = rel < FLAT_THRESHOLD;
  const better = higherIsBetter ? current > baseline : current < baseline;

  const Icon = flat ? TrendingFlatIcon : better ? TrendingUpIcon : TrendingDownIcon;
  const color = flat ? "text.disabled" : better ? "success.main" : "error.main";
  const pctText = Number.isFinite(rel) && !flat ? `${Math.round(rel * 100)}%` : null;

  const verb = flat ? "About the same as" : better ? "Improved vs" : "Worse than";
  const tip = `${verb} the ${scopeLabel} (${fmt(baseline, unit)})${
    pctText ? ` — ${pctText} ${better ? "better" : "worse"}` : ""
  } over ${runCount} run${runCount === 1 ? "" : "s"}.`;

  return (
    <Tooltip title={tip} enterTouchDelay={0} leaveTouchDelay={4000} arrow>
      <Box
        component="span"
        aria-label={tip}
        sx={{
          display: "inline-flex",
          alignItems: "center",
          gap: 0.25,
          ml: 0.75,
          verticalAlign: "middle",
          color,
          cursor: "help",
        }}
      >
        <Icon sx={{ fontSize: "1rem" }} />
        {pctText && (
          <Typography component="span" variant="caption" sx={{ color, fontWeight: 700 }}>
            {pctText}
          </Typography>
        )}
      </Box>
    </Tooltip>
  );
}
