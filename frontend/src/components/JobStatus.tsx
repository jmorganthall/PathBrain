import { useCallback, useEffect, useRef, useState } from "react";
import { Link as RouterLink } from "react-router-dom";
import Badge from "@mui/material/Badge";
import Box from "@mui/material/Box";
import CircularProgress from "@mui/material/CircularProgress";
import Divider from "@mui/material/Divider";
import IconButton from "@mui/material/IconButton";
import LinearProgress from "@mui/material/LinearProgress";
import Link from "@mui/material/Link";
import Popover from "@mui/material/Popover";
import Stack from "@mui/material/Stack";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import NotificationsIcon from "@mui/icons-material/Notifications";
import CheckCircleIcon from "@mui/icons-material/CheckCircle";
import ErrorIcon from "@mui/icons-material/Error";
import CloseIcon from "@mui/icons-material/Close";

import { api, JOBS_REFRESH_EVENT } from "../api/client";
import type { Job } from "../api/types";
import { fmtTimeShort } from "../utils/format";

const ACTIVE_POLL_MS = 2000;
const IDLE_POLL_MS = 10000;
// The countdown ticks on its own clock, far faster than the poll. A progress bar that only
// moves when a poll lands reads as frozen between updates, and "when will this be done?" is
// the question it is standing in for anyway — 3/40 doesn't tell you whether to wait or walk
// away. One second is the coarsest tick that still looks alive.
const TICK_MS = 1000;
// The progress bar has to look continuous, not merely alive, so it redraws faster than the
// countdown's one-second beat — at a second a step a 20s iteration advances in 5% jerks.
const PROGRESS_TICK_MS = 250;
// The ceiling for an *interpolated* bar on a running job (see `useSmoothProgress`).
const RUNNING_MAX_PCT = 99;

/** "1m 04s" / "42s" — the countdown itself, no "~" and no "left" (the label says that). */
function fmtCountdown(ms: number): string {
  const secs = Math.max(0, Math.round(ms / 1000));
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  if (mins < 60) return `${mins}m ${String(secs % 60).padStart(2, "0")}s`;
  return `${Math.floor(mins / 60)}h ${String(mins % 60).padStart(2, "0")}m`;
}

const ETA_TITLE: Record<string, string> = {
  scheduled: "This job is time-boxed, so this isn't an estimate — it's when the window closes.",
  measured: "Work left x how long that unit has actually been taking on recent runs.",
  observed: "Extrapolated from this job's own rate so far, and it self-corrects as it goes.",
  queued:
    "This job hasn't started — it's waiting for the pipeline. That's how long the work takes " +
    "once it begins, so it stays put until then.",
};

/**
 * The live countdown for one job.
 *
 * Anchored on arrival: the server sends how many milliseconds remain *as of that response*,
 * and this converts it to a deadline on the BROWSER's clock. That keeps it immune to any
 * skew between the two machines, and means the number keeps falling truthfully between
 * polls instead of sitting still and then jumping. A fresh `eta_ms` re-anchors it.
 *
 * It floors at zero rather than going negative: an over-running job says "finishing…",
 * which is honest about an estimate that has been overtaken rather than pretending to
 * count into the past.
 */
function Countdown({ etaMs, basis }: { etaMs: number; basis?: string | null }) {
  const [deadline, setDeadline] = useState(() => Date.now() + etaMs);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    setDeadline(Date.now() + etaMs);
    setNow(Date.now());
  }, [etaMs]);

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), TICK_MS);
    return () => clearInterval(id);
  }, []);

  const left = deadline - now;
  return (
    <Tooltip title={ETA_TITLE[basis ?? ""] ?? "Estimated time remaining."}>
      <Typography component="span" variant="caption" color="text.secondary">
        {left > 0 ? `${fmtCountdown(left)} left` : "finishing…"}
      </Typography>
    </Tooltip>
  );
}

/**
 * A queued job's estimate, shown STANDING STILL.
 *
 * The clock it will run on hasn't started: it waits on the coordination lock, and nothing
 * knows when the current holder lets go. So this is a size of work ("20m once it starts"),
 * not a time remaining, and ticking it would be the one reading guaranteed to be wrong —
 * the wait would quietly eat an estimate of time that hasn't begun to elapse, until a job
 * that never ran an iteration read "finishing…". It starts counting when the job does, at
 * which point the server sends a real basis and the countdown below takes over.
 */
