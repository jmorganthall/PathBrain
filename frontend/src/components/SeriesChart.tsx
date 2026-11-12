import {
  Area,
  CartesianGrid,
  ComposedChart,
  Legend,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useTheme } from "@mui/material/styles";
import { fmtTimeShort } from "../utils/format";

export interface SeriesLine {
  key: string;
  name: string;
  color: string;
  /** Draw this line bold and quiet the others (DB6). Defaults to the `overall` key. */
  emphasis?: boolean;
}

export interface SeriesBand {
  lowKey: string;
  highKey: string;
  color: string;
  name?: string;
}

// Generic over any time-stamped row (legacy SeriesPoint, methodology axis-series, …).
interface Props<T extends { timestamp: string }> {
  data: readonly T[];
  lines: SeriesLine[];
  height?: number;
  yDomain?: [number | "auto", number | "auto"];
  unit?: string;
  /** Optional shaded range (e.g. per-run SOPS min/max) drawn behind the lines. */
  band?: SeriesBand;
}

export default function SeriesChart<T extends { timestamp: string }>({
  data,
  lines,
  height = 280,
  yDomain,
  unit,
  band,
}: Props<T>) {
  const theme = useTheme();
  const grid = "rgba(255,255,255,0.08)";
  const axis = theme.palette.text.secondary;

  // DB6: one highlighted series, the rest quieted. An explicit `emphasis` wins; otherwise
  // the Overall is the line that matters whenever it is drawn.
  const emphasised = new Set(
    lines.some((l) => l.emphasis) ? lines.filter((l) => l.emphasis).map((l) => l.key) : lines.filter((l) => l.key === "overall").map((l) => l.key),
  );
  const quiet = emphasised.size > 0 && emphasised.size < lines.length;

  const formatted = data.map((p) => {
    const rec = p as Record<string, unknown>;
    const row: Record<string, unknown> = { ...rec, _t: fmtTimeShort(p.timestamp) };
    if (band) {
      const low = rec[band.lowKey] as number | null | undefined;
      const high = rec[band.highKey] as number | null | undefined;
      row._band = low != null && high != null ? [low, high] : null;
    }
    return row;
  });

  return (
    <ResponsiveContainer width="100%" height={height}>
      <ComposedChart data={formatted} margin={{ top: 8, right: 16, bottom: 8, left: -8 }}>
        <CartesianGrid stroke={grid} strokeDasharray="3 3" />
        <XAxis dataKey="_t" stroke={axis} fontSize={12} tickMargin={8} />
        <YAxis stroke={axis} fontSize={12} domain={yDomain ?? ["auto", "auto"]} unit={unit} />
        <Tooltip
          contentStyle={{
            background: theme.palette.background.paper,
            border: `1px solid ${grid}`,
            borderRadius: 8,
          }}
          labelStyle={{ color: theme.palette.text.secondary }}
        />
        <Legend wrapperStyle={{ fontSize: 12 }} />
        {band && (
          <Area
            type="monotone"
            dataKey="_band"
            name={band.name ?? "variance"}
            stroke="none"
            fill={band.color}
            fillOpacity={0.18}
            connectNulls
            isAnimationActive={false}
            activeDot={false}
          />
        )}
        {lines.map((l) => (
          <Line
            key={String(l.key)}
            type="monotone"
            dataKey={l.key as string}
            name={l.name}
            stroke={l.color}
            strokeWidth={quiet ? (emphasised.has(l.key) ? 3 : 1.5) : 2}
            strokeOpacity={quiet && !emphasised.has(l.key) ? 0.45 : 1}
            dot={false}
            connectNulls
            isAnimationActive={false}
          />
        ))}
      </ComposedChart>
    </ResponsiveContainer>
  );
}
