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
import type { FieldDef } from "../utils/profileFields";
import { fmtFieldValue } from "../utils/profileFields";

// A scatter of profiles over any two numeric fields. When both axes are
// higher-is-better the top-right corner is "best" (the default Speed × Smoothness
// view); for lower-is-better fields the per-axis "↑/↓ better" hints make the good
// direction explicit. The crowned "best" profile is ringed so it's locatable in any
// projection. Confident profiles are coloured; limited-data ones are greyed.
interface Point {
  x: number;
  y: number;
  zRaw: number | null; // the third field's value → drives dot opacity (+ tooltip)
  label: string;
  fingerprint: string;
  iterations: number;
  confident: boolean;
  isBest: boolean;
  isActive: boolean; // currently live on the firewall → drawn as a triangle
}

function median(xs: number[]): number {
  if (!xs.length) return 0;
  const s = [...xs].sort((a, b) => a - b);
  const m = Math.floor(s.length / 2);
  return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
}

function QuadrantTooltip({
  active,
  payload,
  xField,
  yField,
  sizeField,
}: {
  active?: boolean;
  payload?: Array<{ payload: Point }>;
  xField: FieldDef;
  yField: FieldDef;
  sizeField?: FieldDef | null;
}) {
  if (!active || !payload || !payload.length) return null;
  const p = payload[0].payload;
  return (
    <Box sx={{ bgcolor: "background.paper", border: 1, borderColor: "divider", borderRadius: 1, p: 1 }}>
      <Typography variant="caption" sx={{ display: "block", fontWeight: 700, wordBreak: "break-word" }}>
        {p.label}
        {p.isBest ? " · best" : ""}
        {p.isActive ? " · active" : ""}
      </Typography>
      <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
        {yField.label} {fmtFieldValue(p.y, yField.unit)} · {xField.label} {fmtFieldValue(p.x, xField.unit)}
      </Typography>
      {sizeField && sizeField.key !== xField.key && sizeField.key !== yField.key && (
        <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
          {sizeField.label} {p.zRaw == null ? "—" : fmtFieldValue(p.zRaw, sizeField.unit)} (opacity)
        </Typography>
      )}
      <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
        {p.iterations} iteration(s){p.confident ? "" : " — limited data"}
      </Typography>
    </Box>
  );
}

function arrow(higher: boolean) {
  return higher ? "↑ better" : "↓ better";
}

