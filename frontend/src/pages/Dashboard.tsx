// The Dashboard — a NOC-style wall of the tuner's current stance.
//
// Top to bottom: a status strip of KPI tiles (is the pipeline busy, is monitoring on,
// what does an iteration cost, how much data is there, who is following what), the hero
// Overall + headline axis gauges beside the profile the firewall is on right now and
// what is running, the three verdicts (the two crowns, the pooled leaderboard, the duel
// ring), then the time series, the per-metric breakdown and the latest run's waterfall.
// Every number here already exists on an endpoint; this page arranges them so the state
// of the system reads at a glance rather than as a column of prose.
import { useCallback, useEffect, useRef, useState } from "react";
import { Link as RouterLink } from "react-router-dom";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import LinearProgress from "@mui/material/LinearProgress";
import MenuItem from "@mui/material/MenuItem";
import Select from "@mui/material/Select";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import EmojiEventsIcon from "@mui/icons-material/EmojiEvents";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import RefreshIcon from "@mui/icons-material/Refresh";
import SpeedIcon from "@mui/icons-material/Speed";

import { api, ApiError } from "../api/client";
import { useQueuedAction } from "../hooks/useQueuedAction";
import type {
  AxisSeriesResponse,
  CrownFollowStatus,
  CurrentTest,
  DuelStandings,
  JobsResponse,
  MonitoringStatus,
  ScheduleStatus,
  RollingScore,
  RunDetail,
  RunEstimate,
  RunSummary,
  SettingsImpact,
  SettingsProfilesResponse,
} from "../api/types";
import { ImpactBanner } from "./Settings";
import ScoreGauge from "../components/ScoreGauge";
import TwoCrowns from "../components/TwoCrowns";
import SubscoreBreakdown from "../components/SubscoreBreakdown";
import SeriesChart from "../components/SeriesChart";
import Waterfall from "../components/Waterfall";
import StatusChip from "../components/StatusChip";
import Loading from "../components/Loading";
import EmptyState from "../components/EmptyState";
import { HelpTip } from "../components/Explain";
import StatTile, { type Tone } from "../components/dashboard/StatTile";
import Sparkline from "../components/dashboard/Sparkline";
import Leaderboard from "../components/dashboard/Leaderboard";
import RingCard from "../components/dashboard/RingCard";
import ActiveJobs from "../components/dashboard/ActiveJobs";
import { sopsColor } from "../theme";
import { useMetricMeta } from "../utils/metrics";
import { fmtDateTime, fmtDuration, fmtNum, parseApiDate, runRemainingMs } from "../utils/format";
import { useNow } from "../utils/useNow";

// Colors for the headline axis lines/gauges (amber = responsiveness, cyan = speed,
// violet = smoothness, …). Fixed per axis, never cycled.
const AXIS_COLORS: Record<string, string> = {
  overall: "#eceff1",
  responsiveness: "#ffa726",
  speed: "#4dd0e1",
  smoothness: "#ab47bc",
  stability: "#81c784",
  completion: "#90a4ae",
};
const axisColor = (key: string) => AXIS_COLORS[key] ?? "#4dd0e1";

const isRunning = (s: string) => ["running", "pending", "queued"].includes(s.toLowerCase());

// How often the light "ops" tiles (pipeline, jobs, monitoring, iteration cost) re-poll.
// The heavy field reads (profiles, standings) refresh on load and when a run lands.
const OPS_POLL_MS = 15_000;

const fmtReign = (hours: number | null | undefined) => {
  if (hours == null) return "—";
  if (hours < 1) return "<1h";
  if (hours < 48) return `${Math.round(hours)}h`;
  return `${Math.round(hours / 24)}d`;
};

// Section header inside a card: a title plus an optional right-hand slot, one line.
function CardTitle({
  title,
  help,
  right,
}: {
  title: string;
  help?: string;
  right?: React.ReactNode;
}) {
  return (
    <Stack direction="row" spacing={1} alignItems="center" justifyContent="space-between" sx={{ mb: 1.5 }}>
      <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>
        {title}
        {help && <HelpTip title={help} />}
      </Typography>
      {right}
    </Stack>
  );
}

// A strip of tiny status dots — one per recent run, oldest to newest. Always paired
// with a caption that states the failure count in words.
function RunDots({ runs }: { runs: RunSummary[] }) {
  const ordered = [...runs].reverse();
  return (
    <Stack direction="row" spacing={0.35} flexWrap="wrap" useFlexGap sx={{ maxWidth: 96, justifyContent: "flex-end" }}>
      {ordered.map((r) => {
        const s = r.status.toLowerCase();
        const color = s === "failed" || s === "error" ? "#ef5350" : isRunning(s) ? "#4dd0e1" : "#66bb6a";
        return (
          <Box
            key={r.id}
            component="span"
            title={`Run #${r.id} · ${r.status}`}
            sx={{ width: 6, height: 6, borderRadius: "50%", bgcolor: color, opacity: 0.85 }}
          />
        );
      })}
    </Stack>
  );
}

