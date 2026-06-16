import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, Link as RouterLink } from "react-router-dom";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import Divider from "@mui/material/Divider";
import LinearProgress from "@mui/material/LinearProgress";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableRow from "@mui/material/TableRow";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import ArrowBackIcon from "@mui/icons-material/ArrowBack";
import CheckCircleIcon from "@mui/icons-material/CheckCircle";
import ErrorIcon from "@mui/icons-material/Error";
import InfoOutlinedIcon from "@mui/icons-material/InfoOutlined";

import { api } from "../api/client";
import type { RunBaseline, RunDetail as RunDetailType, RunEstimate } from "../api/types";
import ScoreGauge from "../components/ScoreGauge";
import SubscoreBreakdown from "../components/SubscoreBreakdown";
import StatusChip from "../components/StatusChip";
import JsonViewer from "../components/JsonViewer";
import Loading from "../components/Loading";
import MetricDelta from "../components/MetricDelta";
import { getMetricMeta } from "../utils/metrics";
import { fmtDateTime, fmtDuration, metricValue, parseApiDate } from "../utils/format";

const isRunning = (s: string) => ["running", "pending", "queued"].includes(s.toLowerCase());

export default function RunDetail() {
  const { id } = useParams<{ id: string }>();
  const runId = Number(id);
  const [run, setRun] = useState<RunDetailType | null>(null);
  const [baseline, setBaseline] = useState<RunBaseline | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [estimate, setEstimate] = useState<RunEstimate | null>(null);
  const [now, setNow] = useState<number>(Date.now());
  const [cancelling, setCancelling] = useState(false);
  const pollRef = useRef<number | null>(null);

  const cancelRun = useCallback(async () => {
    setCancelling(true);
    try {
      setRun(await api.cancelRun(runId));
    } catch {
      /* ignore; poll will refresh */
    } finally {
      setCancelling(false);
    }
  }, [runId]);

  const load = useCallback(async () => {
    try {
      const d = await api.result(runId);
      setRun(d);
      setError(null);
      return d;
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load run");
      return null;
    } finally {
      setLoading(false);
    }
  }, [runId]);

  useEffect(() => {
    if (Number.isNaN(runId)) {
      setError("Invalid run id");
      setLoading(false);
      return;
    }
    setLoading(true);
    load().then((d) => {
      if (d && isRunning(d.status)) {
        pollRef.current = window.setInterval(async () => {
          const updated = await load();
          if (updated && !isRunning(updated.status) && pollRef.current) {
            window.clearInterval(pollRef.current);
            pollRef.current = null;
          }
        }, 2000);
      }
    });
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
    };
  }, [runId, load]);

  // Fetch the per-iteration estimate once, to drive the live ETA.
  useEffect(() => {
    api.runEstimate().then(setEstimate).catch(() => {});
  }, []);

  // Once the run is finished, fetch the profile-average baseline so we can show
  // improved/worse arrows next to each metric.
  useEffect(() => {
    if (!run || isRunning(run.status)) return;
    api.resultBaseline(runId).then(setBaseline).catch(() => setBaseline(null));
  }, [runId, run?.status]);

  // Tick a 1s clock while the run is in progress so the ETA counts down.
  useEffect(() => {
    if (!run || !isRunning(run.status)) return;
    setNow(Date.now());
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [run?.status, run?.started_at]);

  if (loading) return <Loading label="Loading run…" />;

  if (!run) {
    return (
      <Box>
        <Button component={RouterLink} to="/history" startIcon={<ArrowBackIcon />} sx={{ mb: 2 }}>
          Back to History
        </Button>
        <Alert severity="error">{error ?? "Run not found"}</Alert>
      </Box>
    );
  }

  return (
    <Box>
      <Button component={RouterLink} to="/history" startIcon={<ArrowBackIcon />} sx={{ mb: 2 }}>
        Back to History
      </Button>

      <Stack
        direction={{ xs: "column", sm: "row" }}
        justifyContent="space-between"
        alignItems={{ xs: "flex-start", sm: "center" }}
        spacing={1}
        sx={{ mb: 2 }}
      >
        <Typography variant="h4">Run #{run.id}</Typography>
        <Stack direction="row" spacing={1} alignItems="center">
          {isRunning(run.status) && (
            <Button size="small" color="error" variant="outlined" onClick={cancelRun} disabled={cancelling}>
              {cancelling ? "Cancelling…" : "Cancel run"}
            </Button>
          )}
          <StatusChip status={run.status} />
        </Stack>
      </Stack>

      {isRunning(run.status) && (() => {
        const started = run.started_at ? parseApiDate(run.started_at).getTime() : null;
        const elapsedMs = started != null ? Math.max(now - started, 0) : null;
        const estTotalMs =
          estimate?.per_iteration_ms != null
            ? estimate.per_iteration_ms * (run.iterations || 1)
            : null;
        const haveEta = elapsedMs != null && estTotalMs != null;
        const overdue = haveEta && elapsedMs >= estTotalMs;
        const pct = haveEta ? Math.min((elapsedMs / estTotalMs) * 100, 100) : null;
        const remainingMs = haveEta ? Math.max(estTotalMs - elapsedMs, 0) : null;
        const iterInfo =
          run.iterations > 1
            ? ` · iteration ${Math.min(run.iterations_completed + 1, run.iterations)} of ${run.iterations}`
            : "";

        let caption: string;
        if (!haveEta) {
          caption =
            elapsedMs != null
              ? `Running for ${fmtDuration(elapsedMs)} — auto-refreshing…`
              : "Run in progress — auto-refreshing…";
        } else if (overdue) {
          caption = `Any second now… (${fmtDuration(elapsedMs)} elapsed, est. ${fmtDuration(estTotalMs)})`;
        } else {
          caption = `${fmtDuration(elapsedMs)} elapsed · ~${fmtDuration(remainingMs)} remaining (est. ${fmtDuration(estTotalMs)})`;
        }

        return (
          <Box sx={{ mb: 2 }}>
            <LinearProgress
              variant={haveEta && !overdue ? "determinate" : "indeterminate"}
              value={haveEta && !overdue ? (pct as number) : undefined}
            />
            <Typography variant="caption" color="text.secondary">
              {caption}
              {iterInfo}
            </Typography>
          </Box>
        );
      })()}

      {run.error && (
        <Alert severity="error" sx={{ mb: 2 }}>
          {run.error}
        </Alert>
      )}

      <Box
        sx={{
          display: "grid",
          gap: 2,
          gridTemplateColumns: { xs: "1fr", md: "minmax(260px, 340px) 1fr" },
          mb: 2,
        }}
      >
        <Card>
          <CardContent sx={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 2 }}>
            <ScoreGauge value={run.score?.sops ?? null} label="Seat of Pants Score (how it feels)" />
            {run.score && run.score.sops_stdev != null && run.iterations > 1 && (
              <Typography variant="caption" color="text.secondary" sx={{ textAlign: "center" }}>
                ± {run.score.sops_stdev} · range {run.score.sops_min}–{run.score.sops_max} over{" "}
                {run.iterations} iterations
              </Typography>
            )}
            {run.score?.completion != null && (
              <>
                <Divider flexItem />
                <ScoreGauge
                  value={run.score.completion}
                  size={150}
                  label="Completion Score (infra timing)"
                />
                {run.score.completion_stdev != null && run.iterations > 1 && (
                  <Typography variant="caption" color="text.secondary" sx={{ textAlign: "center" }}>
                    ± {run.score.completion_stdev} · range {run.score.completion_min}–
                    {run.score.completion_max}
                  </Typography>
                )}
                <Typography variant="caption" color="text.secondary" sx={{ textAlign: "center" }}>
                  Raw connection timing (DNS/TCP/TLS/jitter/loss), separate from SOPS.
                </Typography>
              </>
            )}
            <Stack spacing={0.5} alignItems="center">
              {run.label && <Chip size="small" label={run.label} />}
              <Typography variant="caption" color="text.secondary">
                Created {fmtDateTime(run.created_at)}
              </Typography>
              {run.finished_at && (
                <Typography variant="caption" color="text.secondary">
                  Finished {fmtDateTime(run.finished_at)}
                </Typography>
              )}
              <Chip
                size="small"
                variant="outlined"
                label={
                  `${run.iterations} iteration${run.iterations === 1 ? "" : "s"}` +
                  (run.per_iteration_ms != null ? ` · ~${fmtDuration(run.per_iteration_ms)} each` : "")
                }
              />
              {run.settings_fingerprint && (
                <Chip
                  size="small"
                  variant="outlined"
                  label={`SQM profile ${run.settings_fingerprint}`}
                  title="Firewall/SQM settings profile in effect during this run"
                />
              )}
            </Stack>
            {run.notes && (
              <Typography variant="body2" color="text.secondary" sx={{ textAlign: "center" }}>
                {run.notes}
              </Typography>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardContent>
            <Typography variant="h6" gutterBottom>
              Score Breakdown
            </Typography>
            {run.score ? (
              <>
                <Typography variant="overline" color="text.secondary">
                  Seat of Pants · how it feels {Math.round(run.score.sops)}
                </Typography>
                <SubscoreBreakdown score={run.score} />
                {run.score.completion_subscores &&
                  Object.keys(run.score.completion_subscores).length > 0 && (
                    <>
                      <Divider sx={{ my: 2 }} />
                      <Typography variant="overline" color="text.secondary">
                        Completion · infra{" "}
                        {run.score.completion != null ? Math.round(run.score.completion) : "—"}
                      </Typography>
                      <SubscoreBreakdown
                        score={{
                          subscores: run.score.completion_subscores,
                          weights_used: run.score.completion_weights_used ?? {},
                          metric_values: run.score.completion_metric_values ?? {},
                        }}
                      />
                    </>
                  )}
              </>
            ) : (
              <Typography variant="body2" color="text.secondary">
                No score computed for this run.
              </Typography>
            )}
          </CardContent>
        </Card>
      </Box>

      <Typography variant="h6" sx={{ mb: 0.5 }}>
        Plugin Results
      </Typography>
      <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.5 }}>
        Hover or tap a metric name for what it means.
        {baseline && baseline.run_count > 0 && (
          <>
            {" "}
            {baseline.scope === "all" ? (
              <>
                Arrows compare this run to the recent average over {baseline.run_count} run
                {baseline.run_count === 1 ? "" : "s"}
              </>
            ) : baseline.is_best_profile ? (
              <>
                This run is on your <strong>best-scoring profile</strong>
                {baseline.profile_median_sops != null && ` (SOPS ${Math.round(baseline.profile_median_sops)})`}
                ; arrows compare it to that profile's own average over {baseline.run_count} run
                {baseline.run_count === 1 ? "" : "s"}
              </>
            ) : (
              <>
                Arrows compare this run to your <strong>best-scoring profile</strong>
                {baseline.profile_label ? ` (${baseline.profile_label}` : ""}
                {baseline.profile_label && baseline.profile_median_sops != null
                  ? `, SOPS ${Math.round(baseline.profile_median_sops)})`
                  : baseline.profile_label
                  ? ")"
                  : ""}{" "}
                over {baseline.run_count} run{baseline.run_count === 1 ? "" : "s"}
              </>
            )}{" "}
            —{" "}
            <Box component="span" sx={{ color: "success.main", fontWeight: 700 }}>▲ better</Box>,{" "}
            <Box component="span" sx={{ color: "error.main", fontWeight: 700 }}>▼ worse</Box>.
          </>
        )}
      </Typography>

      {run.results.length === 0 ? (
        <Typography variant="body2" color="text.secondary">
          No plugin results recorded.
        </Typography>
      ) : (
        <Box
          sx={{
            display: "grid",
            gap: 2,
            gridTemplateColumns: { xs: "1fr", md: "1fr 1fr" },
          }}
        >
          {run.results.map((res) => {
            const metricKeys = Object.keys(res.metrics);
            const metricStats = ((res.details as Record<string, unknown> | null)
              ?.metric_stats ?? {}) as Record<string, { stdev?: number; n?: number }>;
            return (
              <Card key={res.id}>
                <CardContent>
                  <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 1 }}>
                    <Stack direction="row" spacing={1} alignItems="center">
                      {res.success ? (
                        <CheckCircleIcon color="success" fontSize="small" />
                      ) : (
                        <ErrorIcon color="error" fontSize="small" />
                      )}
                      <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>
                        {res.plugin}
                      </Typography>
                    </Stack>
                    {res.duration_ms != null && (
                      <Chip size="small" variant="outlined" label={`${res.duration_ms.toFixed(0)} ms`} />
                    )}
                  </Stack>

                  {res.error && (
                    <Alert severity="error" sx={{ mb: 1 }}>
                      {res.error}
                    </Alert>
                  )}

                  {metricKeys.length > 0 && (
                    <Table size="small" sx={{ mb: 1 }}>
                      <TableBody>
                        {metricKeys.map((k) => {
                          const st = metricStats[k];
                          const showStdev = st && (st.n ?? 0) > 1 && (st.stdev ?? 0) > 0;
                          const meta = getMetricMeta(k);
                          const baseValue = baseline?.metrics?.[res.plugin]?.[k];
                          return (
                            <TableRow key={k}>
                              <TableCell sx={{ border: 0, py: 0.5, color: "text.secondary" }}>
                                <Tooltip
                                  arrow
                                  enterTouchDelay={0}
                                  leaveTouchDelay={6000}
                                  title={
                                    <>
                                      <strong>{meta.label}</strong>
                                      <br />
                                      {meta.description}
                                    </>
                                  }
                                >
                                  <Box
                                    component="span"
                                    sx={{
                                      display: "inline-flex",
                                      alignItems: "center",
                                      gap: 0.5,
                                      cursor: "help",
                                    }}
                                  >
                                    {k}
                                    <InfoOutlinedIcon sx={{ fontSize: "0.9rem", opacity: 0.5 }} />
                                  </Box>
                                </Tooltip>
                              </TableCell>
                              <TableCell align="right" sx={{ border: 0, py: 0.5, fontWeight: 600 }}>
                                {metricValue(res.metrics[k])}
                                {showStdev && (
                                  <Typography component="span" variant="caption" color="text.secondary" sx={{ ml: 0.5 }}>
                                    ± {st.stdev}
                                  </Typography>
                                )}
                                {res.success && baseValue != null && (
                                  <MetricDelta
                                    current={res.metrics[k]}
                                    baseline={baseValue}
                                    higherIsBetter={meta.higherIsBetter}
                                    unit={meta.unit}
                                    runCount={baseline?.run_count ?? 0}
                                    scopeLabel={
                                      baseline?.scope !== "best_profile"
                                        ? "recent average"
                                        : baseline?.is_best_profile
                                        ? "best-profile average"
                                        : "best profile"
                                    }
                                  />
                                )}
                              </TableCell>
                            </TableRow>
                          );
                        })}
                      </TableBody>
                    </Table>
                  )}

                  {res.plugin === "browser" && res.details != null && (() => {
                    const perUrl = ((res.details as Record<string, unknown>).per_url ??
                      {}) as Record<string, { screenshot_url?: string | null; har_url?: string | null }>;
                    const shots = Object.entries(perUrl)
                      .map(([url, m]) => ({ url, src: m?.screenshot_url, har: m?.har_url }))
                      .filter((s) => s.src);
                    if (shots.length === 0) return null;
                    return (
                      <Box sx={{ display: "flex", gap: 1.5, flexWrap: "wrap", mb: 1 }}>
                        {shots.map((s) => (
                          <Box key={s.url} sx={{ width: 160 }}>
                            <Box
                              component="a"
                              href={s.src as string}
                              target="_blank"
                              rel="noreferrer"
                              sx={{ display: "block" }}
                            >
                              <Box
                                component="img"
                                src={s.src as string}
                                alt={s.url}
                                loading="lazy"
                                sx={{
                                  width: "100%",
                                  height: 100,
                                  objectFit: "cover",
                                  objectPosition: "top",
                                  borderRadius: 1,
                                  border: "1px solid",
                                  borderColor: "divider",
                                  display: "block",
                                }}
                              />
                            </Box>
                            <Typography
                              variant="caption"
                              color="text.secondary"
                              noWrap
                              title={s.url}
                              sx={{ display: "block" }}
                            >
                              {s.url}
                            </Typography>
                            {s.har && (
                              <Typography variant="caption" component="a" href={s.har} target="_blank" rel="noreferrer">
                                download HAR
                              </Typography>
                            )}
                          </Box>
                        ))}
                      </Box>
                    );
                  })()}

                  {res.details != null && (
                    <>
                      <Divider sx={{ my: 1 }} />
                      <JsonViewer data={res.details} label="raw details" />
                    </>
                  )}
                </CardContent>
              </Card>
            );
          })}
        </Box>
      )}

      {run.config_used && (
        <Card sx={{ mt: 2 }}>
          <CardContent>
            <Typography variant="h6" gutterBottom>
              Config Used
            </Typography>
            <JsonViewer data={run.config_used} label="config" />
          </CardContent>
        </Card>
      )}
    </Box>
  );
}
