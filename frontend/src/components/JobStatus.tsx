import { useCallback, useEffect, useRef, useState } from "react";
import { Link as RouterLink } from "react-router-dom";
import Badge from "@mui/material/Badge";
import Box from "@mui/material/Box";
import Chip from "@mui/material/Chip";
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

import { api } from "../api/client";
import type { Job } from "../api/types";
import { fmtDuration, fmtTimeShort, parseApiDate } from "../utils/format";
import { useNow } from "../utils/useNow";

const ACTIVE_POLL_MS = 2000;
const IDLE_POLL_MS = 10000;

type ChipColor = "default" | "primary" | "secondary" | "info" | "success" | "warning";

// Maps each job ``kind`` to a human category label + color so the dropdown clearly
// distinguishes Scheduled Runs, Shotgun Sweeps, Experiments, etc. at a glance.
const CATEGORY: Record<string, { label: string; color: ChipColor }> = {
  run: { label: "Benchmark Run", color: "primary" },
  scheduled_run: { label: "Scheduled Run", color: "info" },
  sweep: { label: "Shotgun Sweep", color: "secondary" },
  profile_test: { label: "Profile Test", color: "warning" },
  experiment: { label: "Experiment", color: "success" },
  regrade: { label: "Re-grade", color: "default" },
  rescore: { label: "Re-score", color: "default" },
  rederive: { label: "Re-derive", color: "default" },
};

function category(kind: string): { label: string; color: ChipColor } {
  return CATEGORY[kind] ?? { label: kind, color: "default" };
}

// Estimated time remaining (ms) for a running job: its estimated total duration minus
// the time already elapsed since it started. Null when the backend can't estimate.
function jobRemainingMs(job: Job, now: number): number | null {
  if (job.eta_total_ms == null || !job.started_at) return null;
  const started = parseApiDate(job.started_at).getTime();
  if (Number.isNaN(started)) return null;
  return job.eta_total_ms - (now - started);
}

function StatusIcon({ status }: { status: Job["status"] }) {
  if (status === "running") return <CircularProgress size={16} />;
  if (status === "succeeded") return <CheckCircleIcon color="success" fontSize="small" />;
  return <ErrorIcon color="error" fontSize="small" />;
}

function JobRow({ job, now }: { job: Job; now: number }) {
  const determinate = job.total != null && job.total > 0 && job.current != null;
  const pct = determinate ? Math.min(100, Math.round((job.current! / job.total!) * 100)) : 0;
  const cat = category(job.kind);
  const remaining = job.status === "running" ? jobRemainingMs(job, now) : null;
  const eta =
    remaining == null ? null : remaining > 1000 ? `ETA: ${fmtDuration(remaining)}` : "wrapping up…";
  return (
    <Box sx={{ px: 2, py: 1.25 }}>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.25 }}>
        <StatusIcon status={job.status} />
        <Chip
          size="small"
          color={cat.color}
          variant={cat.color === "default" ? "outlined" : "filled"}
          label={cat.label}
          sx={{ height: 20, "& .MuiChip-label": { px: 1, fontSize: "0.68rem" } }}
        />
        <Typography variant="caption" color="text.secondary" sx={{ whiteSpace: "nowrap", ml: "auto" }}>
          {fmtTimeShort(job.finished_at ?? job.started_at)}
        </Typography>
      </Stack>
      <Typography variant="body2" sx={{ fontWeight: 600, wordBreak: "break-word", mb: 0.25 }}>
        {job.href ? (
          <Link component={RouterLink} to={job.href} color="inherit" underline="hover">
            {job.label}
          </Link>
        ) : (
          job.label
        )}
      </Typography>
      {job.status === "running" && (
        <LinearProgress
          variant={determinate ? "determinate" : "indeterminate"}
          value={pct}
          sx={{ borderRadius: 1, my: 0.5 }}
        />
      )}
      {(job.message || job.error || eta) && (
        <Typography variant="caption" color={job.error ? "error.main" : "text.secondary"}>
          {[
            job.error ?? job.message,
            determinate && job.status === "running" ? `${pct}%` : null,
            eta,
          ]
            .filter(Boolean)
            .join(" · ")}
        </Typography>
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
  // Tick a 1s clock so the ETA countdowns stay live between polls — only while the
  // dropdown is open and something is running (idle otherwise).
  const now = useNow(open && running > 0);

  const poll = useCallback(async () => {
    try {
      const r = await api.jobs();
      setJobs(r.jobs);
      setRunning(r.running);
    } catch {
      /* transient; keep the last snapshot */
    }
  }, []);

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
          jobs.map((j, i) => (
            <Box key={j.id}>
              {i > 0 && <Divider />}
              <JobRow job={j} now={now} />
            </Box>
          ))
        )}
      </Popover>
    </>
  );
}
