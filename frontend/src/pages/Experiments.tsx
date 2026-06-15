import { useCallback, useEffect, useState } from "react";
import { Link as RouterLink } from "react-router-dom";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";
import ScienceIcon from "@mui/icons-material/Science";
import StopCircleIcon from "@mui/icons-material/StopCircle";

import { api } from "../api/client";
import type { ExperimentDetail, ExperimentsResponse } from "../api/types";
import Loading from "../components/Loading";
import EmptyState from "../components/EmptyState";
import { fmtDateTime } from "../utils/format";

const DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

function windowLabel(w: { days: number[]; start_hour: number; end_hour: number }): string {
  const days = w.days?.length ? w.days.map((d) => DAYS[d] ?? d).join(", ") : "any day";
  return `${days} · ${String(w.start_hour).padStart(2, "0")}:00–${String(w.end_hour).padStart(2, "0")}:00 (local)`;
}

export default function Experiments() {
  const [data, setData] = useState<ExperimentsResponse | null>(null);
  const [detail, setDetail] = useState<ExperimentDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setData(await api.experiments());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load experiments");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const handleAbort = useCallback(async () => {
    setBusy(true);
    try {
      await api.abortExperiment();
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Abort failed");
    } finally {
      setBusy(false);
    }
  }, [load]);

  if (loading) return <Loading label="Loading experiments…" />;

  const status = data?.status;

  return (
    <Box>
      <Typography variant="h4" sx={{ mb: 1 }}>
        Experiments
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        Autonomous sweeps of one shaper parameter within an experimentation window. Configure under{" "}
        <RouterLink to="/config">Config → Experiment Engine</RouterLink>. The baseline config is
        restored when the window closes unless auto-promote is on.
      </Typography>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {status && (
        <Card sx={{ mb: 2 }}>
          <CardContent>
            <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
              <ScienceIcon color={status.enabled ? "secondary" : "disabled"} />
              <Chip
                size="small"
                color={status.enabled ? "success" : "default"}
                label={status.enabled ? "armed" : "disarmed"}
              />
              {status.enabled && (
                <Chip
                  size="small"
                  color={status.dry_run ? "info" : "warning"}
                  variant="outlined"
                  label={status.dry_run ? "dry-run (no changes applied)" : "LIVE — applies changes"}
                />
              )}
              <Chip
                size="small"
                variant="outlined"
                color={status.in_window ? "success" : "default"}
                label={status.in_window ? "in window" : "outside window"}
              />
              {status.auto_promote && <Chip size="small" variant="outlined" label="auto-promote" />}
              {status.active_experiment_id != null && (
                <Button
                  size="small"
                  color="error"
                  startIcon={<StopCircleIcon />}
                  onClick={handleAbort}
                  disabled={busy}
                >
                  Abort & restore baseline
                </Button>
              )}
            </Stack>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 1.5 }}>
              Sweeping <b>{status.param}</b> over{" "}
              <b>{status.candidates.length ? status.candidates.join(", ") : "— (no candidates set)"}</b>
              {" · "}
              {windowLabel(status.window)}
            </Typography>
          </CardContent>
        </Card>
      )}

      {!data || data.experiments.length === 0 ? (
        <Card>
          <CardContent>
            <EmptyState
              icon={<ScienceIcon fontSize="inherit" />}
              title="No experiments yet"
              description="Arm the engine and set candidate values in Config → Experiment Engine. During the next window, PathBrain will sweep them and report which performed best."
            />
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent>
            <Typography variant="h6" gutterBottom>
              History ({data.experiments.length})
            </Typography>
            <TableContainer>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>When</TableCell>
                    <TableCell>Param</TableCell>
                    <TableCell>Status</TableCell>
                    <TableCell>Outcome</TableCell>
                    <TableCell align="right">Trials</TableCell>
                    <TableCell />
                  </TableRow>
                </TableHead>
                <TableBody>
                  {data.experiments.map((e) => (
                    <TableRow key={e.id} hover>
                      <TableCell>{fmtDateTime(e.created_at)}</TableCell>
                      <TableCell>
                        {e.param}
                        {e.dry_run && (
                          <Chip size="small" variant="outlined" label="dry-run" sx={{ ml: 1 }} />
                        )}
                      </TableCell>
                      <TableCell>{e.status}</TableCell>
                      <TableCell>
                        {e.result ? (
                          <span>
                            {e.result.action === "promoted" ? "✅ promoted " : "↩ restored "}
                            {e.result.winner != null && (
                              <Typography component="span" variant="caption" color="text.secondary">
                                winner {e.result.winner} ({e.result.winner_median}) vs baseline{" "}
                                {e.result.baseline_value} ({e.result.baseline_median})
                              </Typography>
                            )}
                          </span>
                        ) : (
                          <Typography variant="caption" color="text.secondary">
                            —
                          </Typography>
                        )}
                      </TableCell>
                      <TableCell align="right">{e.trial_count}</TableCell>
                      <TableCell align="right">
                        <Button size="small" onClick={() => api.experiment(e.id).then(setDetail)}>
                          Trials
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableContainer>

            {detail && (
              <Box sx={{ mt: 2 }}>
                <Typography variant="subtitle2" gutterBottom>
                  Experiment #{detail.id} trials
                </Typography>
                <TableContainer>
                  <Table size="small">
                    <TableHead>
                      <TableRow>
                        <TableCell>When</TableCell>
                        <TableCell>Value</TableCell>
                        <TableCell align="right">SOPS</TableCell>
                        <TableCell>Applied</TableCell>
                        <TableCell>Run</TableCell>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {detail.trials.map((t) => (
                        <TableRow key={t.id}>
                          <TableCell>{fmtDateTime(t.created_at)}</TableCell>
                          <TableCell>{t.value}</TableCell>
                          <TableCell align="right">{t.sops ?? "—"}</TableCell>
                          <TableCell>{t.applied ? "yes" : "dry-run"}</TableCell>
                          <TableCell>
                            {t.run_id != null ? (
                              <RouterLink to={`/runs/${t.run_id}`}>#{t.run_id}</RouterLink>
                            ) : (
                              "—"
                            )}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </TableContainer>
              </Box>
            )}
          </CardContent>
        </Card>
      )}
    </Box>
  );
}
