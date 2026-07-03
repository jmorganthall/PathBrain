import { useCallback, useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Snackbar from "@mui/material/Snackbar";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import RestartAltIcon from "@mui/icons-material/RestartAlt";

import { api } from "../api/client";
import type {
  MethodologyDetail,
  MethodologyMetric,
  MethodologySummary,
} from "../api/types";
import Loading from "../components/Loading";
import { fmtDateTime } from "../utils/format";

function fmtBound(v: number | null, unit: string): string {
  if (v == null) return "—";
  const n = Number.isInteger(v) ? v.toString() : v.toFixed(2);
  return `${n}${unit ? " " + unit : ""}`;
}

// The frozen metric table for one methodology, grouped by axis (display-only last).
function MetricTable({ metrics }: { metrics: MethodologyMetric[] }) {
  const axes = Array.from(new Set(metrics.map((m) => m.axis ?? "display")));
  return (
    <TableContainer sx={{ mt: 1 }}>
      <Table size="small">
        <TableHead>
          <TableRow>
            <TableCell>Metric</TableCell>
            <TableCell>Axis</TableCell>
            <TableCell align="right">Weight</TableCell>
            <TableCell align="right">Best</TableCell>
            <TableCell align="right">Worst</TableCell>
          </TableRow>
        </TableHead>
        <TableBody>
          {axes.flatMap((axis) =>
            metrics
              .filter((m) => (m.axis ?? "display") === axis)
              .map((m) => (
                <TableRow key={m.key}>
                  <TableCell>
                    <Tooltip arrow title={m.description}>
                      <Box component="span" sx={{ cursor: "help" }}>
                        {m.label}
                        {m.required && (
                          <Chip
                            size="small"
                            label="required"
                            color="info"
                            variant="outlined"
                            sx={{ ml: 1, height: 18, fontSize: "0.6rem" }}
                          />
                        )}
                      </Box>
                    </Tooltip>
                  </TableCell>
                  <TableCell>
                    <Typography variant="caption" color="text.secondary">
                      {m.axis ?? "display-only"}
                    </Typography>
                  </TableCell>
                  <TableCell align="right">{m.axis ? m.weight : "—"}</TableCell>
                  <TableCell align="right">{fmtBound(m.best, m.unit)}</TableCell>
                  <TableCell align="right">{fmtBound(m.worst, m.unit)}</TableCell>
                </TableRow>
              )),
          )}
        </TableBody>
      </Table>
    </TableContainer>
  );
}

function VersionRow({ m }: { m: MethodologySummary }) {
  const recorded = m.metric_count > 0;
  return (
    <Box sx={{ py: 1 }}>
      <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
        <Typography variant="subtitle2">{m.version}</Typography>
        {m.is_current && <Chip size="small" color="success" label="current" />}
        {!recorded && (
          <Tooltip title="This version predates the methodology layer, so its full rubric wasn't recorded. Its scores survive; its definition can't be reconstructed.">
            <Chip size="small" variant="outlined" color="warning" label="definition not recorded" />
          </Tooltip>
        )}
        <Typography variant="caption" color="text.secondary">
          derivation {m.derivation_version} · {m.created_at ? fmtDateTime(m.created_at) : "—"}
        </Typography>
      </Stack>
      {recorded && (
        <Typography variant="caption" color="text.secondary">
          {m.scored_metric_count} scored metric(s) across {m.axes.map((a) => a.label).join(" + ")}
          {m.required_metrics.length > 0 && <> · requires {m.required_metrics.join(", ")}</>}
        </Typography>
      )}
      {m.notes && (
        <Typography variant="body2" sx={{ mt: 0.5 }}>
          {m.notes}
        </Typography>
      )}
    </Box>
  );
}

export default function Methodology() {
  const [current, setCurrent] = useState<MethodologyDetail | null>(null);
  const [versions, setVersions] = useState<MethodologySummary[]>([]);
  // Confidence threshold — total iterations a profile needs before it's crownable /
  // comparable (correlation.min_iterations). Lives in runtime config, shown here so the
  // rubric page states not just how a run is scored but when a profile is trusted.
  const [minIterations, setMinIterations] = useState<number | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [regrading, setRegrading] = useState(false);
  const [toast, setToast] = useState<string | null>(null);
  // A pending re-anchor proposal, deep-linked from the Settings-Impact saturation alert
  // (?reanchor=<metric>&best=<suggested>). The 'best' is editable before publishing.
  const [searchParams, setSearchParams] = useSearchParams();
  const reanchorKey = searchParams.get("reanchor");
  const suggestedBest = searchParams.get("best");
  const [proposalBest, setProposalBest] = useState("");
  const [publishing, setPublishing] = useState(false);

  useEffect(() => {
    if (suggestedBest != null) setProposalBest(suggestedBest);
  }, [suggestedBest]);

  const load = useCallback(async () => {
    try {
      const [cur, list, cfg] = await Promise.all([
        api.methodologyCurrent(),
        api.methodologies(),
        api.config(),
      ]);
      setCurrent(cur);
      setVersions(list.methodologies);
      setMinIterations(cfg.correlation?.min_iterations ?? null);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load methodologies");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const handleRegrade = useCallback(async () => {
    setRegrading(true);
    try {
      await api.regradeHistory();
      setToast("Re-grade started — track its progress in the jobs menu (top right) ↗");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not start the re-grade");
    } finally {
      setRegrading(false);
    }
  }, []);

  const proposalMetric =
    reanchorKey && current
      ? current.definition.metrics.find((m) => m.key === reanchorKey) ?? null
      : null;

  const handlePublish = useCallback(async () => {
    if (!proposalMetric) return;
    const best = Number(proposalBest);
    if (!Number.isFinite(best)) {
      setError("Enter a numeric “best” value");
      return;
    }
    setPublishing(true);
    try {
      const res = await api.reanchorMetric(proposalMetric.key, best);
      setToast(`Published ${res.version} and started a re-grade — track it in the jobs menu (top right) ↗`);
      setSearchParams({}, { replace: true }); // clear the proposal from the URL
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not publish the re-anchor");
    } finally {
      setPublishing(false);
    }
  }, [proposalMetric, proposalBest, setSearchParams, load]);

  if (loading) return <Loading label="Loading methodology…" />;

  const others = versions.filter((v) => v.version !== current?.version);

  return (
    <Box>
      <Stack
        direction={{ xs: "column", sm: "row" }}
        justifyContent="space-between"
        alignItems={{ xs: "flex-start", sm: "center" }}
        spacing={1}
        sx={{ mb: 1 }}
      >
        <Typography variant="h4">Methodology</Typography>
        <Tooltip title="Score every run from its preserved raw observations under the current methodology. Never changes a run's at-measure (capture-time) score; writes the at-present score.">
          <span>
            <Button
              variant="outlined"
              startIcon={regrading ? <CircularProgress size={16} /> : <RestartAltIcon />}
              onClick={handleRegrade}
              disabled={regrading}
            >
              {regrading ? "Re-grading…" : "Re-grade history under current"}
            </Button>
          </span>
        </Tooltip>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        How raw observations become a score, versioned. Raw data is the instrumented truth; the
        methodology is the interpretation applied to it. Changing a weight, threshold, or metric
        publishes a new version — old scores keep the methodology they were measured under, and any
        run can be re-scored from its preserved raw under the current one (when comparable).
      </Typography>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {reanchorKey && !proposalMetric && (
        <Alert severity="info" sx={{ mb: 2 }} onClose={() => setSearchParams({}, { replace: true })}>
          “{reanchorKey}” isn’t a scored metric in the current methodology, so it can’t be re-anchored.
        </Alert>
      )}

      {proposalMetric && (
        <Card sx={{ mb: 2, border: 1, borderColor: "warning.main" }}>
          <CardContent>
            <Typography variant="h6" gutterBottom>
              Proposed re-anchor — {proposalMetric.label}
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
              From the saturation check on Settings Impact: <b>{proposalMetric.label}</b> already clears
              its “best” for most profiles, so it can’t rank them. Tightening “best” publishes a new
              methodology version (forked from <b>{current?.version}</b> — append-only, nothing edited in
              place) and re-grades history onto it, so the fastest profile scores highest.
            </Typography>
            <Stack direction="row" spacing={2} alignItems="center" flexWrap="wrap" useFlexGap>
              <Typography variant="body2">
                Current best: <b>{fmtBound(proposalMetric.best, proposalMetric.unit)}</b>
              </Typography>
              <Typography variant="body2" color="text.secondary">
                →
              </Typography>
              <TextField
                label="New best"
                size="small"
                type="number"
                value={proposalBest}
                onChange={(e) => setProposalBest(e.target.value)}
                InputProps={{
                  endAdornment: proposalMetric.unit ? (
                    <Typography variant="caption" color="text.secondary">
                      {proposalMetric.unit}
                    </Typography>
                  ) : null,
                }}
                sx={{ width: 170 }}
              />
              <Button
                variant="contained"
                color="secondary"
                onClick={handlePublish}
                disabled={publishing}
                startIcon={publishing ? <CircularProgress size={16} /> : undefined}
              >
                {publishing ? "Publishing…" : "Publish new version & re-grade"}
              </Button>
              <Button onClick={() => setSearchParams({}, { replace: true })} disabled={publishing}>
                Dismiss
              </Button>
            </Stack>
          </CardContent>
        </Card>
      )}

      {current && (
        <Card sx={{ mb: 2 }}>
          <CardContent>
            <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
              <Typography variant="h6">{current.version}</Typography>
              <Chip size="small" color="success" label="current" />
              {current.axes.map((a) => (
                <Chip key={a.key} size="small" variant="outlined" label={a.label} />
              ))}
            </Stack>
            <Typography variant="caption" color="text.secondary">
              derivation {current.derivation_version}
              {current.created_at ? ` · recorded ${fmtDateTime(current.created_at)}` : ""}
            </Typography>
            {current.notes && (
              <Typography variant="body2" sx={{ mt: 0.5 }}>
                {current.notes}
              </Typography>
            )}
            <MetricTable metrics={current.definition.metrics} />
          </CardContent>
        </Card>
      )}

      {minIterations != null && (
        <Card sx={{ mb: 2 }}>
          <CardContent>
            <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
              <Typography variant="h6">Confidence</Typography>
              <Chip size="small" color="info" variant="outlined" label={`${minIterations} iterations`} />
            </Stack>
            <Typography variant="body2" color="text.secondary" sx={{ mt: 0.5 }}>
              A profile is treated as <b>confident</b> — eligible to be crowned the “best” and
              counted in significance calls — once its runs total at least <b>{minIterations}</b>{" "}
              iterations. Iterations, not run count, are the unit of signal: a {minIterations}
              -iteration run carries far more than a single-iteration one. Below the threshold a
              profile is still measured and shown, but can’t win the crown until it gets there
              (that’s what “Test to minimum” and the challenger race drive toward). Adjust it via{" "}
              <code>correlation.min_iterations</code> in the runtime config.
            </Typography>
          </CardContent>
        </Card>
      )}

      {others.length > 0 && (
        <Card>
          <CardContent>
            <Typography variant="h6" gutterBottom>
              Other versions ({others.length})
            </Typography>
            {others.map((m, i) => (
              <Box key={m.version}>
                {i > 0 && <Box sx={{ borderTop: "1px solid", borderColor: "divider" }} />}
                <VersionRow m={m} />
              </Box>
            ))}
          </CardContent>
        </Card>
      )}

      <Snackbar
        open={toast != null}
        autoHideDuration={6000}
        onClose={() => setToast(null)}
        message={toast ?? ""}
        anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
      />
    </Box>
  );
}
