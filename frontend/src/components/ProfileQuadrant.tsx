import {
  CartesianGrid,
  Cell,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
  ZAxis,
} from "recharts";
import { useTheme } from "@mui/material/styles";
import Box from "@mui/material/Box";
import Typography from "@mui/material/Typography";

import type { SettingsProfile } from "../api/types";

// Speed (x) vs Smoothness (y) quadrant. Both axes are higher-is-better, so the
// TOP-RIGHT corner is the smoothest AND fastest — the profile to aim for. Each dot
// is a profile; confident profiles (>= the iteration minimum) are colored, profiles
// without enough iterations are greyed out so they read as "not yet trustworthy".
interface Point {
  speed: number;
  smoothness: number;
  label: string;
  fingerprint: string;
  iterations: number;
  confident: boolean;
}

function median(xs: number[]): number {
  if (!xs.length) return 0;
  const s = [...xs].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

function QuadrantTooltip({ active, payload }: { active?: boolean; payload?: Array<{ payload: Point }> }) {
  if (!active || !payload || !payload.length) return null;
  const p = payload[0].payload;
  return (
    <Box sx={{ bgcolor: "background.paper", border: 1, borderColor: "divider", borderRadius: 1, p: 1 }}>
      <Typography variant="caption" sx={{ display: "block", fontWeight: 700, wordBreak: "break-word" }}>
        {p.label}
      </Typography>
      <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
        Smoothness {p.smoothness} · Speed {p.speed}
      </Typography>
      <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
        {p.iterations} iteration(s){p.confident ? "" : " — limited data"}
      </Typography>
    </Box>
  );
}

export default function ProfileQuadrant({
  profiles,
  minIterations,
}: {
  profiles: SettingsProfile[];
  minIterations: number;
}) {
  const theme = useTheme();
  // Only profiles that have BOTH axes can be plotted.
  const points: Point[] = profiles
    .filter((p) => p.speed?.median != null && p.median != null)
    .map((p) => ({
      speed: p.speed!.median,
      smoothness: p.median,
      label: p.label,
      fingerprint: p.fingerprint,
      iterations: p.iterations,
      confident: p.confident,
    }));

  if (points.length < 2) {
    return (
      <Typography variant="caption" color="text.secondary">
        Need at least two profiles with both a Speed and a Smoothness score to plot the quadrant.
      </Typography>
    );
  }

  const confident = points.filter((p) => p.confident);
  const limited = points.filter((p) => !p.confident);
  // Quadrant dividers at the median of each axis, so the cloud splits into four.
  const xMid = median(points.map((p) => p.speed));
  const yMid = median(points.map((p) => p.smoothness));
  const greyColor = theme.palette.text.disabled;

  return (
    <Box>
      <Box sx={{ width: "100%", height: 360 }}>
        <ResponsiveContainer>
          <ScatterChart margin={{ top: 16, right: 24, bottom: 32, left: 8 }}>
            <CartesianGrid strokeDasharray="3 3" stroke={theme.palette.divider} />
            <XAxis
              type="number"
              dataKey="speed"
              name="Speed"
              domain={["dataMin - 2", "dataMax + 2"]}
              tick={{ fill: theme.palette.text.secondary, fontSize: 12 }}
              label={{
                value: "Speed (faster →)",
                position: "insideBottom",
                offset: -16,
                fill: theme.palette.text.secondary,
                fontSize: 12,
              }}
            />
            <YAxis
              type="number"
              dataKey="smoothness"
              name="Smoothness"
              domain={["dataMin - 2", "dataMax + 2"]}
              tick={{ fill: theme.palette.text.secondary, fontSize: 12 }}
              label={{
                value: "Smoothness (smoother ↑)",
                angle: -90,
                position: "insideLeft",
                fill: theme.palette.text.secondary,
                fontSize: 12,
                style: { textAnchor: "middle" },
              }}
            />
            <ZAxis range={[80, 80]} />
            <ReferenceLine x={xMid} stroke={theme.palette.divider} />
            <ReferenceLine y={yMid} stroke={theme.palette.divider} />
            <Tooltip content={<QuadrantTooltip />} cursor={{ strokeDasharray: "3 3" }} />
            {/* Confident profiles: colored by performance corner-affinity (green). */}
            <Scatter name="Confident" data={confident} fill={theme.palette.success.main}>
              {confident.map((p) => (
                <Cell key={p.fingerprint} fill={theme.palette.success.main} />
              ))}
            </Scatter>
            {/* Limited-data profiles: grey — not enough iterations to trust. */}
            <Scatter name="Limited data" data={limited} fill={greyColor}>
              {limited.map((p) => (
                <Cell key={p.fingerprint} fill={greyColor} />
              ))}
            </Scatter>
          </ScatterChart>
        </ResponsiveContainer>
      </Box>
      <Typography variant="caption" color="text.secondary">
        Top-right is best: <b>smoothest and fastest</b>. Green dots are confident profiles (≥{" "}
        {minIterations} iterations); grey dots don't yet have enough iterations to trust.
      </Typography>
    </Box>
  );
}