export default function Dashboard() {
  const metricMeta = useMetricMeta();
  const [latest, setLatest] = useState<RunDetail | null>(null);
  const [axisSeries, setAxisSeries] = useState<AxisSeriesResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The shared "add a job" policy: confirm-then-queue when the pipeline is busy, and one
  // wording for what happened. Its message lands in the same place as the page's errors.
  const queue = useQueuedAction(setError);
  const [iterations, setIterations] = useState(3);
  // Raw text backing the Iterations field, so it can be cleared or hold an intermediate value
  // while typing (e.g. "" or "2" on the way to "20") instead of snapping back to 1 each keystroke.
  const [iterationsText, setIterationsText] = useState("3");
  const [estimate, setEstimate] = useState<RunEstimate | null>(null);
  const [rolling, setRolling] = useState<RollingScore | null>(null);
  const [monitoring, setMonitoring] = useState<MonitoringStatus | null>(null);
  const [schedule, setSchedule] = useState<ScheduleStatus | null>(null);
  const [impact, setImpact] = useState<SettingsImpact | null>(null);
  const [field, setField] = useState<SettingsProfilesResponse | null>(null);
  const [standings, setStandings] = useState<DuelStandings | null>(null);
  const [follow, setFollow] = useState<CrownFollowStatus | null>(null);
  const [jobs, setJobs] = useState<{ data: JobsResponse; receivedAt: number } | null>(null);
  const [recentRuns, setRecentRuns] = useState<RunSummary[]>([]);
  const [runCount, setRunCount] = useState<number | null>(null);
  const [configFilter, setConfigFilter] = useState<string>(""); // "" = all configs
  const pollRef = useRef<number | null>(null);
  // "Test current for X minutes": a time-boxed collection session on the live profile.
  const [testMinutes, setTestMinutes] = useState(15);
  const [testMinutesText, setTestMinutesText] = useState("15");
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

  // Rolling + over-time scores, scoped to the selected config (or all).
  const refreshScores = useCallback(() => {
    const fp = configFilter || undefined;
    api.rollingScore(24, fp).then((r) => setRolling(r)).catch(() => {});
    api.axisSeries(100, fp).then((r) => setAxisSeries(r)).catch(() => {});
  }, [configFilter]);

  // The cheap, fast-moving reads: what is running, what an iteration costs, when the
  // next monitoring run is due, what the last few runs did.
  const refreshOps = useCallback(() => {
    api.jobs().then((j) => setJobs({ data: j, receivedAt: Date.now() })).catch(() => {});
    api.runEstimate().then((e) => setEstimate(e)).catch(() => {});
    api.monitoring().then((m) => setMonitoring(m)).catch(() => {});
    api.schedule().then(setSchedule).catch(() => {});
    api.history(30).then((h) => setRecentRuns(h)).catch(() => {});
    api.historyCount().then((c) => setRunCount(c.count)).catch(() => {});
  }, []);

  // The field-level verdicts. Memoized server-side, but still the heavy end of the page.
  const refreshField = useCallback(() => {
    api.settingsProfiles().then((p) => setField(p)).catch(() => {});
    api.settingsImpact().then((i) => setImpact(i)).catch(() => {});
    api.duelStandings().then((s) => setStandings(s)).catch(() => {});
    api.crownFollow().then((f) => setFollow(f)).catch(() => {});
  }, []);

  const loadAll = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      refreshOps();
      refreshField();
      await Promise.all([
        loadLatest(),
        api.currentTestStatus().then((t) => setCurrentTest(t.status ? t : null)).catch(() => {}),
      ]);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load dashboard");
    } finally {
      setLoading(false);
    }
  }, [loadLatest, refreshOps, refreshField]);

  useEffect(() => {
    loadAll();
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
      if (testPollRef.current) window.clearInterval(testPollRef.current);
    };
  }, [loadAll]);

  useEffect(() => {
    refreshScores();
  }, [refreshScores]);

  // Keep the ops strip live while the tab is visible.
  useEffect(() => {
    const id = window.setInterval(() => {
      if (document.visibilityState === "visible") refreshOps();
    }, OPS_POLL_MS);
    return () => window.clearInterval(id);
  }, [refreshOps]);

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
            refreshOps();
            refreshField();
          }
        } catch {
          /* keep polling */
        }
      }, 2000);
    },
    [refreshScores, refreshOps, refreshField]
  );

  // Every "Run this" goes through the shared queue policy: if the pipeline is busy it asks
  // whether to queue, and either way the message says which happened. See useQueuedAction.
  const startRun = useCallback(async () => {
    setRunning(true);
    setError(null);
    try {
      const d = await api.triggerRun({ iterations });
      setLatest(d);
      refreshOps();
      // A queued run has no measurement to poll yet — the jobs feed carries it until it
      // starts, so polling here would just spin on a PENDING row.
      if (!d.queued && isRunning(d.status)) {
        poll(d.id);
      } else {
        setRunning(false);
        if (!d.queued) refreshScores();
      }
      return d;
    } catch (e) {
      setRunning(false);
      setError(e instanceof Error ? e.message : "Failed to start benchmark");
      throw e;
    }
  }, [poll, iterations, refreshScores, refreshOps]);

  const handleRun = useCallback(
    () => queue.submit({ label: `Benchmark run · ${iterations} iteration(s)`, run: startRun }),
    [queue, startRun, iterations],
  );

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
          refreshOps();
          refreshField();
          loadLatest();
        }
      } catch {
        /* keep polling */
      }
    }, 2000);
  }, [refreshScores, refreshOps, refreshField, loadLatest]);

  const testActive = currentTest != null && currentTest.status != null && isRunning(currentTest.status);

  const startTest = useCallback(async () => {
    setError(null);
    try {
      const t = await api.currentTestStart(testMinutes);
      refreshOps();
      if (!t.queued) {
        setCurrentTest(t);
        pollTest();
      }
      return t;
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to start test");
      throw e;
    }
  }, [testMinutes, pollTest, refreshOps]);

  const handleStartTest = useCallback(
    () => queue.submit({ label: `Test current profile · ${testMinutes} min`, run: startTest }),
    [queue, startTest, testMinutes],
  );

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
  const etaMs = estimate?.per_iteration_ms != null ? estimate.per_iteration_ms * iterations : null;
  // Say what the ETA rests on: an iteration priced from the last half hour is a different
  // claim from one priced off runs from this morning, and the reader should know which.
  const etaSource =
    estimate?.basis === "recent"
      ? "from the last 30 min"
      : estimate?.basis === "today"
        ? "from the last 6 h"
        : estimate?.basis === "history"
          ? "from older runs"
          : "";
  const etaLabel =
    etaMs != null ? `ETA ~${fmtDuration(etaMs)}${etaSource ? ` · ${etaSource}` : ""}` : "ETA available after the first run";
  const latestDurationMs =
    latest?.started_at && latest?.finished_at
      ? parseApiDate(latest.finished_at).getTime() - parseApiDate(latest.started_at).getTime()
      : null;
  // Prefer the windowed median breakdown; fall back to the latest run's.
  const aggBreakdown =
    rolling && rolling.count > 0 && Object.keys(rolling.subscores).length > 0
      ? { subscores: rolling.subscores, weights_used: rolling.weights, metric_values: rolling.metric_values }
      : null;

  // ── Derived readings for the tiles ────────────────────────────────────────────────
  const profiles = field?.profiles ?? [];
  const pipeline = jobs?.data.pipeline ?? null;
  const runningJobs = jobs?.data.running ?? 0;
  const pipelineStalled =
    pipeline?.busy && pipeline.stalled_for_s != null && pipeline.stalled_for_s > pipeline.stale_after_s / 2;
  const pipelineTone: Tone = !pipeline ? "idle" : pipelineStalled ? "warn" : pipeline.busy ? "info" : "good";
  const pipelineValue = !pipeline ? "—" : pipelineStalled ? "Stalled" : pipeline.busy ? "Busy" : "Idle";
  const pipelineCaption = !pipeline
    ? "pipeline status unavailable"
    : pipeline.busy
      ? `${pipeline.owner ?? "a session"} · held ${fmtDuration((pipeline.held_for_s ?? 0) * 1000)}${
          pipeline.waiting > 0 ? ` · ${pipeline.waiting} waiting` : ""
        }${pipelineStalled ? ` · quiet ${fmtDuration((pipeline.stalled_for_s ?? 0) * 1000)}` : ""}`
      : runningJobs > 0
        ? `${runningJobs} job${runningJobs === 1 ? "" : "s"} running`
        : "nothing holds the benchmark lock";

  const nextRunMs = monitoring?.next_run_at ? parseApiDate(monitoring.next_run_at).getTime() - Date.now() : null;
  const monitoringTone: Tone = !monitoring ? "idle" : monitoring.enabled ? (monitoring.active ? "info" : "good") : "warn";
  const monitoringCaption = !monitoring
    ? "status unavailable"
    : !monitoring.enabled
      ? "off — enable in Config"
      : monitoring.active
        ? "a monitoring run is in progress"
        : nextRunMs != null
          ? nextRunMs > 0
            ? `next in ${fmtDuration(nextRunMs)}`
            : "next run is due"
          : monitoring.last_run_at
            ? `last ${fmtDateTime(monitoring.last_run_at)}`
            : "waiting for the first run";

  // What runs next across EVERY schedule, not just the monitoring cadence. A duel window
  // opening at 03:00, a nightly baseline test or an armed experiment change what the
  // platform is doing for hours, and used to announce themselves only by taking the
  // pipeline — so "nothing is running" and "a duel opens in twenty minutes" read the same.
  // The server's `next` already skips the monitoring cadence — the Monitoring tile beside
  // this one says when that is due, and naming it here too hid the answer this tile exists
  // for: the overnight duel, the baseline test, the experiment window.
  const nextJob = schedule?.next ?? null;
  const nextJobMs = nextJob?.at ? parseApiDate(nextJob.at).getTime() - Date.now() : null;
  const armed = (schedule?.upcoming ?? []).filter((e) => e.enabled && e.kind !== "monitoring");
  // Armed, but on a cadence rather than a clock (a continuous duel, the crown-follow
  // backstop): there is no next time to name, and inventing one would be worse than saying
  // so — but it IS the scheduled work, so name it rather than "on demand".
  const cadence = armed.find((e) => !e.at) ?? null;
  const scheduleValue = !schedule
    ? "—"
    : nextJob
      ? nextJob.label
      : cadence
        ? cadence.label
        : "Nothing armed";
  const scheduleCaption = !schedule
    ? "status unavailable"
    : nextJob && nextJobMs != null
      ? nextJobMs > 0
        ? `in ${fmtDuration(nextJobMs)} · ${fmtDateTime(nextJob.at!)}`
        : "due now"
      : cadence
        ? `${cadence.detail ?? "armed"} · no fixed time`
        : "no overnight or scheduled work armed";
  const scheduleTone: Tone = !schedule ? "idle" : nextJob ? "good" : cadence ? "info" : "warn";
  // Everything armed, and everything off, as one hover — so "why is nothing scheduled?" is
  // answerable without opening Config.
  const scheduleHelp = [
    "The next scheduled event beyond the monitoring cadence (that one has its own tile): the duel ladder, the nightly baseline test, the experiment window and the crown-follow check. Every source, armed or off:",
    ...(schedule?.upcoming ?? []).map((e) => {
      const when = e.at ? fmtDateTime(e.at) : e.enabled ? e.detail || "no fixed time" : "off";
      return `• ${e.label}: ${when}${e.at && e.detail ? ` (${e.detail})` : ""}`;
    }),
  ].join("\n");

  const iterSeries = [...recentRuns].reverse().map((r) => r.per_iteration_ms ?? null);
  const iterCaption = estimate?.per_iteration_ms != null
    ? `${etaSource || "measured"}${estimate.based_on_iterations ? ` · ${estimate.based_on_iterations} iterations` : ""}`
    : "no timed run yet";

  const recentFailed = recentRuns.filter((r) => ["failed", "error"].includes(r.status.toLowerCase())).length;
  const runsCaption = [
    rolling ? `${rolling.count} scored in ${rolling.window_hours}h` : null,
    recentRuns.length ? `${recentFailed} failed of last ${recentRuns.length}` : null,
  ]
    .filter(Boolean)
    .join(" · ");
  const totalIterations = profiles.reduce((s, p) => s + (p.iterations ?? 0), 0);

  const confidentCount = profiles.filter((p) => p.confident).length;
  const heirsTotal = field?.heirs?.total ?? 0;
  const profilesCaption = field
    ? `${confidentCount} confident · ${totalIterations.toLocaleString()} iterations${
        heirsTotal > 0 ? ` · ${heirsTotal} could beat the crown` : ""
      }`
    : "loading the field";

  const followOn = follow?.config?.enabled ?? false;
  const followTone: Tone = !follow ? "idle" : followOn ? "good" : "idle";
  const followCaption = follow
    ? `${followOn ? `policy ${follow.config.policy}` : "manual"} · crown changed ${follow.stats.changes_7d}× in 7d · reign ${fmtReign(
        follow.stats.current_reign_hours
      )}`
    : "status unavailable";

  const crown = field?.best_fingerprint ? profiles.find((p) => p.fingerprint === field.best_fingerprint) ?? null : null;
  const liveFp = field?.current_fingerprint ?? null;
  const live = liveFp ? profiles.find((p) => p.fingerprint === liveFp) ?? null : null;
  const liveIsCrown = liveFp != null && liveFp === field?.best_fingerprint;
  const liveIsTied = liveFp != null && (field?.co_leaders ?? []).includes(liveFp);
  const rankedConfident = profiles
    .filter((p) => p.confident && p.overall != null)
    .sort((a, b) => (b.overall ?? 0) - (a.overall ?? 0));
  const liveRank = liveFp ? rankedConfident.findIndex((p) => p.fingerprint === liveFp) + 1 : 0;
  const crownMetrics = field?.overall_metrics ?? [];
  const minIterations = field?.min_iterations ?? 15;

  // What the Overall is ACTUALLY computed from, read off the methodology on every request.
  // The axes are a different decomposition and have not been the Overall's inputs since
  // v5 — under v16 they are dominated by metrics (render, load_event, cadence, evenness,
  // byte earliness, CLS) the Overall never reads, so a hero card built on them shows a
  // rubric that stopped being current many versions ago.
  const overallLegs = rolling?.overall_metrics ?? crownMetrics;
  const overallWeights = rolling?.overall_weights ?? {};
  const overallMethod = rolling?.overall_method ?? "corner";
  const allAxes = rolling?.axes ?? [];
  const overallStat = rolling?.axis_scores["overall"] ?? null;

  return (
    <Box>
      {/* ── Header + run controls ─────────────────────────────────────────────── */}
      <Stack
        direction={{ xs: "column", sm: "row" }}
        justifyContent="space-between"
        alignItems={{ xs: "flex-start", sm: "center" }}
        spacing={2}
        sx={{ mb: 2 }}
      >
        <Box>
          <Typography variant="h4">Dashboard</Typography>
          <Typography variant="caption" color="text.secondary">
            Live stance of the tuner — what is running, who is winning, what a run costs.
          </Typography>
        </Box>
        <Stack spacing={0.5} alignItems={{ xs: "flex-start", sm: "flex-end" }} sx={{ maxWidth: "100%" }}>
          <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
            <Tooltip
              title={`How many times to run the full suite and average the results (1–${maxIterations}). More iterations = steadier score, longer run.`}
            >
              <TextField
                label="Iterations"
                type="text"
                size="small"
                value={iterationsText}
                onChange={(e) => {
                  const raw = e.target.value.replace(/[^0-9]/g, "");
                  setIterationsText(raw);
                  const n = parseInt(raw, 10);
                  if (!Number.isNaN(n)) setIterations(Math.max(1, Math.min(n, maxIterations)));
                }}
                onBlur={() => {
                  const n = parseInt(iterationsText, 10);
                  const clamped = Number.isNaN(n) ? iterations : Math.max(1, Math.min(n, maxIterations));
                  setIterations(clamped);
                  setIterationsText(String(clamped));
                }}
                inputProps={{ inputMode: "numeric", pattern: "[0-9]*", min: 1, max: maxIterations }}
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

      {!loading && impact && impact.changed && impact.significant && <ImpactBanner impact={impact} />}

      {/* ── Status strip ──────────────────────────────────────────────────────── */}
      <Box
        sx={{
          display: "grid",
          gap: 1.5,
          mb: 2,
          gridTemplateColumns: { xs: "repeat(2, 1fr)", sm: "repeat(3, 1fr)", lg: "repeat(6, 1fr)" },
        }}
      >
        <StatTile
          label="Pipeline"
          value={pipelineValue}
          caption={pipelineCaption}
          tone={pipelineTone}
          live={!!pipeline?.busy && !pipelineStalled}
          help="The benchmark lock every measurement session takes. Busy is normal for hours during a duel; stalled means the holder has stopped reporting progress and will be evicted."
        />
        <StatTile
          label="Monitoring"
          value={monitoring ? (monitoring.enabled ? `every ${monitoring.interval_minutes}m` : "Off") : "—"}
          caption={monitoringCaption}
          tone={monitoringTone}
          live={!!monitoring?.active}
          to="/config"
          help="Scheduled background runs on whatever profile the firewall is on."
        />
        <StatTile
          label="Next scheduled"
          value={scheduleValue}
          caption={scheduleCaption}
          tone={scheduleTone}
          to="/config"
          help={scheduleHelp}
        />
        <StatTile
          label="Avg iteration"
          value={estimate?.per_iteration_ms != null ? fmtDuration(estimate.per_iteration_ms) : "—"}
          caption={iterCaption}
          aside={<Sparkline values={iterSeries} color="#4dd0e1" width={72} />}
          to="/methodology?audit=instrument"
          help="What one iteration of the suite costs, priced from the most recent runs first. The sparkline is the per-iteration time of the last 30 runs, oldest to newest. Climbing? Tap to run the instrument-drift audit: it says whether the measurement got slower (grading at risk) or the run got bigger (nothing graded moved)."
        />
        <StatTile
          label="Runs"
          value={runCount != null ? runCount.toLocaleString() : "—"}
          caption={runsCaption || "no runs yet"}
          tone={recentFailed > 0 ? (recentFailed >= 5 ? "bad" : "warn") : undefined}
          aside={recentRuns.length > 0 ? <RunDots runs={recentRuns} /> : undefined}
          to="/history"
          help="Every benchmark run on record. The dots are the last 30, oldest to newest: green complete, red failed, blue in progress."
        />
        <StatTile
          label="Profiles"
          value={field ? profiles.length.toLocaleString() : "—"}
          caption={profilesCaption}
          tone={heirsTotal > 0 ? "warn" : undefined}
          to="/settings"
          help={`Distinct firewall settings profiles with scored runs. A profile is confident at ${minIterations} iterations; heirs are under-sampled profiles whose optimistic ceiling could still beat the crown.`}
        />
        <StatTile
          label="Follow best"
          value={follow ? (followOn ? "On" : "Off") : "—"}
          caption={followCaption}
          tone={followTone}
          help="Whether the firewall is kept on the crowned profile automatically, and how often the crown has been changing hands — the number that says whether following would thrash."
        />
      </Box>

      {loading && !rolling ? (
        <Loading label="Loading dashboard…" />
      ) : latest == null && !rolling ? (
        <Card>
          <CardContent>
            <EmptyState
              icon={<SpeedIcon fontSize="inherit" />}
              title="No benchmark runs yet"
              description="Run your first benchmark to measure network path quality and compute an Overall score."
              action={
                <Button variant="contained" startIcon={<PlayArrowIcon />} onClick={handleRun} disabled={activeRun}>
                  {activeRun ? "Running…" : "Run Benchmark"}
                </Button>
              }
            />
          </CardContent>
        </Card>
      ) : (
        <>
          {/* ── Stance row: hero gauges · live profile · now running ───────────── */}
          <Box
            sx={{
              display: "grid",
              gap: 2,
              mb: 2,
              gridTemplateColumns: { xs: "1fr", md: "repeat(2, 1fr)", lg: "2fr 1fr 1fr" },
            }}
          >
            <Card sx={{ gridColumn: { md: "1 / -1", lg: "auto" } }}>
              <CardContent>
                <CardTitle
                  title={`Overall · last ${rolling?.window_hours ?? 24}h`}
                  help="Median of every scored run in the window, under the current methodology. The smaller gauges are the metrics the Overall is actually computed from — read from the methodology itself, so they follow a rubric change. The axis breakdown below is a separate decomposition, not the Overall's inputs."
                  right={
                    profiles.length > 1 ? (
                      <Select
                        size="small"
                        value={configFilter}
                        displayEmpty
                        onChange={(e) => setConfigFilter(e.target.value)}
                        sx={{ minWidth: 160, maxWidth: 240 }}
                      >
                        <MenuItem value="">All profiles</MenuItem>
                        {profiles.map((p) => (
                          <MenuItem key={p.fingerprint} value={p.fingerprint}>
                            {p.name || p.label}
                          </MenuItem>
                        ))}
                      </Select>
                    ) : undefined
                  }
                />
                {rolling && rolling.count > 0 ? (
                  <Stack
                    direction={{ xs: "column", sm: "row" }}
                    spacing={3}
                    alignItems="center"
                    justifyContent="space-around"
                    flexWrap="wrap"
                    useFlexGap
                  >
                    <Box sx={{ textAlign: "center" }}>
                      <ScoreGauge value={overallStat?.median ?? null} size={168} label="Overall" />
                      {overallStat && (
                        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>
                          IQR {Math.round(overallStat.p25)}–{Math.round(overallStat.p75)} · {rolling.count} run
                          {rolling.count === 1 ? "" : "s"}
                        </Typography>
                      )}
                    </Box>
                    {/* The Overall's actual legs — one gauge per crown metric, named and
                        weighted by the methodology itself. A metric that leaves the crown
                        leaves this card with it. */}
                    <Stack direction="row" spacing={2} justifyContent="center" flexWrap="wrap" useFlexGap>
                      {overallLegs.map((k) => {
                        const meta = metricMeta(k);
                        const sub = rolling.subscores?.[k];
                        const raw = rolling.metric_values?.[k];
                        const weight = overallWeights[k];
                        return (
                          // The label lives OUTSIDE the ring: metric names ("Largest
                          // Contentful Paint") are far longer than the axis names that used
                          // to sit here, and inside a 104px circle they wrap to mush.
                          <Box key={k} sx={{ textAlign: "center", maxWidth: 128 }}>
                            <Tooltip title={meta.description || meta.label}>
                              <Box component="span">
                                <ScoreGauge value={sub ?? null} size={104} label="" />
                              </Box>
                            </Tooltip>
                            <Typography variant="caption" sx={{ display: "block", mt: 0.25, lineHeight: 1.2 }}>
                              {meta.label}
                            </Typography>
                            <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
                              {raw != null
                                ? `${Number.isInteger(raw) ? raw : raw.toFixed(1)}${meta.unit ? ` ${meta.unit}` : ""}`
                                : "—"}
                            </Typography>
                            {/* Weight only means something where the Overall is a weighted
                                average; a corner is an intersection and has none. */}
                            {overallMethod === "weighted" && weight != null && (
                              <Typography variant="caption" color="text.disabled" sx={{ display: "block" }}>
                                ×{weight}
                              </Typography>
                            )}
                          </Box>
                        );
                      })}
                    </Stack>
                    <Stack spacing={0.75} sx={{ minWidth: 130 }}>
                      <Typography variant="caption" color="text.disabled">
                        Axis breakdown
                      </Typography>
                      {allAxes.map((a) => {
                        const stat = rolling.axis_scores[a.key];
                        return (
                          <Stack key={a.key} direction="row" justifyContent="space-between" spacing={1.5}>
                            <Typography variant="caption" color="text.secondary" noWrap>
                              {a.label}
                            </Typography>
                            <Typography variant="body2" sx={{ fontWeight: 700, color: sopsColor(stat?.median) }}>
                              {stat ? Math.round(stat.median) : "—"}
                            </Typography>
                          </Stack>
                        );
                      })}
                      <Typography variant="caption" color="text.disabled">
                        {overallMethod === "weighted" ? "weighted" : overallMethod} ·{" "}
                        <RouterLink to="/methodology" style={{ color: "inherit" }}>
                          {rolling.methodology}
                        </RouterLink>
                      </Typography>
                    </Stack>
                  </Stack>
                ) : (
                  <Typography variant="body2" color="text.secondary">
                    No comparable scored run in the last {rolling?.window_hours ?? 24}h
                    {configFilter ? " for this profile" : ""}.
                  </Typography>
                )}
              </CardContent>
            </Card>

            <Card>
              <CardContent>
                <CardTitle
                  title="On the firewall"
                  help="The profile the firewall is currently set to (live discovery), with its standing in the pooled field and its median crown metrics."
                  right={
                    liveIsCrown ? (
                      <Chip size="small" color="warning" icon={<EmojiEventsIcon />} label="the crown" />
                    ) : liveIsTied ? (
                      <Chip size="small" variant="outlined" label="tied with crown" />
                    ) : undefined
                  }
                />
                {live ? (
                  <Stack spacing={1}>
                    <Box sx={{ minWidth: 0 }}>
                      <Typography
                        variant="h6"
                        noWrap
                        component={RouterLink}
                        to={`/profiles/${encodeURIComponent(live.fingerprint)}`}
                        sx={{ color: "inherit", textDecoration: "none", display: "block" }}
                        title={live.label}
                      >
                        {live.name || live.label}
                      </Typography>
                      <Typography variant="caption" color="text.disabled" noWrap component="div" title={live.label}>
                        {live.label}
                      </Typography>
                    </Box>
                    <Stack direction="row" spacing={2} alignItems="baseline">
                      <Typography sx={{ fontSize: 34, fontWeight: 600, lineHeight: 1, color: sopsColor(live.overall) }}>
                        {fmtNum(live.overall, 1)}
                      </Typography>
                      <Typography variant="caption" color="text.secondary">
                        Overall
                        {live.confident && liveRank > 0 ? ` · #${liveRank} of ${rankedConfident.length}` : ""}
                        {!live.confident ? ` · ${Math.max(0, minIterations - live.iterations)} iterations to confidence` : ""}
                      </Typography>
                    </Stack>
                    {crown && !liveIsCrown && crown.overall != null && live.overall != null && (
                      <Typography variant="caption" color="text.secondary">
                        {fmtNum(crown.overall - live.overall, 1)} behind the crown ({crown.name || crown.label})
                      </Typography>
                    )}
                    {crownMetrics.length > 0 && (
                      <Stack spacing={0.6} sx={{ mt: 0.5 }}>
                        {crownMetrics.map((k) => {
                          const meta = metricMeta(k);
                          const raw = live.metrics?.[k];
                          const pct = live.crown_norm?.[k];
                          return (
                            <Box key={k}>
                              <Stack direction="row" justifyContent="space-between" spacing={1}>
                                <Typography variant="caption" color="text.secondary" noWrap>
                                  {meta.label}
                                </Typography>
                                <Typography variant="caption" sx={{ fontVariantNumeric: "tabular-nums" }}>
                                  {raw != null ? `${Number.isInteger(raw) ? raw : raw.toFixed(1)}${meta.unit ? ` ${meta.unit}` : ""}` : "—"}
                                  {pct != null && (
                                    <Typography component="span" variant="caption" color="text.disabled">
                                      {" "}· p{Math.round(pct)}
                                    </Typography>
                                  )}
                                </Typography>
                              </Stack>
                              <Box sx={{ height: 4, borderRadius: 2, bgcolor: "rgba(255,255,255,0.06)", overflow: "hidden" }}>
                                <Box
                                  sx={{
                                    width: `${Math.max(0, Math.min(100, pct ?? 0))}%`,
                                    height: "100%",
                                    bgcolor: "#4dd0e1",
                                    opacity: 0.8,
                                  }}
                                />
                              </Box>
                            </Box>
                          );
                        })}
                        <Typography variant="caption" color="text.disabled">
                          p = percentile within the field (higher is better)
                        </Typography>
                      </Stack>
                    )}
                  </Stack>
                ) : (
                  <Typography variant="body2" color="text.secondary">
                    {field
                      ? liveFp
                        ? "The firewall is on a profile with no scored runs yet."
                        : "Live firewall discovery is unavailable."
                      : "Loading…"}
                  </Typography>
                )}

                {/* Test current settings: the action that belongs to the live profile. */}
                <Box sx={{ mt: 1.5, pt: 1.5, borderTop: 1, borderColor: "divider" }}>
                  {testActive ? (
                    <Stack spacing={0.75}>
                      <Stack direction="row" justifyContent="space-between" alignItems="center">
                        <Typography variant="body2">
                          Testing · {fmtDuration(testRemainMs)} left · {currentTest?.iterations_run ?? 0} it ·{" "}
                          {currentTest?.runs_created ?? 0} run{(currentTest?.runs_created ?? 0) === 1 ? "" : "s"}
                        </Typography>
                        <Button size="small" color="warning" variant="outlined" onClick={handleCancelTest}>
                          Stop
                        </Button>
                      </Stack>
                      <LinearProgress variant="determinate" value={testPct} sx={{ borderRadius: 1 }} />
                    </Stack>
                  ) : (
                    <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
                      <Tooltip title="Keep benchmarking the current settings for this long. Data is collected in ~5-iteration chunks and saved as it goes — the way to mature the live profile toward confidence.">
                        <TextField
                          label="Minutes"
                          type="text"
                          size="small"
                          value={testMinutesText}
                          onChange={(e) => {
                            const raw = e.target.value.replace(/[^0-9]/g, "");
                            setTestMinutesText(raw);
                            const n = parseInt(raw, 10);
                            if (!Number.isNaN(n)) setTestMinutes(Math.max(1, Math.min(n, 1440)));
                          }}
                          onBlur={() => {
                            const n = parseInt(testMinutesText, 10);
                            const clamped = Number.isNaN(n) ? testMinutes : Math.max(1, Math.min(n, 1440));
                            setTestMinutes(clamped);
                            setTestMinutesText(String(clamped));
                          }}
                          inputProps={{ inputMode: "numeric", pattern: "[0-9]*", min: 1, max: 1440 }}
                          disabled={activeRun}
                          sx={{ width: 96 }}
                        />
                      </Tooltip>
                      <Button size="small" variant="outlined" startIcon={<PlayArrowIcon />} onClick={handleStartTest} disabled={activeRun}>
                        Test current for {testMinutes} min
                      </Button>
                    </Stack>
                  )}
                  {currentTest && !testActive && currentTest.status && currentTest.status !== "pending" && (
                    <Typography variant="caption" color="text.disabled" sx={{ display: "block", mt: 0.75 }}>
                      Last test {currentTest.status} · {currentTest.iterations_run} iteration
                      {currentTest.iterations_run === 1 ? "" : "s"} across {currentTest.runs_created} run
                      {currentTest.runs_created === 1 ? "" : "s"}
                      {currentTest.error ? ` · ${currentTest.error}` : ""}
                    </Typography>
                  )}
                </Box>
              </CardContent>
            </Card>

            <Card>
              <CardContent>
                <CardTitle
                  title="Now running"
                  help="Top-level background jobs from the jobs feed — the same list as the top-right dropdown, without the per-chunk detail."
                  right={
                    runningJobs > 0 ? (
                      <Chip size="small" color="info" label={`${runningJobs} active`} />
                    ) : undefined
                  }
                />
                <ActiveJobs jobs={jobs?.data.jobs ?? []} receivedAt={jobs?.receivedAt ?? Date.now()} />
                {latest && (
                  <Box sx={{ mt: 1.5, pt: 1.5, borderTop: 1, borderColor: "divider" }}>
                    <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
                      <Typography variant="caption" color="text.secondary">
                        Latest run
                      </Typography>
                      <StatusChip status={latest.status} etaMs={latestEtaMs} />
                      <Chip
                        size="small"
                        variant="outlined"
                        component={RouterLink}
                        to={`/runs/${latest.id}`}
                        clickable
                        label={`#${latest.id}`}
                      />
                      {latest.score?.legacy && (
                        <Tooltip title="Scored before the current rubric — not comparable to current runs.">
                          <Chip size="small" variant="outlined" color="warning" label="legacy" />
                        </Tooltip>
                      )}
                      <Typography variant="body2" sx={{ fontWeight: 600, color: sopsColor(latest.overall) }}>
                        {latest.overall != null ? Math.round(latest.overall) : "—"}
                      </Typography>
                    </Stack>
                    {activeRun && (() => {
                      const total = latest.iterations ?? iterations;
                      const done = latest.iterations_completed ?? 0;
                      const determinate = done > 0 && total > 0;
                      return (
                        <Box sx={{ mt: 0.75 }}>
                          <LinearProgress
                            variant={determinate ? "determinate" : "indeterminate"}
                            value={determinate ? (done / total) * 100 : undefined}
                            sx={{ height: 6, borderRadius: 3 }}
                          />
                          <Typography variant="caption" color="text.secondary">
                            {total > 1 ? `Iteration ${Math.min(done + 1, total)} of ${total}…` : "Benchmark in progress…"}
                          </Typography>
                        </Box>
                      );
                    })()}
                    <Typography variant="caption" color="text.disabled" component="div" sx={{ mt: 0.5 }}>
                      {latest.label ? `${latest.label} · ` : ""}
                      {fmtDateTime(latest.finished_at ?? latest.created_at)}
                      {latestDurationMs != null ? ` · took ${fmtDuration(latestDurationMs)}` : ""}
                      {latest.iterations > 1 && latest.per_iteration_ms != null
                        ? ` (${latest.iterations} × ~${fmtDuration(latest.per_iteration_ms)})`
                        : ""}
                    </Typography>
                    {latest.error && (
                      <Alert severity="error" sx={{ mt: 1 }}>
                        {latest.error}
                      </Alert>
                    )}
                  </Box>
                )}
              </CardContent>
            </Card>
          </Box>

          {/* ── Verdict row: the two crowns · pooled leaderboard · the ring ──────── */}
          <Box
            sx={{
              display: "grid",
              gap: 2,
              mb: 2,
              gridTemplateColumns: { xs: "1fr", md: "repeat(2, 1fr)", lg: "repeat(3, 1fr)" },
              // TwoCrowns renders its own Card with a bottom margin meant for a stacked
              // page; inside a grid cell that margin only opens a gap under it.
              "& > .two-crowns > .MuiCard-root": { mb: 0, height: "100%" },
            }}
          >
            <Box className="two-crowns" sx={{ gridColumn: { md: "1 / -1", lg: "auto" } }}>
              <TwoCrowns />
            </Box>
            <Card>
              <CardContent>
                <CardTitle
                  title="Top profiles by Overall"
                  help="Confident profiles ranked by pooled Overall — the crown is #1 by definition. The live profile is kept in view even when it sits below the cut."
                  right={
                    <Button size="small" component={RouterLink} to="/settings" sx={{ flexShrink: 0 }}>
                      Standings
                    </Button>
                  }
                />
                {field ? (
                  <Leaderboard
                    profiles={profiles}
                    bestFingerprint={field.best_fingerprint}
                    currentFingerprint={field.current_fingerprint}
                    coLeaders={field.co_leaders ?? []}
                    minIterations={minIterations}
                  />
                ) : (
                  <Loading label="Ranking the field…" />
                )}
              </CardContent>
            </Card>
            <Card>
              <CardContent>
                <CardTitle
                  title="In the ring"
                  help="The duel ladder's own verdict: the belt holder and the standings on fitted head-to-head strength. A controlled comparison under shared weather, so it can disagree with the pooled record — that is the point of running both."
                  right={
                    <Button size="small" component={RouterLink} to="/duels">
                      Duels
                    </Button>
                  }
                />
                {standings ? <RingCard standings={standings} /> : <Loading label="Reading the ledger…" />}
              </CardContent>
            </Card>
          </Box>

          {/* ── Trend + breakdown + waterfall ─────────────────────────────────────── */}
          <Box
            sx={{
              display: "grid",
              gap: 2,
              gridTemplateColumns: { xs: "1fr", md: "2fr 1fr" },
            }}
          >
            <Card>
              <CardContent>
                <CardTitle
                  title="Scores over time"
                  help="Each scored run's headline axes, most recent 100 runs, under the current methodology."
                />
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

            <Card>
              <CardContent>
                <CardTitle
                  title="By metric"
                  help={
                    aggBreakdown
                      ? `Median subscore per metric over the last ${rolling?.window_hours ?? 24}h (${rolling?.count} runs).`
                      : "The latest run's subscore per metric."
                  }
                />
                {aggBreakdown ? (
                  <SubscoreBreakdown score={aggBreakdown} attribution={rolling?.attribution} />
                ) : latest?.score ? (
                  <SubscoreBreakdown score={latest.score} />
                ) : (
                  <Typography variant="body2" color="text.secondary">
                    Score not available yet.
                  </Typography>
                )}
              </CardContent>
            </Card>

            {(() => {
              const bm = latest?.results?.find((r) => r.plugin === "browser")?.metrics ?? null;
              if (!bm) return null;
              return (
                <Card sx={{ gridColumn: { md: "1 / -1" } }}>
                  <CardContent>
                    <CardTitle
                      title={`Load waterfall · run #${latest?.id}`}
                      help="Cool bars up to first byte are network setup (DNS/TCP/TLS/TTFB), dominated by weather. Delivery (first byte → response done) is body delivery through your queue — the one phase your shaper moves. Purple bars after are client render, which shaping can't touch."
                    />
                    <Waterfall metrics={bm} />
                  </CardContent>
                </Card>
              );
            })()}
          </Box>
        </>
      )}
      {queue.dialog}
    </Box>
  );
}
