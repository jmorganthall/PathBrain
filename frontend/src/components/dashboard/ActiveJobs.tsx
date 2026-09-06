// What the pipeline is doing right now: the running top-level jobs from the jobs feed,
// each with a bar and a countdown. A compact read of the same feed the top-right jobs
// dropdown shows in full — the Dashboard wants "is anything running, and how far along",
// not the chunk-by-chunk detail.
import { Link as RouterLink } from "react-router-dom";
import Box from "@mui/material/Box";
import LinearProgress from "@mui/material/LinearProgress";
import Link from "@mui/material/Link";
import Stack from "@mui/material/Stack";
import Typography from "@mui/material/Typography";

import type { Job } from "../../api/types";
import { fmtDuration } from "../../utils/format";
import { useNow } from "../../utils/useNow";

interface Props {
  jobs: Job[];
  // Client clock at the moment the feed was received, so `eta_ms` can tick down between
  // polls without reading the server's timestamps against the browser's clock.
  receivedAt: number;
  limit?: number;
}

export default function ActiveJobs({ jobs, receivedAt, limit = 4 }: Props) {
  const running = jobs.filter((j) => j.status === "running" && !j.parent_id).slice(0, limit);
  // The clock lives here, not on the page: a one-second tick re-rendering the whole
  // dashboard (charts included) for the sake of one countdown is the wrong trade.
  const now = useNow(running.length > 0);
  if (running.length === 0) {
    return (
      <Typography variant="body2" color="text.secondary">
        Nothing running. The pipeline is idle.
      </Typography>
    );
  }
  const elapsed = Math.max(0, now - receivedAt);
  return (
    <Stack spacing={1.25}>
      {running.map((j) => {
        const queued = !!j.queued;
        let pct: number | null = null;
        if (!queued && j.total != null && j.total > 0 && j.current != null) {
          pct = Math.min(100, (j.current / j.total) * 100);
        } else if (!queued && j.window_ms != null && j.window_ms > 0 && j.eta_ms != null) {
          const left = Math.max(0, j.eta_ms - elapsed);
          pct = Math.min(99, ((j.window_ms - left) / j.window_ms) * 100);
        }
        const eta =
          j.eta_ms == null
            ? null
            : queued
              ? `${fmtDuration(j.eta_ms)} once it starts`
              : j.eta_ms - elapsed > 1000
                ? `${fmtDuration(j.eta_ms - elapsed)} left`
                : "finishing…";
        const stalled = j.stalled_ms != null && j.stalled_ms > 0;
        return (
          <Box key={j.id}>
            <Stack direction="row" spacing={1} alignItems="baseline" justifyContent="space-between" sx={{ minWidth: 0 }}>
              <Typography variant="body2" noWrap sx={{ minWidth: 0, fontWeight: 500 }} title={j.detail ?? undefined}>
                {j.href ? (
                  <Link component={RouterLink} to={j.href} underline="hover" color="inherit">
                    {j.label}
                  </Link>
                ) : (
                  j.label
                )}
              </Typography>
              <Typography variant="caption" color={stalled ? "warning.main" : "text.secondary"} sx={{ flexShrink: 0 }}>
                {stalled
                  ? `no progress for ${fmtDuration(j.stalled_ms)}`
                  : queued
                    ? `queued${eta ? ` · ${eta}` : ""}`
                    : j.total != null && j.current != null
                      ? `${j.current}/${j.total}${eta ? ` · ${eta}` : ""}`
                      : (eta ?? "running")}
              </Typography>
            </Stack>
            <LinearProgress
              variant={pct == null ? "indeterminate" : "determinate"}
              value={pct ?? undefined}
              color={stalled ? "warning" : "primary"}
              sx={{ mt: 0.5, height: 6, borderRadius: 3, bgcolor: "rgba(255,255,255,0.06)" }}
            />
            {j.message && (
              <Typography variant="caption" color="text.disabled" noWrap component="div" sx={{ mt: 0.25 }}>
                {j.message}
              </Typography>
            )}
          </Box>
        );
      })}
    </Stack>
  );
}