function QueuedEta({ etaMs }: { etaMs: number }) {
  return (
    <Tooltip title={ETA_TITLE.queued}>
      <Typography component="span" variant="caption" color="text.secondary">
        {`${fmtCountdown(etaMs)} once it starts`}
      </Typography>
    </Tooltip>
  );
}

/**
 * A job that holds the pipeline and has shown no sign of progress.
 *
 * This replaces the countdown rather than sitting beside it, because the countdown is
 * exactly what stops being true here: a time-boxed job past its deadline floors at
 * "finishing…", so a duel wedged at 23:00 in a browser call that never returned read
 * "finishing…" all night while nothing whatsoever happened. Silence is the one thing that
 * can be stated for certain, and how long it has lasted is what decides whether to look.
 */
function Stalled({ ms }: { ms: number }) {
  return (
    <Tooltip
      title={
        "This job holds the benchmark pipeline and hasn't reported progress. If it stays " +
        "quiet it is handed on automatically; /api/health/pipeline shows what call it is in."
      }
    >
      <Typography component="span" variant="caption" color="warning.main">
        {`no progress for ${fmtCountdown(ms)}`}
      </Typography>
    </Tooltip>
  );
}

/**
 * The progress bar advances *between* counter ticks, not only on them — and for a job made
 * of no counters at all, it draws the window instead of giving up.
 *
 * Two kinds of job, two denominators, one rule: **the bar and the countdown are two views
 * of the same estimate**, so whichever fact the countdown was built from is the one the bar
 * measures against.
 *
 * **Time-boxed** (`window_ms`) — a duel session, a challenger race, "test current for 20
 * minutes". These run until their window closes, not until a count is exhausted, so they
 * report no `total` and the bar had nothing to draw but the indeterminate sweep: the same
 * animation at minute one of a six-hour duel as at minute three hundred, saying only that
 * something, somewhere, is happening. That is the least informative thing on screen for the
 * job that runs the longest. The window fixes it exactly: `eta_ms` is what remains,
 * `window_ms` is what it remains of, and the share between them is measured progress rather
 * than an estimate of it. It is anchored on the browser's clock the moment it arrives —
 * exactly as `Countdown` anchors the same deadline, and for the same reason — so it advances
 * smoothly between polls instead of stepping once every two seconds.
 *
 * **Unit-counted** (`current`/`total`) — the counter records *finished* units, so a bar
 * drawn straight from it is motionless for the whole of a unit and then jumps. On a
 * benchmark run a unit is a full iteration: tens of seconds of a bar that looks hung,
 * followed by a lurch. What fills the gap is the unit's expected cost (`unit_ms`, again the
 * number the countdown beside it is built from), crossed at the rate the unit is expected
 * to take.
 *
 * Neither reading ever runs ahead of the work. Interpolation is a claim about a step the
 * job has not finished, so it is capped at the edge of the current unit — an iteration
 * running long parks the bar there, which is the one honest thing to show, since all that is
 * actually known is that the step is overdue — and both are capped below a full bar, because
 * 100% is the single reading that means *finished* and this is being drawn on a job that is
 * still running.
 */
function useSmoothProgress(job: Job): { determinate: boolean; pct: number } {
  const total = job.total ?? 0;
  const current = job.current ?? 0;
  const running = job.status === "running" && !job.queued;
  const counted = job.total != null && total > 0 && job.current != null;

  // A window outranks a unit count for the same reason it outranks every other estimate on
  // the server: it is a deadline, not an extrapolation, and it is what actually ends the
  // job. (Nothing reports both today; stating the precedence is what keeps the bar and the
  // countdown from ever disagreeing if something does.)
  const windowMs = job.window_ms ?? 0;
  const timeBoxed = running && windowMs > 0 && job.eta_ms != null;
  const smoothing = !timeBoxed && counted && running && !!job.unit_ms && current < total;

  // When the counter last moved, on the browser's clock. Observed here rather than sent by
  // the server because the server keeps no per-unit timestamps — it would have to assume
  // every unit so far took exactly the estimate, which is precisely the assumption that
  // fails on the slow job this exists to keep alive. The cost is an anchor lagging by up
  // to one poll, which parks a second or two of a long unit at the boundary; the bar is
  // never ahead of the work, only occasionally early to wait.
  const anchor = useRef({ current, at: Date.now() });
  if (anchor.current.current !== current) anchor.current = { current, at: Date.now() };

  // The window's deadline, likewise on the browser's clock, re-anchored by each poll's
  // fresh remainder. Same treatment as `Countdown`: a duration is skew-free where an
  // absolute server timestamp would fold the two machines' clock difference into the bar.
  const etaMs = job.eta_ms ?? null;
  const deadline = useRef({ etaMs, at: Date.now() });
  if (deadline.current.etaMs !== etaMs) deadline.current = { etaMs, at: Date.now() };

  const ticking = timeBoxed || smoothing;
  const [, tick] = useState(0);
  useEffect(() => {
    if (!ticking) return;
    const id = setInterval(() => tick((t) => t + 1), PROGRESS_TICK_MS);
    return () => clearInterval(id);
  }, [ticking]);

  if (timeBoxed) {
    const left = Math.max(0, (deadline.current.etaMs ?? 0) - (Date.now() - deadline.current.at));
    const pct = ((windowMs - left) / windowMs) * 100;
    return { determinate: true, pct: Math.min(RUNNING_MAX_PCT, Math.max(0, pct)) };
  }
  if (!counted) return { determinate: false, pct: 0 };
  const within = smoothing ? Math.min(1, (Date.now() - anchor.current.at) / job.unit_ms!) : 0;
  const pct = ((current + within) / total) * 100;
  // Interpolation may reach a unit boundary but never a full bar: 100% is the one reading
  // that means finished, and this is being drawn on a job that is still running.
  return { determinate: true, pct: smoothing ? Math.min(RUNNING_MAX_PCT, pct) : Math.min(100, pct) };
}

