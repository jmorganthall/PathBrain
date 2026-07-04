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

import MenuItem from "@mui/material/MenuItem";
import Select from "@mui/material/Select";

import { api, ApiError } from "../api/client";
import type {
  AxisSeriesResponse,
  CurrentTest,
  MonitoringStatus,
  RollingScore,
  RunDetail,
  RunEstimate,
  SettingsImpact,
  SettingsProfile,
} from "../api/types";
import { ImpactBanner } from "./Settings";
import ScoreGauge from "../components/ScoreGauge";
import SubscoreBreakdown from "../components/SubscoreBreakdown";
import SeriesChart from "../components/SeriesChart";
import Waterfall from "../components/Waterfall";
import StatusChip from "../components/StatusChip";
import Loading from "../components/Loading";
import EmptyState from "../components/EmptyState";
import { sopsColor } from "../theme";
import { fmtDateTime, fmtDuration, parseApiDate, runRemainingMs } from "../utils/format";

// Colors for the headline axis lines/gauges (amber = responsiveness, cyan = speed,
// violet = smoothness, …).
const AXIS_COLORS: Record<string, string> = {
  overall: "#eceff1",
  responsiveness: "#ffa726",
  speed: "#4dd0e1",
  smoothness: "#ab47bc",
  stability: "#81c784",
  completion: "#90a4ae",
};
const axisColor = (key: string) => AXIS_COLORS[key] ?? "#4dd0e1";
import { useNow } from "../utils/useNow";

const isRunning = (s: string) => ["running", "pending", "queued"].includes(s.toLowerCase());

