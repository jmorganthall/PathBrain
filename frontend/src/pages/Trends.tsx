import { useCallback, useEffect, useState } from "react";
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
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import { useTheme } from "@mui/material/styles";

import { api } from "../api/client";
import type { TrendHeatmapResponse, TrendRelativeResponse } from "../api/types";
import TrendHeatmap from "../components/TrendHeatmap";
import RelativeDelta, { fmtBucket } from "../components/RelativeDelta";
import Loading from "../components/Loading";

// Environment-first ordering: the infra metrics describe "how good the internet
// is right now" (config-insensitive); the score axes come after.
const METRIC_OPTIONS: { key: string; label: string }[] = [
  { key: "latency", label: "Latency (ping)" },
  { key: "jitter", label: "Jitter" },
  { key: "packet_loss", label: "Packet loss" },
  { key: "transfer", label: "Transfer speed" },
  { key: "dns", label: "DNS lookup" },
  { key: "tcp", label: "TCP connect" },
  { key: "tls", label: "TLS handshake" },
  { key: "ttfb", label: "Time to First Byte" },
  { key: "overall", label: "Overall" },
  { key: "responsiveness", label: "Responsiveness" },
  { key: "smoothness", label: "Smoothness" },
  { key: "speed", label: "Speed" },
  { key: "stability", label: "Stability" },
  { key: "completion", label: "Completion" },
];

// Which metrics to surface in the "right now vs typical" panel (headline Overall +
// score axes first).
const RELATIVE_KEYS = [
  "overall",
  "responsiveness",
  "smoothness",
  "speed",
  "latency",
  "jitter",
  "packet_loss",
  "transfer",
  "dns",
  "tcp",
  "tls",
  "ttfb",
];

export default function Trends() {
  const theme = useTheme();
  const [metric, setMetric] = useState("latency");
  const [heatmap, setHeatmap] = useState<TrendHeatmapResponse | null>(null);
  const [relative, setRelative] = useState<TrendRelativeResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

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
      <Stack
        direction={{ xs: "column", sm: "row" }}
        justifyContent="space-between"
        alignItems={{ xs: "flex-start", sm: "center" }}
        spacing={2}
        sx={{ mb: 1 }}
      >
        <Typography variant="h4">Historical Trends</Typography>
        <TextField
          select
          size="small"
          label="Metric"
          value={metric}
          onChange={(e) => setMetric(e.target.value)}
          sx={{ minWidth: 200 }}
        >
          {METRIC_OPTIONS.map((o) => (
            <MenuItem key={o.key} value={o.key}>
              {o.label}
            </MenuItem>
          ))}
        </TextField>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3, maxWidth: 720 }}>
        How each metric typically behaves by day of week and hour of day — the
        network's "weather". The infra metrics (ping, jitter, speed) track general
        internet conditions, so a reading can be judged <em>relative to what's normal
        for this time</em>, not just in absolute terms.
      </Typography>

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
                  {RELATIVE_KEYS.filter((k) => relative.metrics[k]).map((k) => (
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
                  No recent runs to compare against the baseline.
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
                      fill="#4dd0e1"
                      fillOpacity={0.15}
                      isAnimationActive={false}
                      activeDot={false}
                    />
                    <Line
                      type="monotone"
                      dataKey="median"
                      name={`Median ${heatmap.label}`}
                      stroke="#4dd0e1"
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
