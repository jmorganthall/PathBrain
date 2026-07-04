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

import { api, JOBS_REFRESH_EVENT } from "../api/client";
import type { Job } from "../api/types";
import { fmtTimeShort } from "../utils/format";

const ACTIVE_POLL_MS = 2000;
const IDLE_POLL_MS = 10000;

function StatusIcon({ status }: { status: Job["status"] }) {
  if (status === "running") return <CircularProgress size={16} />;
  if (status === "succeeded") return <CheckCircleIcon color="success" fontSize="small" />;
  return <ErrorIcon color="error" fontSize="small" />;
}

function JobRow({ job }: { job: Job }) {
  const determinate = job.total != null && job.total > 0 && job.current != null;
  const pct = determinate ? Math.min(100, Math.round((job.current! / job.total!) * 100)) : 0;
  return (
    <Box sx={{ px: 2, py: 1.25 }}>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.25 }}>
        <StatusIcon status={job.status} />
        <Typography variant="body2" sx={{ fontWeight: 600, flexGrow: 1, wordBreak: "break-word" }}>
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
      </Stack>
      {job.status === "running" && (
        <LinearProgress
          variant={determinate ? "determinate" : "indeterminate"}
          value={pct}
          sx={{ borderRadius: 1, my: 0.5 }}
        />
      )}
      {(job.message || job.error) && (
        <Typography variant="caption" color={job.error ? "error.main" : "text.secondary"}>
          {job.error ?? job.message}
          {determinate && job.status === "running" ? ` · ${pct}%` : ""}
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
          jobs.map((j, i) => (
            <Box key={j.id}>
              {i > 0 && <Divider />}
              <JobRow job={j} />
            </Box>
          ))
        )}
      </Popover>
    </>
  );
}