export default function Dashboard() {
  const [latest, setLatest] = useState<RunDetail | null>(null);
  const [axisSeries, setAxisSeries] = useState<AxisSeriesResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [iterations, setIterations] = useState(3);
  const [estimate, setEstimate] = useState<RunEstimate | null>(null);
  const [rolling, setRolling] = useState<RollingScore | null>(null);
  const [monitoring, setMonitoring] = useState<MonitoringStatus | null>(null);
  const [impact, setImpact] = useState<SettingsImpact | null>(null);
  const [profiles, setProfiles] = useState<SettingsProfile[]>([]);
  const [configFilter, setConfigFilter] = useState<string>(""); // "" = all configs
  const pollRef = useRef<number | null>(null);
  // "Test current for X minutes": a time-boxed collection session on the live profile.
  const [testMinutes, setTestMinutes] = useState(15);
  const [currentTest, setCurrentTest] = useState<CurrentTest | null>(null);
  const testPollRef = useRef<number | null>(null);

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

  // Rolling + over-time scores, scoped to the selected config (or all). Re-runs
  // whenever the config filter changes.
  const refreshScores = useCallback(() => {
    const fp = configFilter || undefined;
    api.rollingScore(24, fp).then((r) => setRolling(r)).catch(() => {});
    api.axisSeries(100, fp).then((r) => setAxisSeries(r)).catch(() => {});
  }, [configFilter]);

  const loadAll = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      await Promise.all([
        loadLatest(),
        api.runEstimate().then((e) => setEstimate(e)).catch(() => {}),
        api.monitoring().then((m) => setMonitoring(m)).catch(() => {}),
        api.settingsImpact().then((i) => setImpact(i)).catch(() => {}),
        api.settingsProfiles().then((p) => setProfiles(p.profiles)).catch(() => {}),
        api.currentTestStatus().then((t) => setCurrentTest(t.status ? t : null)).catch(() => {}),
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
      if (testPollRef.current) window.clearInterval(testPollRef.current);
    };
  }, [loadAll]);

  // Fetch (and refetch on filter change) the windowed scores.
  useEffect(() => {
    refreshScores();
  }, [refreshScores]);

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
            refreshScores();
          }
        } catch {
          /* keep polling */
        }
      }, 2000);
    },
    [refreshScores]
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
        refreshScores();
      }
    } catch (e) {
      setRunning(false);
      setError(e instanceof Error ? e.message : "Failed to start benchmark");
    }
  }, [poll, iterations, refreshScores]);

  // Poll the timed test until it reaches a terminal state, then refresh scores/latest.
  const pollTest = useCallback(() => {
    if (testPollRef.current) window.clearInterval(testPollRef.current);
    testPollRef.current = window.setInterval(async () => {
      try {
        const t = await api.currentTestStatus();
        setCurrentTest(t.status ? t : null);
        if (!t.status || !isRunning(t.status)) {
          if (testPollRef.current) window.clearInterval(testPollRef.current);
          testPollRef.current = null;
          refreshScores();
          loadLatest();
        }
      } catch {
        /* keep polling */
      }
    }, 2000);
  }, [refreshScores, loadLatest]);

  const testActive = currentTest != null && currentTest.status != null && isRunning(currentTest.status);

  const handleStartTest = useCallback(async () => {
    setError(null);
    try {
      const t = await api.currentTestStart(testMinutes);
      setCurrentTest(t);
      pollTest();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to start test");
    }
  }, [testMinutes, pollTest]);

  const handleCancelTest = useCallback(async () => {
    try {
      await api.currentTestCancel();
      pollTest();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to cancel test");
    }
  }, [pollTest]);

  // Resume polling if a test is already running when the page (re)loads.
  useEffect(() => {
    if (testActive && testPollRef.current == null) pollTest();
  }, [testActive, pollTest]);

  const activeRun = running || (latest != null && isRunning(latest.status));
  const now = useNow(activeRun || testActive);
  const testStartedMs = currentTest?.started_at ? parseApiDate(currentTest.started_at).getTime() : null;
  const testElapsedMs = testActive && testStartedMs != null ? Math.max(0, now - testStartedMs) : 0;
  const testTotalMs = (currentTest?.duration_s ?? 0) * 1000;
  const testRemainMs = testActive ? Math.max(0, testTotalMs - testElapsedMs) : 0;
  const testPct = testActive && testTotalMs > 0 ? Math.min(100, (testElapsedMs / testTotalMs) * 100) : 0;
  const latestEtaMs =
    latest && isRunning(latest.status)
      ? runRemainingMs(latest.started_at, latest.iterations, estimate?.per_iteration_ms, now)
      : null;
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
                disabled={activeRun || testActive}
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
              disabled={activeRun || testActive}
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

      {/* Test current settings for X minutes: a time-boxed collection loop on the live
          profile, chunked into short runs so an interruption keeps its data. */}
      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Stack
            direction={{ xs: "column", sm: "row" }}
            spacing={1.5}
            alignItems={{ xs: "flex-start", sm: "center" }}
            justifyContent="space-between"
          >
            <Box sx={{ minWidth: 0 }}>
              <Typography variant="h6">Test current settings</Typography>
              <Typography variant="caption" color="text.secondary">
                Collect data on the live profile for a set time — runs in short chunks so partial
                progress is never lost. Great for maturing the current profile toward confidence.
              </Typography>
            </Box>
            {testActive ? (
              <Stack direction="row" spacing={1} alignItems="center" flexShrink={0}>
                <Box sx={{ minWidth: 180 }}>
                  <Typography variant="body2">
                    {fmtDuration(testRemainMs)} left · {currentTest?.iterations_run ?? 0} iteration
                    {(currentTest?.iterations_run ?? 0) === 1 ? "" : "s"} · {currentTest?.runs_created ?? 0} run
                    {(currentTest?.runs_created ?? 0) === 1 ? "" : "s"}
                  </Typography>
                  <LinearProgress variant="determinate" value={testPct} sx={{ mt: 0.5, borderRadius: 1 }} />
                </Box>
                <Button color="warning" variant="outlined" onClick={handleCancelTest}>
                  Stop
                </Button>
              </Stack>
            ) : (
              <Stack direction="row" spacing={1} alignItems="center" flexShrink={0}>
                <Tooltip title="How long to keep benchmarking the current settings. Data is collected in ~5-iteration chunks and saved as it goes.">
                  <TextField
                    label="Minutes"
                    type="number"
                    size="small"
                    value={testMinutes}
                    onChange={(e) => {
                      const n = parseInt(e.target.value, 10);
                      setTestMinutes(Number.isNaN(n) ? 1 : Math.max(1, Math.min(n, 1440)));
                    }}
                    inputProps={{ min: 1, max: 1440 }}
                    disabled={activeRun}
                    sx={{ width: 110 }}
                  />
                </Tooltip>
                <Button
                  variant="contained"
                  startIcon={<PlayArrowIcon />}
                  onClick={handleStartTest}
                  disabled={activeRun}
                >
                  Test for {testMinutes} min
                </Button>
              </Stack>
            )}
          </Stack>
          {currentTest && !testActive && currentTest.status && currentTest.status !== "pending" && (
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
              Last test {currentTest.status} · collected {currentTest.iterations_run} iteration
              {currentTest.iterations_run === 1 ? "" : "s"} across {currentTest.runs_created} run
              {currentTest.runs_created === 1 ? "" : "s"}
              {currentTest.error ? ` · ${currentTest.error}` : ""}
            </Typography>
          )}
        </CardContent>
      </Card>

      {!loading && impact && impact.changed && impact.significant && (
        <ImpactBanner impact={impact} />
      )}

      {!loading && rolling && rolling.count > 0 && (
        <Card sx={{ mb: 2 }}>
          <CardContent>
            <Stack
              direction={{ xs: "column", sm: "row" }}
              justifyContent="space-between"
              alignItems={{ xs: "flex-start", sm: "center" }}
              spacing={1}
            >
              <Typography variant="h6">Current Responsiveness</Typography>
              {profiles.length > 1 && (
                <Select
                  size="small"
                  value={configFilter}
                  displayEmpty
                  onChange={(e) => setConfigFilter(e.target.value)}
                  sx={{ minWidth: 200 }}
                >
                  <MenuItem value="">All configs</MenuItem>
                  {profiles.map((p) => (
                    <MenuItem key={p.fingerprint} value={p.fingerprint}>
                      {p.label}
                    </MenuItem>
                  ))}
                </Select>
              )}
            </Stack>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
              Median over the last {rolling.window_hours}h · {rolling.count} run
              {rolling.count === 1 ? "" : "s"}
              {configFilter && " · this config only"} · methodology{" "}
              <RouterLink to="/methodology" style={{ color: "inherit" }}>
                {rolling.methodology}
              </RouterLink>
            </Typography>
            <Stack
              direction={{ xs: "column", sm: "row" }}
              spacing={4}
              alignItems={{ xs: "flex-start", sm: "center" }}
              flexWrap="wrap"
              useFlexGap
            >
              {rolling.axes
                .filter((a) => a.role === "headline")
                .map((a) => {
                  const stat = rolling.axis_scores[a.key];
                  return (
                    <Box key={a.key} sx={{ textAlign: "center" }}>
                      <ScoreGauge value={stat?.median ?? null} size={150} label={a.label} />
                      {stat && (
                        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>
                          IQR {stat.p25}–{stat.p75} · p95 {stat.p95}
                        </Typography>
                      )}
                    </Box>
                  );
                })}
              <Stack spacing={1.5}>
                {rolling.axes
                  .filter((a) => a.role !== "headline")
                  .map((a) => {
                    const stat = rolling.axis_scores[a.key];
                    return (
                      <Box key={a.key}>
                        <Typography variant="caption" color="text.secondary">
                          {a.label}
                        </Typography>{" "}
                        <Typography
                          component="span"
                          sx={{ fontWeight: 700, color: sopsColor(stat?.median) }}
                        >
                          {stat ? Math.round(stat.median) : "—"}
                        </Typography>
                      </Box>
                    );
                  })}
                <Box>
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
                </Box>
              </Stack>
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
              description="Run your first benchmark to measure network path quality and compute an Overall score."
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
              <ScoreGauge value={latest.overall ?? null} label="Overall" />
              <Typography variant="caption" color="text.secondary" sx={{ textAlign: "center" }}>
                Overall — how close this run sits to the perfect feel corner.
              </Typography>
              <Stack direction="row" spacing={1} alignItems="center">
                <StatusChip status={latest.status} etaMs={latestEtaMs} />
                <Chip
                  size="small"
                  variant="outlined"
                  component={RouterLink}
                  to={`/runs/${latest.id}`}
                  clickable
                  label={`Run #${latest.id}`}
                />
                {latest.score?.legacy && (
                  <Tooltip title="Scored before the current rubric — not comparable to current runs.">
                    <Chip size="small" variant="outlined" color="warning" label="legacy" />
                  </Tooltip>
                )}
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
                <SubscoreBreakdown score={aggBreakdown} attribution={rolling?.attribution} />
              ) : latest.score ? (
                <SubscoreBreakdown score={latest.score} />
              ) : (
                <Typography variant="body2" color="text.secondary">
                  Score not available yet.
                </Typography>
              )}
            </CardContent>
          </Card>

          {(() => {
            const bm = latest.results?.find((r) => r.plugin === "browser")?.metrics ?? null;
            if (!bm) return null;
            return (
              <Card sx={{ gridColumn: { md: "1 / -1" } }}>
                <CardContent>
                  <Typography variant="h6" gutterBottom>
                    Load waterfall (latest run)
                  </Typography>
                  <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 2 }}>
                    The page load split into independent phases. Cool bars up to <b>first byte</b>
                    {" "}are network setup (DNS/TCP/TLS/TTFB) — weather-dominated, baked into FCP &amp;
                    LCP. The amber <b>Delivery</b> phase (first byte → response done) is body delivery
                    through your queue — the one phase your shaper actually moves. The purple bars
                    after are client render (parse/paint), which shaping can&apos;t touch.
                  </Typography>
                  <Waterfall metrics={bm} />
                </CardContent>
              </Card>
            );
          })()}

          <Card sx={{ gridColumn: { md: "1 / -1" } }}>
            <CardContent>
              <Typography variant="h6" gutterBottom>
                Scores Over Time
              </Typography>
              {axisSeries && axisSeries.points.length > 0 ? (
                <SeriesChart
                  data={axisSeries.points}
                  yDomain={[0, 100]}
                  lines={axisSeries.axes
                    .filter((a) => a.role === "headline")
                    .map((a) => ({ key: a.key, name: a.label, color: axisColor(a.key) }))}
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
