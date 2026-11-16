// Colors for axis lines and gauges (amber = responsiveness, cyan = speed, violet =
// smoothness, …), fixed per axis and never cycled. A key not in the map (a metric key,
// or an axis a later methodology adds) takes a distinct colour by position: falling back
// to one colour drew several lines in the same cyan. Keyed on index rather than hashed
// off the key, because a hash gives two keys near-identical hues often enough to matter,
// and position is stable within a render.
export const AXIS_COLORS: Record<string, string> = {
  overall: "#e8eaed",
  responsiveness: "#f5a65b",
  speed: "#6fd0dc",
  smoothness: "#c58af9",
  stability: "#9fd5a6",
  completion: "#9aa6b2",
};

export const SERIES_COLORS = ["#6fd0dc", "#f5a65b", "#c58af9", "#9fd5a6", "#f48fb1", "#9aa6b2"];

export const axisColor = (key: string, index = 0) =>
  AXIS_COLORS[key] ?? SERIES_COLORS[index % SERIES_COLORS.length];
