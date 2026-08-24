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
  const determinate = job.total != null && job.total > 0 && job.current != null;
  const pct = determinate ? Math.min(100, Math.round((job.current! / job.total!) * 100)) : 0;
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
            {determinate && job.status === "running" ? ` · ${pct}%` : ""}
          </Typography>
          {job.status === "running" && !job.error && (
            <Typography component="span" variant="caption" color="text.secondary">
              {job.eta_ms == null ? (
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
