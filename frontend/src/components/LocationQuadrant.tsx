// The location map — the Settings-Impact quadrant asked of PLACES instead of profiles.
//
// The Away test's readout was one run on one device against that device's own home runs.
// That answers "how did this hotel do?" and nothing wider: a person who has measured six
// networks over a month has six separate answers and no picture. This is the picture. Each
// dot is one network — every run taken there, on any device, pooled to a median — placed on
// any two portable metrics beside HOME on the profile the firewall is on now. The reference
// lines run through the home dot rather than the field's median, because the question the
// chart answers is "where does each place stand against home?", so the quadrants are
// relative to home: right-and-up of it is better on both axes (when both axes read that
// way), and each axis is labelled with its better direction.
//
// Same grammar as the profile quadrant so the eye carries over: the home dot is RINGED (as
// the crown is there), PathBrain's own wired readings are the TRIANGLE (a different device
// class, kept apart as everywhere else on this instrument), a place with too few runs is
// GREY, and a third metric is dot OPACITY by rank. One thing the profile chart never had to
// say: a setup-bound axis (first/largest/waterfall complete, byte earliness) is only
// comparable between pools whose connection warmth matches, so a place that fails that test
// is drawn with a dashed outline and named in the caption when such an axis is plotted.
import { useEffect, useMemo, useState } from "react";
import {
  CartesianGrid,
  Cell,
  LabelList,
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
import Chip from "@mui/material/Chip";
import FormControl from "@mui/material/FormControl";
import InputLabel from "@mui/material/InputLabel";
import ListSubheader from "@mui/material/ListSubheader";
import MenuItem from "@mui/material/MenuItem";
import Select from "@mui/material/Select";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";

import Button from "@mui/material/Button";

import type { PortableCrownLeg, PortableLocation, PortableLocationMap, PortableLocationMetric } from "../api/types";
import { fmtFieldValue } from "../utils/profileFields";

export interface LocationField {
  key: string;
  label: string;
  unit: string;
  higherIsBetter: boolean;
  group: string;
  setupBound: boolean;
  get: (loc: PortableLocation) => number | null;
}

const X_KEY = "pathbrain.portable.map.x";
const Y_KEY = "pathbrain.portable.map.y";
const SHADE_KEY = "pathbrain.portable.map.shade";
// The defaults are the CROWN's legs translated onto this instrument (`map.crown.legs`:
// X = the first leg's stand-in, Y = the second's, Shade = the stand-in score when every leg
// has one, else the third leg) — so the map opens on what the methodology crowns on now and
// re-points itself after a publish. These are only the fallback for a crown with no legs.
const DEFAULT_X = "rtt_ms";
const DEFAULT_Y = "last_complete_ms";
const DEFAULT_SHADE = "score";

function crownDefaults(legs: PortableCrownLeg[], complete: boolean): { x: string; y: string; shade: string } {
  const keys = legs.map((l) => l.portable_metric).filter((k): k is string => !!k);
  return {
    x: keys[0] ?? DEFAULT_X,
    y: keys[1] ?? DEFAULT_Y,
    shade: complete ? "crown_score" : keys[2] ?? DEFAULT_SHADE,
  };
}

function groupOf(key: string): string {
  if (key === "score" || key === "crown_score") return "Score";
  if (key.startsWith("stream_") || key === "throughput_mbps") return "Stream";
  if (key === "rtt_ms" || key === "jitter_ms") return "Round trip";
  if (key === "interleave_index" || key === "bulk_share" || key === "small_under_large_ms") return "Burst fairness";
  return "Waterfall";
}

export function locationFields(metrics: PortableLocationMetric[], legs: PortableCrownLeg[] = []): LocationField[] {
  const standsFor = new Map<string, string>();
  for (const leg of legs) if (leg.portable_metric) standsFor.set(leg.portable_metric, leg.crown_label);
  return metrics.map((m) => ({
    key: m.key,
    label: standsFor.has(m.key) ? `${m.label} (for ${standsFor.get(m.key)})` : m.label,
    unit: m.unit,
    higherIsBetter: !m.lower_is_better,
    group: groupOf(m.key),
    setupBound: m.setup_bound,
    get: (loc) => (m.key === "score" ? loc.score : m.key === "crown_score" ? loc.crown_score : loc.metrics[m.key] ?? null),
  }));
}

function readKey(storage: string, fallback: string, fields: LocationField[]): string {
  try {
    const v = localStorage.getItem(storage);
    if (v && fields.some((f) => f.key === v)) return v;
  } catch {
    /* storage unavailable */
  }
  return fields.some((f) => f.key === fallback) ? fallback : fields[0]?.key ?? fallback;
}

function AxisSelect({
  label,
  value,
  fields,
  onChange,
}: {
  label: string;
  value: string;
  fields: LocationField[];
  onChange: (key: string) => void;
}) {
  const groups: { name: string; items: LocationField[] }[] = [];
  for (const f of fields) {
    const g = groups.find((x) => x.name === f.group);
    if (g) g.items.push(f);
    else groups.push({ name: f.group, items: [f] });
  }
  return (
    <FormControl size="small" sx={{ minWidth: 170, flex: 1 }}>
      <InputLabel>{label}</InputLabel>
      <Select label={label} value={value} onChange={(e) => onChange(e.target.value)}>
        {groups.flatMap((g) => [
          <ListSubheader key={`h-${g.name}`}>{g.name}</ListSubheader>,
          ...g.items.map((f) => (
            <MenuItem key={f.key} value={f.key}>
              {f.label}
            </MenuItem>
          )),
        ])}
      </Select>
    </FormControl>
  );
}

interface Point {
  x: number;
  y: number;
  zRaw: number | null;
  name: string;
  loc: PortableLocation;
}

// Pad an axis so a dot at the extreme is drawn whole rather than half off the plot; a
// flat axis (every place equal) still gets a visible band.
function padded(values: number[]): [number, number] {
  const lo = Math.min(...values);
  const hi = Math.max(...values);
  const pad = hi > lo ? (hi - lo) * 0.1 : Math.max(Math.abs(hi) * 0.1, 1);
  return [lo - pad, hi + pad];
}

// A plain <text> label: recharts' default label inherits the dot's stroke (a dashed dot
// gave a dashed name) and wraps long names into the plot edge.
const dotLabel = (color: string, bold = false) =>
  function DotLabel(props: { x?: number | string; y?: number | string; value?: string | number }) {
    const x = Number(props.x ?? 0);
    const y = Number(props.y ?? 0);
    return (
      <text x={x} y={y - 10} textAnchor="middle" fontSize={11} fontWeight={bold ? 700 : 400} fill={color} stroke="none">
        {props.value}
      </text>
    );
  };

function arrow(higher: boolean) {
  return higher ? "↑ better" : "↓ better";
}

function MapTooltip({
  active,
  payload,
  xField,
  yField,
  shadeField,
}: {
  active?: boolean;
  payload?: Array<{ payload: Point }>;
  xField: LocationField;
  yField: LocationField;
  shadeField: LocationField | null;
}) {
  if (!active || !payload || !payload.length) return null;
  const p = payload[0].payload;
  const loc = p.loc;
  const devices = loc.devices.map((d) => `${d.label || d.device_id.slice(0, 8)} ×${d.runs}`).join(", ");
  return (
    <Box sx={{ bgcolor: "background.paper", border: 1, borderColor: "divider", borderRadius: 1, p: 1, maxWidth: 300 }}>
      <Typography variant="caption" sx={{ display: "block", fontWeight: 700, overflowWrap: "anywhere" }}>
        {loc.label}
        {loc.kind === "home" ? " · home" : loc.kind === "home_server" ? " · wired" : ""}
      </Typography>
      <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
        {yField.label} {fmtFieldValue(p.y, yField.unit)} · {xField.label} {fmtFieldValue(p.x, xField.unit)}
      </Typography>
      {shadeField && (
        <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
          {shadeField.label} {p.zRaw == null ? "—" : fmtFieldValue(p.zRaw, shadeField.unit)} (opacity)
        </Typography>
      )}
      <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
        {loc.runs} run{loc.runs === 1 ? "" : "s"}{loc.confident ? "" : " — limited data"} · {devices}
      </Typography>
      {loc.setup_comparable === false && loc.setup_note && (
        <Typography variant="caption" color="warning.main" sx={{ display: "block" }}>
          Setup-bound metrics not comparable with home: {loc.setup_note}.
        </Typography>
      )}
    </Box>
  );
}

const fmtDelta = (v: number | null, unit: string, higher: boolean): { text: string; good: boolean | null } => {
  if (v == null) return { text: "—", good: null };
  if (v === 0) return { text: "level", good: null };
  const sign = v > 0 ? "+" : "−";
  return { text: `${sign}${fmtFieldValue(Math.abs(v), unit)}`, good: higher ? v > 0 : v < 0 };
};

export default function LocationQuadrant({ map }: { map: PortableLocationMap }) {
  const theme = useTheme();
  const crown = map.crown;
  const fields = useMemo(() => locationFields(map.metrics, crown.legs), [map.metrics, crown.legs]);
  const crownAxes = useMemo(() => crownDefaults(map.crown.legs, map.crown.complete), [map.crown]);
  const [xKey, setXKey] = useState(() => readKey(X_KEY, crownAxes.x, fields));
  const [yKey, setYKey] = useState(() => readKey(Y_KEY, crownAxes.y, fields));
  const [shadeKey, setShadeKey] = useState(() => readKey(SHADE_KEY, crownAxes.shade, fields));
  const onCrownAxes = xKey === crownAxes.x && yKey === crownAxes.y && shadeKey === crownAxes.shade;
  const useCrownAxes = () => {
    setXKey(crownAxes.x);
    setYKey(crownAxes.y);
    setShadeKey(crownAxes.shade);
  };
  useEffect(() => {
    try {
      localStorage.setItem(X_KEY, xKey);
      localStorage.setItem(Y_KEY, yKey);
      localStorage.setItem(SHADE_KEY, shadeKey);
    } catch {
      /* storage unavailable */
    }
  }, [xKey, yKey, shadeKey]);

  const byKey = (k: string) => fields.find((f) => f.key === k) ?? fields[0];
  const xField = byKey(xKey);
  const yField = byKey(yKey);
  const shadeCandidate = byKey(shadeKey);
  const shadeField = shadeCandidate.key !== xField.key && shadeCandidate.key !== yField.key ? shadeCandidate : null;

  const points: Point[] = map.locations
    .map((loc) => ({ loc, x: xField.get(loc), y: yField.get(loc) }))
    .filter((r): r is { loc: PortableLocation; x: number; y: number } => r.x != null && r.y != null)
    .map(({ loc, x, y }) => ({
      x, y, zRaw: shadeField ? shadeField.get(loc) : null, loc,
      name: loc.kind === "home" ? "Home" : loc.kind === "home_server" ? "wired" : loc.label,
    }));
  const xDomain = points.length ? padded(points.map((p) => p.x)) : undefined;
  const yDomain = points.length ? padded(points.map((p) => p.y)) : undefined;
  const home = points.find((p) => p.loc.kind === "home") ?? null;
  const homeAny = home ?? points.find((p) => p.loc.kind === "home_server") ?? null;

  const greyColor = theme.palette.text.disabled;
  const goodColor = theme.palette.success.main;
  const homeColor = theme.palette.warning.main;
  const setupAxis = xField.setupBound || yField.setupBound || !!shadeField?.setupBound;
  const incomparable = setupAxis ? points.filter((p) => p.loc.kind === "away" && p.loc.setup_comparable === false) : [];

  // Opacity by RANK on the shade field (ties share a rank), honouring its better direction;
  // home is always fully opaque so the reference never fades.
  const MIN_OPACITY = 0.15;
  const shadeVals = points.map((p) => p.zRaw).filter((v): v is number => v != null);
  const sorted = [...shadeVals].sort((a, b) => a - b);
  const n = sorted.length;
  const fracByVal = new Map<number, number>();
  for (let i = 0; i < n; ) {
    let j = i;
    while (j < n && sorted[j] === sorted[i]) j++;
    fracByVal.set(sorted[i], n > 1 ? (i + j - 1) / 2 / (n - 1) : 1);
    i = j;
  }
  const opacityOf = (p: Point): number => {
    if (!shadeField || p.loc.kind !== "away") return 1;
    if (p.zRaw == null) return MIN_OPACITY;
    if (n <= 1) return 1;
    const frac = fracByVal.get(p.zRaw) ?? 0;
    const good = shadeField.higherIsBetter ? frac : 1 - frac;
    return MIN_OPACITY + (1 - MIN_OPACITY) * good;
  };
  const strokeOf = (p: Point) =>
    p.loc.kind === "home" ? homeColor : p.loc.setup_comparable === false && setupAxis ? theme.palette.warning.light : undefined;

  const away = points.filter((p) => p.loc.kind === "away");
  const server = points.filter((p) => p.loc.kind === "home_server");
  const homes = points.filter((p) => p.loc.kind === "home");
  const bothHigher = xField.higherIsBetter && yField.higherIsBetter;

  // The table pins the CROWN legs, as Settings Impact pins the crown metrics: the chart
  // already shows the two plotted axes, and the columns should be the readings the verdict
  // rests on whatever the axes are set to. "vs home" is on the crown stand-in score when it
  // exists, else on the portable score.
  const rows = map.locations;
  const legFields = crown.legs
    .map((leg) => (leg.portable_metric ? fields.find((f) => f.key === leg.portable_metric) : undefined))
    .filter((f): f is LocationField => !!f);
  const headline: LocationField = crown.complete
    ? { key: "crown_score", label: "Crown stand-in", unit: "score", higherIsBetter: true, group: "Score", setupBound: false, get: (l) => l.crown_score }
    : { key: "score", label: "Score", unit: "score", higherIsBetter: true, group: "Score", setupBound: false, get: (l) => l.score };
  const homeRow = rows.find((l) => l.kind === "home") ?? null;
  const homeHeadline = homeRow ? headline.get(homeRow) : null;

  const wiring = crown.legs.length > 0 && (
    <Typography variant="caption" color="text.secondary" component="div" sx={{ mb: 1 }}>
      Wired to <b>{crown.methodology}</b>, which crowns on{" "}
      {crown.legs.map((leg, i) => (
        <span key={leg.crown_metric}>
          {i > 0 ? " × " : ""}
          <b>{leg.crown_label}</b>
          {leg.portable_label ? (
            <> → {leg.portable_label}</>
          ) : (
            <> → <span style={{ color: theme.palette.warning.main }}>no stand-in on this instrument</span></>
          )}
        </span>
      ))}
      . A browser tab can't read a real page's paint timing, so each leg is read from the nearest thing the
      synthetic waterfall measures; the crown stand-in score applies the methodology's own weights to those
      readings.
      {!onCrownAxes && (
        <>
          {" "}
          <Button size="small" variant="text" onClick={useCrownAxes} sx={{ py: 0, minWidth: 0, textTransform: "none" }}>
            Show the crown axes
          </Button>
        </>
      )}
    </Typography>
  );
  const controls = (
    <>
      {wiring}
      <Stack direction={{ xs: "column", sm: "row" }} spacing={1} sx={{ mb: 1 }}>
        <AxisSelect label="X axis" value={xField.key} fields={fields} onChange={setXKey} />
        <AxisSelect label="Y axis" value={yField.key} fields={fields} onChange={setYKey} />
        <AxisSelect label="Shade" value={shadeCandidate.key} fields={fields} onChange={setShadeKey} />
      </Stack>
    </>
  );

  if (points.length < 2) {
    return (
      <Box>
        {controls}
        <Typography variant="caption" color="text.secondary">
          Need home and at least one other place with both “{xField.label}” and “{yField.label}” to draw the map
          {map.locations.length < 2 ? " — run the Away test somewhere that isn't home." : "."}
        </Typography>
      </Box>
    );
  }

  return (
    <Box>
      {controls}
      {incomparable.length > 0 && (
        <Box sx={{ mb: 1, px: 1, py: 0.75, borderRadius: 1, bgcolor: "warning.dark", color: "warning.contrastText" }}>
          <Typography variant="caption" component="span" sx={{ fontWeight: 700 }}>
            ⚠ Setup-bound axis:{" "}
          </Typography>
          <Typography variant="caption" component="span" sx={{ opacity: 0.9 }}>
            {incomparable.map((p) => p.loc.label).join(", ")} {incomparable.length === 1 ? "was" : "were"} measured with
            different connection warmth from home (a warm tab skips the handshakes a cold one pays), so on{" "}
            {[xField, yField, shadeField].filter((f): f is LocationField => !!f && f.setupBound).map((f) => f.label).join(" / ")}{" "}
            {incomparable.length === 1 ? "its" : "their"} dashed dot{incomparable.length === 1 ? "" : "s"} should not be read
            against home. Round trip, jitter, stall and stream metrics are unaffected.
          </Typography>
        </Box>
      )}
      <Box sx={{ width: "100%", height: 340 }}>
        <ResponsiveContainer>
          <ScatterChart margin={{ top: 28, right: 32, bottom: 36, left: 12 }}>
            <CartesianGrid strokeDasharray="3 3" stroke={theme.palette.divider} />
            <XAxis
              type="number"
              dataKey="x"
              name={xField.label}
              domain={xDomain ?? ["dataMin", "dataMax"]}
              tickFormatter={(v: number) => fmtFieldValue(v, "")}
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
              domain={yDomain ?? ["dataMin", "dataMax"]}
              tickFormatter={(v: number) => fmtFieldValue(v, "")}
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
            <ZAxis range={[90, 90]} />
            {/* The quadrants are relative to HOME, not the field's median. */}
            {homeAny && <ReferenceLine x={homeAny.x} stroke={homeColor} strokeOpacity={0.5} />}
            {homeAny && <ReferenceLine y={homeAny.y} stroke={homeColor} strokeOpacity={0.5} />}
            <Tooltip content={<MapTooltip xField={xField} yField={yField} shadeField={shadeField} />} cursor={{ strokeDasharray: "3 3" }} />
            <Scatter name="Places" data={away} fill={goodColor}>
              {away.map((p) => (
                <Cell
                  key={p.loc.key}
                  fill={p.loc.confident ? goodColor : greyColor}
                  fillOpacity={opacityOf(p)}
                  stroke={strokeOf(p)}
                  strokeWidth={strokeOf(p) ? 2 : 0}
                  strokeDasharray={strokeOf(p) ? "3 3" : undefined}
                />
              ))}
              <LabelList dataKey="name" content={dotLabel(theme.palette.text.secondary)} />
            </Scatter>
            <Scatter name="Home" data={homes} fill={homeColor}>
              {homes.map((p) => (
                <Cell key={p.loc.key} fill={homeColor} fillOpacity={1} stroke={theme.palette.common.white} strokeWidth={2.5} />
              ))}
              <LabelList dataKey="name" content={dotLabel(homeColor, true)} />
            </Scatter>
            <Scatter name="Home (wired)" data={server} shape="triangle" fill={homeColor}>
              {server.map((p) => (
                <Cell key={p.loc.key} fill={homeColor} fillOpacity={0.9} stroke={theme.palette.common.white} strokeWidth={1.5} />
              ))}
              <LabelList dataKey="name" content={dotLabel(theme.palette.text.secondary)} />
            </Scatter>
          </ScatterChart>
        </ResponsiveContainer>
      </Box>
      <Typography variant="caption" color="text.secondary" component="div" sx={{ mb: 1.5 }}>
        The <b style={{ color: homeColor }}>ringed</b> dot is home on the current profile, and the lines run through it:{" "}
        {bothHigher ? (
          <>right-and-up of it is better on both axes.</>
        ) : (
          <>each axis says which side of it is better.</>
        )}
        {server.length > 0 ? <> The <b>▲ triangle</b> is PathBrain's own wired reading of home, a different device class.</> : null}
        {" "}Grey dots have fewer than {map.min_location_runs} runs.
        {shadeField ? <> Opacity = <b>{shadeField.label}</b> (brighter = better).</> : null}
      </Typography>

      <Box sx={{ overflowX: "auto" }}>
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>Location</TableCell>
              <TableCell align="right">Runs</TableCell>
              <TableCell align="right" sx={{ whiteSpace: "nowrap" }}>{headline.label}</TableCell>
              <TableCell align="right" sx={{ whiteSpace: "nowrap" }}>vs home</TableCell>
              {legFields.map((f) => (
                <TableCell key={f.key} align="right" sx={{ whiteSpace: "nowrap" }}>
                  {f.label}
                </TableCell>
              ))}
              {crown.complete ? <TableCell align="right">Score</TableCell> : null}
              <TableCell>Devices</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {rows.map((loc) => {
              const mine = headline.get(loc);
              const delta = loc.kind === "home" || homeHeadline == null || mine == null ? null : mine - homeHeadline;
              const d = fmtDelta(delta, "score", true);
              return (
                <TableRow key={loc.key} sx={{ opacity: loc.confident ? 1 : 0.7 }}>
                  <TableCell sx={{ whiteSpace: "nowrap" }}>
                    {loc.kind === "home" ? <b>{loc.label}</b> : loc.label}
                    {loc.kind === "home_server" ? <Chip size="small" label="wired" sx={{ ml: 0.5, height: 18 }} /> : null}
                    {!loc.confident ? <Chip size="small" variant="outlined" label="thin" sx={{ ml: 0.5, height: 18 }} /> : null}
                  </TableCell>
                  <TableCell align="right">{loc.runs}</TableCell>
                  <TableCell align="right">{mine == null ? "—" : Math.round(mine)}</TableCell>
                  <TableCell align="right" sx={{ color: d.good == null ? "text.secondary" : d.good ? "success.main" : "error.main" }}>
                    {loc.kind === "home" ? "reference" : d.text}
                  </TableCell>
                  {legFields.map((f) => (
                    <TableCell key={f.key} align="right">
                      {fmtFieldValue(f.get(loc), f.unit)}
                    </TableCell>
                  ))}
                  {crown.complete ? <TableCell align="right">{loc.score == null ? "—" : Math.round(loc.score)}</TableCell> : null}
                  <TableCell sx={{ color: "text.secondary", whiteSpace: "nowrap" }}>
                    {loc.devices.map((dv) => `${dv.label || dv.device_id.slice(0, 8)} ×${dv.runs}`).join(", ")}
                  </TableCell>
                </TableRow>
              );
            })}
          </TableBody>
        </Table>
      </Box>
    </Box>
  );
}
