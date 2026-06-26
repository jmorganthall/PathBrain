import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import ArrowUpwardIcon from "@mui/icons-material/ArrowUpward";
import ArrowDownwardIcon from "@mui/icons-material/ArrowDownward";
import RemoveIcon from "@mui/icons-material/Remove";

import type { TrendRelative } from "../api/types";

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** "Mon 2 PM" for a (weekday, hour) bucket, in the viewer's locale-ish form. */
export function fmtBucket(weekday: number, hour: number): string {
  const wd = WEEKDAYS[weekday] ?? "?";
  const h12 = hour % 12 === 0 ? 12 : hour % 12;
  const ampm = hour < 12 ? "AM" : "PM";
  return `${wd} ${h12} ${ampm}`;
}

function fmtVal(v: number | null | undefined, unit: string): string {
  if (v == null || Number.isNaN(v)) return "—";
  const n = Number.isInteger(v) ? v.toString() : v.toFixed(1);
  return unit ? `${n} ${unit}` : n;
}

const NEUTRAL = "#90a4ae";
const GOOD = "#66bb6a";
const BAD = "#ef5350";

function tone(r: TrendRelative): string {
  if (r.delta == null || r.band === "typical") return NEUTRAL;
  return r.better ? GOOD : BAD;
}

/** Plain-English summary, e.g. "much better than typical". */
function phrase(r: TrendRelative): string {
  if (r.delta == null) return "no current reading";
  if (r.band === "typical") return "about typical";
  const mag = r.band === "strong" ? "much" : "a bit";
  return `${mag} ${r.better ? "better" : "worse"} than typical`;
}

interface Props {
  reading: TrendRelative;
  /** The (weekday, hour) the baseline is for, to render "vs typical Mon 2 PM". */
  weekday: number;
  hour: number;
  /** Compact = a single inline chip (Dashboard); else a labeled row (Trends). */
  compact?: boolean;
}

export default function RelativeDelta({ reading: r, weekday, hour, compact }: Props) {
  const color = tone(r);
  const lowConfidence = r.baseline_source !== "exact";
  const Arrow =
    r.delta == null || r.band === "typical"
      ? RemoveIcon
      : r.better
        ? ArrowUpwardIcon
        : ArrowDownwardIcon;

  const deltaText =
    r.delta == null
      ? "—"
      : `${r.delta > 0 ? "+" : ""}${fmtVal(r.delta, r.unit)}`;

  const sourceNote =
    r.baseline_source === "exact"
      ? `${fmtBucket(weekday, hour)} history`
      : r.baseline_source === "hour"
        ? `${hour % 12 === 0 ? 12 : hour % 12} ${hour < 12 ? "AM" : "PM"} across all days`
        : r.baseline_source === "weekday"
          ? `all of ${WEEKDAYS[weekday]}`
          : "all hours (sparse data)";

  const tip = (
    <Box>
      <Typography variant="caption" display="block">
        {r.label}: {phrase(r)}
      </Typography>
      <Typography variant="caption" display="block" color="text.secondary">
        now {fmtVal(r.current, r.unit)} · typical {fmtVal(r.baseline, r.unit)} (IQR{" "}
        {fmtVal(r.p25, r.unit)}–{fmtVal(r.p75, r.unit)})
      </Typography>
      {r.percentile != null && (
        <Typography variant="caption" display="block" color="text.secondary">
          {r.percentile}th percentile{r.z != null ? ` · z=${r.z}` : ""}
        </Typography>
      )}
      <Typography variant="caption" display="block" color="text.secondary">
        baseline: {sourceNote} · n={r.count}
        {lowConfidence ? " · low confidence" : ""}
      </Typography>
    </Box>
  );

  const chip = (
    <Chip
      size="small"
      icon={<Arrow sx={{ fontSize: 16 }} />}
      label={`${deltaText} vs typical`}
      variant="outlined"
      sx={{
        color,
        borderColor: color,
        opacity: lowConfidence ? 0.65 : 1,
        "& .MuiChip-icon": { color },
        fontWeight: 600,
      }}
    />
  );

  if (compact) {
    return <Tooltip title={tip}>{chip}</Tooltip>;
  }

  return (
    <Box
      sx={{
        display: "grid",
        gridTemplateColumns: "minmax(120px, 1fr) auto auto",
        alignItems: "center",
        gap: 1,
        py: 0.5,
      }}
    >
      <Typography variant="body2">{r.label}</Typography>
      <Typography variant="body2" color="text.secondary" sx={{ textAlign: "right" }}>
        {fmtVal(r.current, r.unit)} <span style={{ opacity: 0.6 }}>/ {fmtVal(r.baseline, r.unit)}</span>
      </Typography>
      <Tooltip title={tip}>{chip}</Tooltip>
    </Box>
  );
}
