import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip as RTooltip,
  XAxis,
  YAxis,
} from "recharts";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import MenuItem from "@mui/material/MenuItem";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import { useTheme } from "@mui/material/styles";

import { api } from "../api/client";
import type { TrendHeatmapResponse, TrendRelativeResponse } from "../api/types";
import TrendHeatmap from "../components/TrendHeatmap";
import RelativeDelta, { fmtBucket } from "../components/RelativeDelta";
import Loading from "../components/Loading";
import PageHeader from "../components/PageHeader";
import { useMetricMeta } from "../utils/metrics";
import { useMethodology } from "../utils/methodology";

// The headline Overall always leads; the crown measurements (what we rank on today) come from
// the relative response, then the methodology's axes and its other scored metrics, all read
// from the current methodology so the picker follows it. (The probe metrics that used to
// close the list, DNS/TCP/TLS/latency/jitter, are no longer collected by default runs.)
const FALLBACK_AXES: { key: string; label: string }[] = [
  { key: "responsiveness", label: "Responsiveness" },
  { key: "smoothness", label: "Smoothness" },
  { key: "speed", label: "Speed" },
];

export default function Trends() {
  const theme = useTheme();
  const meta = useMetricMeta();
  const methodology = useMethodology();
  const [metric, setMetric] = useState("overall");
  const [heatmap, setHeatmap] = useState<TrendHeatmapResponse | null>(null);
  const [relative, setRelative] = useState<TrendRelativeResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // The crown measurements (fcp/lcp/total_stall today) come from the methodology via the
  // relative response, so the picker + "vs typical" panel feature exactly what we rank on now.
  const crownOptions = useMemo(
    () => (relative?.crown_metrics ?? []).map((k) => ({ key: k, label: meta(k).label })),
    [relative, meta],
  );
  // Metric picker: Overall → crown measurements → the axes that score something → the other
  // scored metrics. Deduped by key (first wins) so a crown metric can't double up.
  const metricOptions = useMemo(() => {
    const scored = (methodology?.definition.metrics ?? []).filter((m) => m.weight > 0);
    const axisOptions = methodology
      ? methodology.definition.axes
          .filter((a) => scored.some((m) => m.axis === a.key))
          .map((a) => ({ key: a.key, label: a.label }))
      : FALLBACK_AXES;
    const scoredOptions = scored
      .map((m) => ({ key: m.key, label: m.label }));
    const all = [{ key: "overall", label: "Overall" }, ...crownOptions, ...axisOptions, ...scoredOptions];
    const seen = new Set<string>();
    return all.filter((o) => (seen.has(o.key) ? false : seen.add(o.key)));
  }, [crownOptions, methodology]);
  // "Right now vs typical" panel keys, same headline-first ordering.
  const relativeKeys = useMemo(() => [...new Set(metricOptions.map((o) => o.key))], [metricOptions]);

  const loadHeatmap = useCallback(async (m: string) => {
    const h = await api.trendsHeatmap(m);
    setHeatmap(h);
  }, []);

  useEffect(() => {
    setLoading(true);
    setError(null);
    Promise.all([loadHeatmap(metric), api.trendsRelative().then(setRelative)])
      .catch((e) => setError(e instanceof Error ? e.message : "Failed to load trends"))
      .finally(() => setLoading(false));
  }, [metric, loadHeatmap]);

  const nowBucket = relative ? fmtBucket(relative.weekday, relative.hour) : null;

  const hourChart = heatmap
    ? heatmap.by_hour.map((b) => ({
        hour: `${b.hour}:00`,
        median: b.median,
        band: [b.p25, b.p75] as [number, number],
      }))
    : [];

  return (
    <Box>
      <PageHeader
        title="Trends"
        lead="What is normal for each metric by day of week and hour of day, and how right now compares."
        compact
        actions={
          <TextField
            select
            size="small"
            label="Metric"
            value={metric}
            onChange={(e) => setMetric(e.target.value)}
            sx={{ minWidth: 200 }}
          >
            {metricOptions.map((o) => (
              <MenuItem key={o.key} value={o.key}>
                {o.label}
              </MenuItem>
            ))}
          </TextField>
        }
      />

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {loading && !heatmap ? (
        <Loading label="Loading trends…" />
      ) : (
        <Box
          sx={{
            display: "grid",
            gap: 2,
            gridTemplateColumns: { xs: "1fr", lg: "2fr 1fr" },
          }}
        >
          <Card>
            <CardContent>
              <Typography variant="h6" gutterBottom>
                {heatmap?.label ?? "Metric"} by day &amp; hour
              </Typography>
              {heatmap && heatmap.total > 0 ? (
                <Box sx={{ overflowX: "auto" }}>
                  <TrendHeatmap
                    data={heatmap}
                    nowWeekday={relative?.weekday}
                    nowHour={relative?.hour}
                  />
                </Box>
              ) : (
                <Typography variant="body2" color="text.secondary">
                  Not enough history yet for this metric. Trends build up as monitoring
                  runs accumulate.
                </Typography>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardContent>
              <Typography variant="h6">Right now vs. typical</Typography>
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1 }}>
                {nowBucket ? `Baseline for ${nowBucket}` : "Current reading vs. historical baseline"}
              </Typography>
              {relative && Object.keys(relative.metrics).length > 0 ? (
                <Box>
                  {relativeKeys.filter((k) => relative.metrics[k]).map((k) => (
                    <RelativeDelta
                      key={k}
                      reading={relative.metrics[k]}
                      weekday={relative.weekday}
                      hour={relative.hour}
                    />
                  ))}
                </Box>
              ) : (
                <Typography variant="body2" color="text.secondary">
                  {relative ? "No recent runs to compare against the baseline." : "Loading the current reading…"}
                </Typography>
              )}
            </CardContent>
          </Card>

          {heatmap && heatmap.by_hour.length > 0 && (
            <Card sx={{ gridColumn: { lg: "1 / -1" } }}>
              <CardContent>
                <Typography variant="h6" gutterBottom>
                  {heatmap.label} by hour of day
                </Typography>
                <ResponsiveContainer width="100%" height={260}>
                  <ComposedChart data={hourChart} margin={{ top: 8, right: 16, bottom: 8, left: -8 }}>
                    <CartesianGrid stroke="rgba(255,255,255,0.08)" strokeDasharray="3 3" />
                    <XAxis dataKey="hour" stroke={theme.palette.text.secondary} fontSize={12} />
                    <YAxis
                      stroke={theme.palette.text.secondary}
                      fontSize={12}
                      unit={heatmap.unit ? ` ${heatmap.unit}` : undefined}
                    />
                    <RTooltip
                      contentStyle={{
                        background: theme.palette.background.paper,
                        border: "1px solid rgba(255,255,255,0.08)",
                        borderRadius: 8,
                      }}
                      labelStyle={{ color: theme.palette.text.secondary }}
                    />
                    <Area
                      type="monotone"
                      dataKey="band"
                      name="IQR"
                      stroke="none"
                      fill="#6fd0dc"
                      fillOpacity={0.15}
                      isAnimationActive={false}
                      activeDot={false}
                    />
                    <Line
                      type="monotone"
                      dataKey="median"
                      name={`Median ${heatmap.label}`}
                      stroke="#6fd0dc"
                      strokeWidth={2}
                      dot={false}
                      isAnimationActive={false}
                    />
                  </ComposedChart>
                </ResponsiveContainer>
              </CardContent>
            </Card>
          )}
        </Box>
      )}
    </Box>
  );
}
