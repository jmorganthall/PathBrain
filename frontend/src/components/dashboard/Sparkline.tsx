// A dependency-free SVG sparkline for stat tiles. Recharts is already on the Dashboard
// for the big series chart, but a sparkline is a single polyline and a dot: pulling a
// ResponsiveContainer + axes + tooltip machinery in for each tile would cost more
// layout work than the pixels it draws. Nulls break the line rather than being bridged,
// so a gap in the data reads as a gap.
import Box from "@mui/material/Box";

interface Props {
  values: Array<number | null | undefined>;
  color: string;
  width?: number;
  height?: number;
  // Fixed y-range (e.g. [0, 100] for scores) so tiles on the same scale compare; auto when
  // omitted.
  domain?: [number, number];
  // Emphasise the last point (the current reading) with a marker.
  dot?: boolean;
}

export default function Sparkline({
  values,
  color,
  width = 96,
  height = 28,
  domain,
  dot = true,
}: Props) {
  const nums = values.filter((v): v is number => v != null && !Number.isNaN(v));
  if (nums.length < 2) return <Box sx={{ width, height }} />;
  const lo = domain ? domain[0] : Math.min(...nums);
  const hi = domain ? domain[1] : Math.max(...nums);
  const span = hi - lo || 1;
  const pad = 2;
  const n = values.length;
  const x = (i: number) => pad + (i / Math.max(n - 1, 1)) * (width - pad * 2);
  const y = (v: number) => height - pad - ((v - lo) / span) * (height - pad * 2);

  // Split into segments at nulls so a missing reading is a visible break.
  const segments: string[] = [];
  let cur: string[] = [];
  values.forEach((v, i) => {
    if (v == null || Number.isNaN(v)) {
      if (cur.length) segments.push(cur.join(" "));
      cur = [];
      return;
    }
    cur.push(`${x(i).toFixed(1)},${y(v).toFixed(1)}`);
  });
  if (cur.length) segments.push(cur.join(" "));

  let lastIdx = -1;
  for (let i = n - 1; i >= 0; i--) {
    const v = values[i];
    if (v != null && !Number.isNaN(v)) {
      lastIdx = i;
      break;
    }
  }

  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} aria-hidden="true">
      {segments.map((pts, i) => (
        <polyline
          key={i}
          points={pts}
          fill="none"
          stroke={color}
          strokeWidth={1.5}
          strokeLinejoin="round"
          strokeLinecap="round"
          opacity={0.85}
        />
      ))}
      {dot && lastIdx >= 0 && (
        <circle cx={x(lastIdx)} cy={y(values[lastIdx] as number)} r={2.5} fill={color} />
      )}
    </svg>
  );
}