export default function ProfileQuadrant({
  profiles,
  xField,
  yField,
  shadeField,
  bestFingerprint,
  currentFingerprint,
}: {
  profiles: SettingsProfile[];
  xField: FieldDef;
  yField: FieldDef;
  shadeField?: FieldDef | null;
  bestFingerprint: string | null;
  currentFingerprint?: string | null;
}) {
  const theme = useTheme();
  // The third dimension drives dot OPACITY (better = fully opaque, worse fades out) —
  // only when it's a distinct field from the two plotted axes (otherwise it'd just
  // restate an axis). Opacity reads with any number of points, unlike bubble size.
  const shadeOn =
    shadeField != null && shadeField.key !== xField.key && shadeField.key !== yField.key;
  const points: Point[] = profiles
    .map((p) => ({ p, x: xField.get(p), y: yField.get(p) }))
    .filter((r): r is { p: SettingsProfile; x: number; y: number } => r.x != null && r.y != null)
    .map(({ p, x, y }) => ({
      x,
      y,
      zRaw: shadeOn ? shadeField!.get(p) ?? null : null,
      label: p.label,
      fingerprint: p.fingerprint,
      iterations: p.iterations,
      confident: p.confident,
      isBest: p.fingerprint === bestFingerprint,
      isActive: currentFingerprint != null && p.fingerprint === currentFingerprint,
    }));

  if (points.length < 2) {
    return (
      <Typography variant="caption" color="text.secondary">
        Need at least two profiles with both “{xField.label}” and “{yField.label}” to plot the quadrant.
      </Typography>
    );
  }

  // The live profile is drawn separately as a triangle; everyone else as circles.
  const active = points.filter((p) => p.isActive);
  const confident = points.filter((p) => p.confident && !p.isActive);
  const limited = points.filter((p) => !p.confident && !p.isActive);
  const xMid = median(points.map((p) => p.x));
  const yMid = median(points.map((p) => p.y));
  const greyColor = theme.palette.text.disabled;
  const goodColor = theme.palette.success.main;
  const bestColor = theme.palette.warning.main;
  const bothHigher = xField.higherIsBetter && yField.higherIsBetter;

  const cellColor = (p: Point) =>
    p.isBest ? bestColor : p.confident ? goodColor : greyColor;

  // Opacity encodes the third field: best on it = fully opaque, worst fades to 15%.
  // We spread by **rank** (percentile), not raw value, so a tight cluster of scores
  // (e.g. 85–88) still separates clearly instead of all looking near-opaque — and one
  // low outlier doesn't squash everyone else. Honours the field's "better" direction;
  // the crowned dot is always full opacity.
  const MIN_OPACITY = 0.15;
  const shadeVals = points.map((p) => p.zRaw).filter((v): v is number => v != null);
  const sorted = [...shadeVals].sort((a, b) => a - b);
  const n = sorted.length;
  // value → averaged 0..1 rank fraction (ties share a rank).
  const fracByVal = new Map<number, number>();
  for (let i = 0; i < n; ) {
    let j = i;
    while (j < n && sorted[j] === sorted[i]) j++;
    fracByVal.set(sorted[i], n > 1 ? (i + j - 1) / 2 / (n - 1) : 1);
    i = j;
  }
  const opacityOf = (p: Point): number => {
    if (!shadeOn || p.isBest || p.isActive) return 1; // always show best + active clearly
    if (p.zRaw == null) return MIN_OPACITY; // no value on the third axis → faint
    if (n <= 1) return 1; // nothing to rank against
    const frac = fracByVal.get(p.zRaw) ?? 0; // 0 = lowest rank, 1 = highest
    const good = shadeField!.higherIsBetter ? frac : 1 - frac;
    return MIN_OPACITY + (1 - MIN_OPACITY) * good;
  };

  return (
    <Box>
      <Box sx={{ width: "100%", height: 360 }}>
        <ResponsiveContainer>
          <ScatterChart margin={{ top: 16, right: 24, bottom: 36, left: 12 }}>
            <CartesianGrid strokeDasharray="3 3" stroke={theme.palette.divider} />
            <XAxis
              type="number"
              dataKey="x"
              name={xField.label}
              domain={["dataMin", "dataMax"]}
              tick={{ fill: theme.palette.text.secondary, fontSize: 12 }}
              label={{
                value: `${xField.label} (${arrow(xField.higherIsBetter)})`,
                position: "insideBottom",
                offset: -18,
                fill: theme.palette.text.secondary,
                fontSize: 12,
              }}
            />
            <YAxis
              type="number"
              dataKey="y"
              name={yField.label}
              domain={["dataMin", "dataMax"]}
              tick={{ fill: theme.palette.text.secondary, fontSize: 12 }}
              label={{
                value: `${yField.label} (${arrow(yField.higherIsBetter)})`,
                angle: -90,
                position: "insideLeft",
                fill: theme.palette.text.secondary,
                fontSize: 12,
                style: { textAnchor: "middle" },
              }}
            />
            {/* Uniform dot size — the third field is encoded as opacity, not size. */}
            <ZAxis range={[90, 90]} />
            <ReferenceLine x={xMid} stroke={theme.palette.divider} />
            <ReferenceLine y={yMid} stroke={theme.palette.divider} />
            <Tooltip
              content={<QuadrantTooltip xField={xField} yField={yField} sizeField={shadeField} />}
              cursor={{ strokeDasharray: "3 3" }}
            />
            <Scatter name="Confident" data={confident} fill={goodColor}>
              {confident.map((p) => (
                <Cell
                  key={p.fingerprint}
                  fill={cellColor(p)}
                  fillOpacity={opacityOf(p)}
                  stroke={p.isBest ? theme.palette.warning.light : undefined}
                  strokeWidth={p.isBest ? 3 : 0}
                />
              ))}
            </Scatter>
            <Scatter name="Limited data" data={limited} fill={greyColor}>
              {limited.map((p) => (
                <Cell key={p.fingerprint} fill={greyColor} fillOpacity={opacityOf(p)} />
              ))}
            </Scatter>
            {/* The live-on-the-firewall profile: a triangle so it stands out among the
                circles, regardless of its position/score. Drawn last (on top). */}
            <Scatter name="Active" data={active} shape="triangle" fill={goodColor}>
              {active.map((p) => (
                <Cell
                  key={p.fingerprint}
                  fill={cellColor(p)}
                  fillOpacity={1}
                  stroke={theme.palette.common.white}
                  strokeWidth={1.5}
                />
              ))}
            </Scatter>
          </ScatterChart>
        </ResponsiveContainer>
      </Box>
      <Typography variant="caption" color="text.secondary">
        {bothHigher ? (
          <>
            Top-right is best: <b>high {xField.label} and high {yField.label}</b>. The{" "}
            <b style={{ color: bestColor }}>ringed</b> dot is the crowned profile (closest to the
            ideal corner); grey dots don’t yet have enough iterations to trust.
          </>
        ) : (
          <>
            Each axis is labelled with its “better” direction. The{" "}
            <b style={{ color: bestColor }}>ringed</b> dot is the crowned profile (best on
            Speed × Smoothness); grey dots are limited-data profiles.
          </>
        )}
        {shadeOn ? <> Opacity = <b>{shadeField!.label}</b> (brighter = better; faded = worse).</> : null}
        {active.length > 0 ? <> The <b>▲ triangle</b> is the profile live on the firewall now.</> : null}
      </Typography>
    </Box>
  );
}
