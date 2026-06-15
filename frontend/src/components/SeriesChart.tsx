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
import type { SeriesPoint } from "../api/types";
import { fmtTimeShort } from "../utils/format";

export interface SeriesLine {
  key: keyof SeriesPoint;
  name: string;
  color: string;
}

export interface SeriesBand {
  lowKey: keyof SeriesPoint;
  highKey: keyof SeriesPoint;
  color: string;
  name?: string;
}

interface Props {
  data: SeriesPoint[];
  lines: SeriesLine[];
  height?: number;
  yDomain?: [number | "auto", number | "auto"];
  unit?: string;
  /** Optional shaded range (e.g. per-run SOPS min/max) drawn behind the lines. */
  band?: SeriesBand;
}

export default function SeriesChart({ data, lines, height = 280, yDomain, unit, band }: Props) {
  const theme = useTheme();
  const grid = "rgba(255,255,255,0.08)";
  const axis = theme.palette.text.secondary;

  const formatted = data.map((p) => {
    const row: Record<string, unknown> = { ...p, _t: fmtTimeShort(p.timestamp) };
    if (band) {
      const low = p[band.lowKey] as number | null | undefined;
      const high = p[band.highKey] as number | null | undefined;
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
            strokeWidth={2}
            dot={false}
            connectNulls
            isAnimationActive={false}
          />
        ))}
      </ComposedChart>
    </ResponsiveContainer>
  );
}