function StatusIcon({ status }: { status: Job["status"] }) {
  if (status === "running") return <CircularProgress size={16} />;
  if (status === "succeeded") return <CheckCircleIcon color="success" fontSize="small" />;
  return <ErrorIcon color="error" fontSize="small" />;
}

function JobRow({
  job,
  indent = false,
  onCancel,
}: {
  job: Job;
  indent?: boolean;
  onCancel?: (job: Job) => void;
}) {
  const { determinate, pct } = useSmoothProgress(job);
  const canCancel = job.status === "running" && !!job.cancel_url;
  return (
    <Box
      sx={{
        pl: indent ? 3.5 : 2,
        pr: 1,
        py: indent ? 0.75 : 1.25,
        // Nested chunks get a subtle left rail so the grouping reads at a glance.
        ...(indent ? { borderLeft: 2, borderColor: "divider", ml: 2 } : {}),
      }}
    >
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.25 }}>
        <StatusIcon status={job.status} />
        <Typography
          variant={indent ? "caption" : "body2"}
          // The label is the profile's call sign; the settings summary behind it is the
          // hover, so a narrow dropdown isn't three lines of "q3550 t3 i60 ecn".
          title={job.detail ?? undefined}
          sx={{ fontWeight: indent ? 500 : 600, flexGrow: 1, wordBreak: "break-word" }}
        >
          {job.href ? (
            <Link component={RouterLink} to={job.href} color="inherit" underline="hover">
              {job.label}
            </Link>
          ) : (
            job.label
          )}
        </Typography>
        <Typography variant="caption" color="text.secondary" sx={{ whiteSpace: "nowrap" }}>
          {fmtTimeShort(job.finished_at ?? job.started_at)}
        </Typography>
        {canCancel && onCancel && (
          <Tooltip title={indent ? "Cancel this chunk" : "Cancel this job"}>
            <IconButton
              size="small"
              onClick={() => onCancel(job)}
              aria-label={indent ? "cancel this chunk" : "cancel this job"}
              sx={{ p: 0.25 }}
            >
              <CloseIcon fontSize="inherit" />
            </IconButton>
          </Tooltip>
        )}
      </Stack>
      {job.status === "running" && (
        <LinearProgress
          variant={determinate ? "determinate" : "indeterminate"}
          value={pct}
          sx={{ borderRadius: 1, my: 0.5 }}
        />
      )}
      {(job.message || job.error || job.status === "running") && (
        <Stack direction="row" spacing={0.75} alignItems="baseline" flexWrap="wrap" useFlexGap>
          <Typography variant="caption" color={job.error ? "error.main" : "text.secondary"}>
            {job.error ?? job.message}
            {determinate && job.status === "running" ? ` · ${Math.round(pct)}%` : ""}
          </Typography>
          {job.status === "running" && !job.error && (
            <Typography component="span" variant="caption" color="text.secondary">
              {job.stalled_ms != null ? (
                <>
                  {"· "}
                  <Stalled ms={job.stalled_ms} />
                </>
              ) : job.eta_ms == null ? (
                // Say so rather than showing nothing: "no estimate yet" is information
                // ("it hasn't finished a unit"), an empty space is ambiguous. A job that
                // hasn't started says the more specific thing.
                job.queued ? "· waiting to start" : "· no estimate yet"
              ) : (
                <>
                  {"· "}
                  {job.queued ? (
                    <QueuedEta etaMs={job.eta_ms} />
                  ) : (
                    <Countdown etaMs={job.eta_ms} basis={job.eta_basis} />
                  )}
                </>
              )}
            </Typography>
          )}
        </Stack>
      )}
    </Box>
  );
}

