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
import Typography from "@mui/material/Typography";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import RefreshIcon from "@mui/icons-material/Refresh";
import SpeedIcon from "@mui/icons-material/Speed";

import { api, ApiError } from "../api/client";
import type { RunDetail, SeriesPoint } from "../api/types";
import ScoreGauge from "../components/ScoreGauge";
import SubscoreBreakdown from "../components/SubscoreBreakdown";
import SeriesChart from "../components/SeriesChart";
import StatusChip from "../components/StatusChip";
import Loading from "../components/Loading";
import EmptyState from "../components/EmptyState";
import { fmtDateTime } from "../utils/format";

const isRunning = (s: string) => ["running", "pending", "queued"].includes(s.toLowerCase());

export default function Dashboard() {
  const [latest, setLatest] = useState<RunDetail | null>(null);
  const [series, setSeries] = useState<SeriesPoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);
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
      const [, s] = await Promise.all([
        loadLatest(),
        api.historySeries(100).then((r) => setSeries(r.points)),
      ]);
      void s;
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
      const d = await api.triggerRun({});
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
  }, [poll]);

  const activeRun = running || (latest != null && isRunning(latest.status));

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
        <Stack direction="row" spacing={1}>
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
      </Stack>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
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
              {activeRun && (
                <Box sx={{ width: "100%" }}>
                  <LinearProgress />
                  <Typography variant="caption" color="text.secondary">
                    Benchmark in progress…
                  </Typography>
                </Box>
              )}
              <ScoreGauge value={latest.score?.sops ?? null} />
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
              {latest.error && (
                <Alert severity="error" sx={{ width: "100%" }}>
                  {latest.error}
                </Alert>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardContent>
              <Typography variant="h6" gutterBottom>
                Subscore Breakdown
              </Typography>
              {latest.score ? (
                <SubscoreBreakdown score={latest.score} />
              ) : (
                <Typography variant="body2" color="text.secondary">
                  Score not available for this run yet.
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
