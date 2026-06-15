import { useCallback, useEffect, useRef, useState } from "react";
import { Link as RouterLink } from "react-router-dom";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import LinearProgress from "@mui/material/LinearProgress";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import RefreshIcon from "@mui/icons-material/Refresh";
import SpeedIcon from "@mui/icons-material/Speed";

import { api, ApiError } from "../api/client";
import type {
  MonitoringStatus,
  RollingScore,
  RunDetail,
  RunEstimate,
  SeriesPoint,
  SettingsImpact,
} from "../api/types";
import { ImpactBanner } from "./Settings";
import ScoreGauge from "../components/ScoreGauge";
import SubscoreBreakdown from "../components/SubscoreBreakdown";
import SeriesChart from "../components/SeriesChart";
import StatusChip from "../components/StatusChip";
import Loading from "../components/Loading";
import EmptyState from "../components/EmptyState";
import { fmtDateTime, fmtDuration, parseApiDate } from "../utils/format";

const isRunning = (s: string) => ["running", "pending", "queued"].includes(s.toLowerCase());

export default function Dashboard() {
  const [latest, setLatest] = useState<RunDetail | null>(null);
  const [series, setSeries] = useState<SeriesPoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [iterations, setIterations] = useState(3);
  const [estimate, setEstimate] = useState<RunEstimate | null>(null);
  const [rolling, setRolling] = useState<RollingScore | null>(null);
  const [monitoring, setMonitoring] = useState<MonitoringStatus | null>(null);
  const [impact, setImpact] = useState<SettingsImpact | null>(null);
  const pollRef = useRef<number | null>(null);

  const loadLatest = useCallback(async () => {
    try {
      const d = await api.latestResult();
      setLatest(d);
      return d;
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        setLatest(null);
        return null;
      }
      throw e;
    }
  }, []);

  const loadAll = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      await Promise.all([
        loadLatest(),
        api.historySeries(100).then((r) => setSeries(r.points)),
        api.runEstimate().then((e) => setEstimate(e)).catch(() => {}),
        api.rollingScore(24).then((r) => setRolling(r)).catch(() => {}),
        api.monitoring().then((m) => setMonitoring(m)).catch(() => {}),
        api.settingsImpact().then((i) => setImpact(i)).catch(() => {}),
      ]);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load dashboard");
    } finally {
      setLoading(false);
    }
  }, [loadLatest]);

  useEffect(() => {
    loadAll();
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
    };
  }, [loadAll]);

  const poll = useCallback(
    (id: number) => {
      if (pollRef.current) window.clearInterval(pollRef.current);
      pollRef.current = window.setInterval(async () => {
        try {
          const d = await api.result(id);
          setLatest(d);
          if (!isRunning(d.status)) {
            if (pollRef.current) window.clearInterval(pollRef.current);
            pollRef.current = null;
            setRunning(false);
            api.historySeries(100).then((r) => setSeries(r.points)).catch(() => {});
            api.rollingScore(24).then((r) => setRolling(r)).catch(() => {});
          }
        } catch {
          /* keep polling */
        }
      }, 2000);
    },
    []
  );

  const handleRun = useCallback(async () => {
    setRunning(true);
    setError(null);
    try {
      const d = await api.triggerRun({ iterations });
      setLatest(d);
      if (isRunning(d.status)) {
        poll(d.id);
      } else {
        setRunning(false);
        api.historySeries(100).then((r) => setSeries(r.points)).catch(() => {});
      }
    } catch (e) {
      setRunning(false);
      setError(e instanceof Error ? e.message : "Failed to start benchmark");
    }
  }, [poll, iterations]);

  const activeRun = running || (latest != null && isRunning(latest.status));
  const maxIterations = estimate?.max_iterations ?? 20;
  const etaMs =
    estimate?.per_iteration_ms != null ? estimate.per_iteration_ms * iterations : null;
  const etaLabel =
    etaMs != null
      ? `ETA ~${fmtDuration(etaMs)}`
      : "ETA available after the first run";
  const latestDurationMs =
    latest?.started_at && latest?.finished_at
      ? parseApiDate(latest.finished_at).getTime() - parseApiDate(latest.started_at).getTime()
      : null;
  // Prefer the windowed median breakdown; fall back to the latest run's.
  const aggBreakdown =
    rolling && rolling.count > 0 && Object.keys(rolling.subscores).length > 0
      ? {
          subscores: rolling.subscores,
          weights_used: rolling.weights,
          metric_values: rolling.metric_values,
        }
      : null;

  return (
    <Box>
      <Stack
        direction={{ xs: "column", sm: "row" }}
        justifyContent="space-between"
        alignItems={{ xs: "flex-start", sm: "center" }}
        spacing={2}
        sx={{ mb: 3 }}
      >
        <Typography variant="h4">Dashboard</Typography>
        <Stack spacing={0.5} alignItems={{ xs: "flex-start", sm: "flex-end" }}>
          <Stack direction="row" spacing={1} alignItems="center">
            <Tooltip
              title={`How many times to run the full suite and average the results (1–${maxIterations}). More iterations = steadier score, longer run.`}
            >
              <TextField
                label="Iterations"
                type="number"
                size="small"
                value={iterations}
                onChange={(e) => {
                  const n = parseInt(e.target.value, 10);
                  setIterations(Number.isNaN(n) ? 1 : Math.max(1, Math.min(n, maxIterations)));
                }}
                inputProps={{ min: 1, max: maxIterations }}
                disabled={activeRun}
                sx={{ width: 110 }}
              />
            </Tooltip>
            <Button startIcon={<RefreshIcon />} onClick={loadAll} disabled={loading}>
              Refresh
            </Button>
            <Button
              variant="contained"
              startIcon={<PlayArrowIcon />}
              onClick={handleRun}
              disabled={activeRun}
            >
              {activeRun ? "Running…" : "Run Benchmark"}
            </Button>
          </Stack>
          <Typography variant="caption" color="text.secondary">
            {etaLabel}
          </Typography>
        </Stack>
      </Stack>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {!loading && impact && impact.changed && impact.significant && (
        <ImpactBanner impact={impact} />
      )}

      {!loading && rolling && rolling.count > 0 && (
        <Card sx={{ mb: 2 }}>
          <CardContent>
            <Stack
              direction={{ xs: "column", sm: "row" }}
              spacing={3}
              alignItems={{ xs: "flex-start", sm: "center" }}
            >
              <ScoreGauge value={rolling.median} />
              <Box>
                <Typography variant="h6">Current Responsiveness</Typography>
                <Typography variant="body2" color="text.secondary">
                  Median SOPS over the last {rolling.window_hours}h · {rolling.count} run
                  {rolling.count === 1 ? "" : "s"}
                </Typography>
                <Typography variant="body2" sx={{ mt: 1 }}>
                  Typical range (IQR): <b>{rolling.p25}–{rolling.p75}</b>
                  <Typography component="span" variant="caption" color="text.secondary" sx={{ ml: 1 }}>
                    (min {rolling.min} · max {rolling.max})
                  </Typography>
                </Typography>
                <Box sx={{ mt: 1.5 }}>
                  {monitoring?.enabled ? (
                    <Chip
                      size="small"
                      color="success"
                      label={`Auto-monitoring every ${monitoring.interval_minutes}m`}
                    />
                  ) : (
                    <Chip
                      size="small"
                      variant="outlined"
                      component={RouterLink}
                      to="/config"
                      clickable
                      label="Monitoring off — enable in Config"
                    />
                  )}
                  {monitoring?.enabled && monitoring.next_run_at && (
                    <Typography variant="caption" color="text.secondary" sx={{ ml: 1 }}>
                      next ~{fmtDateTime(monitoring.next_run_at)}
                    </Typography>
                  )}
                </Box>
              </Box>
            </Stack>
          </CardContent>
        </Card>
      )}

      {loading ? (
        <Loading label="Loading dashboard…" />
      ) : latest == null ? (
        <Card>
          <CardContent>
            <EmptyState
              icon={<SpeedIcon fontSize="inherit" />}
              title="No benchmark runs yet"
              description="Run your first benchmark to measure network path quality and compute a Seat of Pants Score."
              action={
                <Button
                  variant="contained"
                  startIcon={<PlayArrowIcon />}
                  onClick={handleRun}
                  disabled={activeRun}
                >
                  {activeRun ? "Running…" : "Run Benchmark"}
                </Button>
              }
            />
          </CardContent>
        </Card>
      ) : (
        <Box
          sx={{
            display: "grid",
            gap: 2,
            gridTemplateColumns: { xs: "1fr", md: "minmax(280px, 360px) 1fr" },
          }}
        >
          <Card>
            <CardContent sx={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 2 }}>
              <Typography variant="overline" color="text.secondary" sx={{ alignSelf: "flex-start" }}>
                Latest run
              </Typography>
              {activeRun && (() => {
                const total = latest?.iterations ?? iterations;
                const done = latest?.iterations_completed ?? 0;
                const determinate = done > 0 && total > 0;
                return (
                  <Box sx={{ width: "100%" }}>
                    <LinearProgress
                      variant={determinate ? "determinate" : "indeterminate"}
                      value={determinate ? (done / total) * 100 : undefined}
                    />
                    <Typography variant="caption" color="text.secondary">
                      {total > 1
                        ? `Iteration ${Math.min(done + 1, total)} of ${total}…`
                        : "Benchmark in progress…"}
                    </Typography>
                  </Box>
                );
              })()}
              <ScoreGauge value={latest.score?.sops ?? null} />
              {latest.score && latest.score.sops_stdev != null && latest.iterations > 1 && (
                <Typography variant="caption" color="text.secondary">
                  ± {latest.score.sops_stdev} (range {latest.score.sops_min}–{latest.score.sops_max})
                </Typography>
              )}
              <Stack direction="row" spacing={1} alignItems="center">
                <StatusChip status={latest.status} />
                <Chip
                  size="small"
                  variant="outlined"
                  component={RouterLink}
                  to={`/runs/${latest.id}`}
                  clickable
                  label={`Run #${latest.id}`}
                />
              </Stack>
              <Typography variant="caption" color="text.secondary">
                {latest.label ? `${latest.label} · ` : ""}
                {fmtDateTime(latest.finished_at ?? latest.created_at)}
              </Typography>
              {latestDurationMs != null && (
                <Typography variant="caption" color="text.secondary">
                  Took {fmtDuration(latestDurationMs)}
                  {latest.iterations > 1 && latest.per_iteration_ms != null
                    ? ` · ${latest.iterations} iterations (~${fmtDuration(latest.per_iteration_ms)} each)`
                    : ""}
                </Typography>
              )}
              {latest.error && (
                <Alert severity="error" sx={{ width: "100%" }}>
                  {latest.error}
                </Alert>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardContent>
              <Typography variant="h6">Responsiveness by Metric</Typography>
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.5 }}>
                {aggBreakdown
                  ? `Median per metric · last ${rolling?.window_hours ?? 24}h · ${rolling?.count} run${rolling?.count === 1 ? "" : "s"}`
                  : "Latest run"}
              </Typography>
              {aggBreakdown ? (
                <SubscoreBreakdown score={aggBreakdown} />
              ) : latest.score ? (
                <SubscoreBreakdown score={latest.score} />
              ) : (
                <Typography variant="body2" color="text.secondary">
                  Score not available yet.
                </Typography>
              )}
            </CardContent>
          </Card>

          <Card sx={{ gridColumn: { md: "1 / -1" } }}>
            <CardContent>
              <Typography variant="h6" gutterBottom>
                SOPS Over Time
              </Typography>
              {series.length > 0 ? (
                <SeriesChart
                  data={series}
                  yDomain={[0, 100]}
                  lines={[{ key: "sops", name: "SOPS", color: "#4dd0e1" }]}
                  band={{ lowKey: "sops_min", highKey: "sops_max", color: "#4dd0e1", name: "± range" }}
                />
              ) : (
                <Typography variant="body2" color="text.secondary">
                  Not enough history to chart yet.
                </Typography>
              )}
            </CardContent>
          </Card>
        </Box>
      )}
    </Box>
  );
}