// Azure-portal-style "running jobs" bell in the AppBar: a badge with the count of
// active background operations, and a dropdown listing them (with live progress) plus
// recently-finished ones. Polls /api/jobs faster while anything is running or the
// menu is open, slower when idle.
export default function JobStatus() {
  const [jobs, setJobs] = useState<Job[]>([]);
  const [running, setRunning] = useState(0);
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const open = Boolean(anchor);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const poll = useCallback(async () => {
    try {
      const r = await api.jobs();
      setJobs(r.jobs);
      setRunning(r.running);
    } catch {
      /* transient; keep the last snapshot */
    }
  }, []);

  const handleCancel = useCallback(
    async (job: Job) => {
      if (!job.cancel_url) return;
      const whole = !job.parent_id; // a top-level (parent) row cancels the whole operation
      const msg = whole
        ? `Cancel "${job.label}" and stop the whole job?`
        : `Cancel this chunk of "${job.label}"? (its broader job will stop too)`;
      if (!window.confirm(msg)) return;
      try {
        await api.cancelJob(job.cancel_url);
      } catch {
        /* best-effort; the next poll reflects reality */
      }
      void poll();
    },
    [poll]
  );

  // Self-scheduling poll loop: cadence depends on whether work is active / menu open.
  useEffect(() => {
    let cancelled = false;
    const tick = async () => {
      await poll();
      if (cancelled) return;
      const fast = running > 0 || open;
      timer.current = setTimeout(tick, fast ? ACTIVE_POLL_MS : IDLE_POLL_MS);
    };
    tick();
    return () => {
      cancelled = true;
      if (timer.current) clearTimeout(timer.current);
    };
  }, [poll, running, open]);

  // When a job is kicked off anywhere in the app, poll immediately so the badge reflects it
  // without waiting out the idle interval.
  useEffect(() => {
    const onStart = () => void poll();
    window.addEventListener(JOBS_REFRESH_EVENT, onStart);
    return () => window.removeEventListener(JOBS_REFRESH_EVENT, onStart);
  }, [poll]);

  return (
    <>
      <Tooltip title="Background jobs">
        <IconButton color="inherit" onClick={(e) => setAnchor(e.currentTarget)} aria-label="background jobs">
          <Badge badgeContent={running} color="primary" overlap="circular">
            <NotificationsIcon />
          </Badge>
        </IconButton>
      </Tooltip>
      <Popover
        open={open}
        anchorEl={anchor}
        onClose={() => setAnchor(null)}
        anchorOrigin={{ vertical: "bottom", horizontal: "right" }}
        transformOrigin={{ vertical: "top", horizontal: "right" }}
        slotProps={{ paper: { sx: { width: 380, maxHeight: 480, overflow: "auto" } } }}
      >
        <Typography variant="subtitle2" sx={{ px: 2, pt: 1.5, pb: 0.5 }}>
          Jobs{running > 0 ? ` — ${running} running` : ""}
        </Typography>
        <Divider />
        {jobs.length === 0 ? (
          <Typography variant="body2" color="text.secondary" sx={{ px: 2, py: 2 }}>
            No background jobs running. Re-grading history, sweeps, profile tests and
            benchmark runs show up here.
          </Typography>
        ) : (
          (() => {
            // Group chunks under their broader job: a top-level row per parent (or a job with
            // no/absent parent), with its chunk rows nested underneath.
            const ids = new Set(jobs.map((j) => j.id));
            const childrenByParent: Record<string, Job[]> = {};
            jobs.forEach((j) => {
              if (j.parent_id && ids.has(j.parent_id)) {
                (childrenByParent[j.parent_id] ||= []).push(j);
              }
            });
            const topLevel = jobs.filter((j) => !j.parent_id || !ids.has(j.parent_id));
            return topLevel.map((j, i) => (
              <Box key={j.id}>
                {i > 0 && <Divider />}
                <JobRow job={j} onCancel={handleCancel} />
                {(childrenByParent[j.id] ?? []).map((c) => (
                  <JobRow key={c.id} job={c} indent onCancel={handleCancel} />
                ))}
              </Box>
            ));
          })()
        )}
      </Popover>
    </>
  );
}
