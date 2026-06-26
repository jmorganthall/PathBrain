import Box from "@mui/material/Box";
import Stack from "@mui/material/Stack";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";

import type { TrendHeatmapResponse } from "../api/types";
import { fmtBucket } from "./RelativeDelta";

const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const EMPTY = "rgba(255,255,255,0.04)";

/** Red→amber→green by "goodness" in [0,1] (1 = best). */
function goodnessColor(good: number): string {
  const hue = 0 + good * 120; // 0=red, 60=amber, 120=green
  return `hsl(${hue}, 65%, 45%)`;
}

function fmtVal(v: number | null | undefined, unit: string): string {
  if (v == null || Number.isNaN(v)) return "—";
  const n = Number.isInteger(v) ? v.toString() : v.toFixed(1);
  return unit ? `${n} ${unit}` : n;
}

interface Props {
  data: TrendHeatmapResponse;
  /** Highlight the current (weekday, hour) cell, if known. */
  nowWeekday?: number;
  nowHour?: number;
}

export default function TrendHeatmap({ data, nowWeekday, nowHour }: Props) {
  const byCell = new Map<string, (typeof data.cells)[number]>();
  for (const c of data.cells) byCell.set(`${c.weekday}-${c.hour}`, c);

  const medians = data.cells.map((c) => c.median);
  const min = medians.length ? Math.min(...medians) : 0;
  const max = medians.length ? Math.max(...medians) : 1;
  const span = max - min || 1;
  const goodnessOf = (v: number) => {
    const norm = (v - min) / span; // 0 = lowest median, 1 = highest
    return data.higher_is_better ? norm : 1 - norm;
  };

  const hours = Array.from({ length: 24 }, (_, h) => h);

  return (
    <Box>
      <Box
        sx={{
          display: "grid",
          gridTemplateColumns: `36px repeat(24, 1fr)`,
          gap: "2px",
          minWidth: 560,
        }}
      >
        {/* Header row: hour labels (every 3h to stay readable) */}
        <Box />
        {hours.map((h) => (
          <Typography
            key={h}
            variant="caption"
            sx={{ textAlign: "center", color: "text.secondary", fontSize: 10 }}
          >
            {h % 3 === 0 ? h : ""}
          </Typography>
        ))}

        {WEEKDAYS.map((wd, wi) => (
          <Box key={wd} sx={{ display: "contents" }}>
            <Typography
              variant="caption"
              sx={{ color: "text.secondary", fontSize: 11, alignSelf: "center" }}
            >
              {wd}
            </Typography>
            {hours.map((h) => {
              const cell = byCell.get(`${wi}-${h}`);
              const isNow = wi === nowWeekday && h === nowHour;
              const bg = cell ? goodnessColor(goodnessOf(cell.median)) : EMPTY;
              const title = cell ? (
                <Box>
                  <Typography variant="caption" display="block">
                    {fmtBucket(wi, h)} — {data.label}
                  </Typography>
                  <Typography variant="caption" display="block" color="text.secondary">
                    median {fmtVal(cell.median, data.unit)} · IQR{" "}
                    {fmtVal(cell.p25, data.unit)}–{fmtVal(cell.p75, data.unit)} · n={cell.count}
                  </Typography>
                </Box>
              ) : (
                <Box>
                  <Typography variant="caption">{fmtBucket(wi, h)}</Typography>
                  <Typography variant="caption" display="block" color="text.secondary">
                    no data
                  </Typography>
                </Box>
              );
              return (
                <Tooltip key={h} title={title} disableInteractive>
                  <Box
                    sx={{
                      height: 16,
                      borderRadius: 0.5,
                      bgcolor: bg,
                      outline: isNow ? "2px solid #fff" : "none",
                      outlineOffset: isNow ? "-1px" : 0,
                      cursor: "default",
                    }}
                  />
                </Tooltip>
              );
            })}
          </Box>
        ))}
      </Box>

      <Stack direction="row" spacing={1} alignItems="center" sx={{ mt: 1.5 }}>
        <Typography variant="caption" color="text.secondary">
          worse
        </Typography>
        <Box
          sx={{
            width: 120,
            height: 8,
            borderRadius: 1,
            background: `linear-gradient(90deg, ${goodnessColor(0)}, ${goodnessColor(0.5)}, ${goodnessColor(1)})`,
          }}
        />
        <Typography variant="caption" color="text.secondary">
          better
        </Typography>
        <Typography variant="caption" color="text.secondary" sx={{ ml: 2 }}>
          {data.total} runs · last {data.window_days}d · local time
        </Typography>
      </Stack>
    </Box>
  );
}
