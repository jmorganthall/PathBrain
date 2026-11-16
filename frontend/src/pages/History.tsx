import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import Stack from "@mui/material/Stack";
import Tooltip from "@mui/material/Tooltip";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TablePagination from "@mui/material/TablePagination";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";

import { api } from "../api/client";
import type { RunEstimate, RunSummary, SeriesPoint } from "../api/types";
import StatusChip from "../components/StatusChip";
import SeriesChart from "../components/SeriesChart";
import Loading from "../components/Loading";
import PageHeader from "../components/PageHeader";
import EmptyState from "../components/EmptyState";
import { fmtDateTime, fmtScore, runRemainingMs } from "../utils/format";
import { useNow } from "../utils/useNow";
import { sopsColor } from "../theme";
import { axisColor } from "../utils/axisColors";
import { joinLabels, overallShares, useMethodology } from "../utils/methodology";

const FALLBACK_HEADLINE = [
  { key: "responsiveness", label: "Responsiveness", role: "headline" },
  { key: "smoothness", label: "Smoothness", role: "headline" },
  { key: "speed", label: "Speed", role: "headline" },
];

// SeriesChart reads flat keys, so lift each point's axes, subscores and values to the top.
function flattenSeries(points: SeriesPoint[]) {
  return points.map((p) => {
    const flat: Record<string, unknown> = { ...p };
    for (const [k, v] of Object.entries(p.axes ?? {})) flat[`axis_${k}`] = v;
    for (const [k, v] of Object.entries(p.subscores ?? {})) flat[`sub_${k}`] = v;
    for (const [k, v] of Object.entries(p.values ?? {})) flat[`val_${k}`] = v;
    return flat as SeriesPoint & Record<string, unknown>;
  });
}

const axisOf = (r: RunSummary, key: string): number | null | undefined =>
  r.axes?.[key] ?? (r as unknown as Record<string, number | null | undefined>)[key];

const isRunning = (s: string) => ["running", "pending", "queued"].includes(s.toLowerCase());

export default function History() {
  const navigate = useNavigate();
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [series, setSeries] = useState<SeriesPoint[]>([]);
  const [estimate, setEstimate] = useState<RunEstimate | null>(null);
  const [page, setPage] = useState(0);
  const [rowsPerPage, setRowsPerPage] = useState(25);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  // Legacy runs (scored before the current rubric) aren't comparable — hide them
  // by default; the toggle reveals the archive.
  const [hideLegacy, setHideLegacy] = useState(true);

  const loadPage = useCallback(async (p: number, rpp: number) => {
    try {
      const rows = await api.history(rpp, p * rpp);
      setRuns(rows);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load history");
    }
  }, []);

  useEffect(() => {
    (async () => {
      setLoading(true);
      try {
        const [c, s] = await Promise.all([api.historyCount(), api.historySeries(100)]);
        setTotal(c.count);
        setSeries(s.points);
        api.runEstimate().then(setEstimate).catch(() => {});
        await loadPage(0, rowsPerPage);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to load history");
      } finally {
        setLoading(false);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const handlePage = (_e: unknown, next: number) => {
    setPage(next);
    loadPage(next, rowsPerPage);
  };
  const handleRowsPerPage = (e: React.ChangeEvent<HTMLInputElement>) => {
    const rpp = parseInt(e.target.value, 10);
    setRowsPerPage(rpp);
    setPage(0);
    loadPage(0, rpp);
  };

  const toggleLegacy = async () => {
    const next = !hideLegacy;
    setHideLegacy(next);
    try {
      const s = await api.historySeries(100, !next); // includeLegacy = !hideLegacy
      setSeries(s.points);
    } catch {
      /* keep existing series on failure */
    }
  };

  // Tick a clock only while something on the page is in progress, to drive ETAs.
  const now = useNow(runs.some((r) => isRunning(r.status)));

  if (loading) return <Loading label="Loading history…" />;

  const shownRuns = hideLegacy ? runs.filter((r) => !r.legacy) : runs;

  // Axes, the Overall's inputs and their labels come from the current methodology, so
  // the charts and columns follow it. Until it loads, the three headline axes stand in.
  const methodology = useMethodology();
  const axisNames = methodology?.definition.axes.map((a) => ({ key: a.key, label: a.label })) ?? [];
  const headline =
    methodology?.definition.axes.filter((a) => a.role === "headline") ?? FALLBACK_HEADLINE;
  const overallKeys = overallShares(methodology).map((o) => o.key);
  const chartData = flattenSeries(series);
  const overallMethod = methodology?.definition.overall?.method ?? "weighted";
  const metricLabel = (k: string) =>
    methodology?.definition.metrics.find((m) => m.key === k)?.label ?? k;
  const msKeys = overallKeys.filter(
    (k) => methodology?.definition.metrics.find((m) => m.key === k)?.unit === "ms"
  );
  const hiddenCount = runs.length - shownRuns.length;

  return (
    <Box>
      <PageHeader
        title="Run history"
        lead="Every benchmark run, newest first, with its scores and the profile it measured. Click a run for its full detail."
        compact
      />

      {error && (
        <Alert severity="error" sx={{ mb: 2 }}>
          {error}
        </Alert>
      )}

      {total === 0 ? (
        <Card>
          <CardContent>
            <EmptyState
              title="No runs recorded"
              description="Run a benchmark from the Dashboard to start building history."
            />
          </CardContent>
        </Card>
      ) : (
        <Box sx={{ display: "grid", gap: 2 }}>
          {/* Charts first */}
          <Box
            sx={{
              display: "grid",
              gap: 2,
              gridTemplateColumns: { xs: "1fr", lg: "1fr 1fr" },
            }}
          >
            <Card>
              <CardContent>
                <Typography variant="h6" gutterBottom>
                  {joinLabels(
                    ["overall", ...headline.map((a) => a.key)],
                    [{ key: "overall", label: "Overall" }, ...axisNames]
                  )}
                </Typography>
                <SeriesChart
                  data={chartData}
                  yDomain={[0, 100]}
                  lines={[
                    { key: "overall", name: "Overall", color: axisColor("overall") },
                    ...headline.map((a, i) => ({
                      key: `axis_${a.key}`,
                      name: a.label,
                      color: axisColor(a.key, i),
                    })),
                  ]}
                />
              </CardContent>
            </Card>

            <Card>
              <CardContent>
                <Typography variant="h6" gutterBottom>
                  What makes up the Overall (score)
                </Typography>
                <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1 }}>
                  {overallKeys.length > 0
                    ? `Each input's 0 to 100 score; the Overall is their ${overallMethod} average.`
                    : "Loading the current methodology…"}
                </Typography>
                <SeriesChart
                  data={chartData}
                  yDomain={[0, 100]}
                  lines={overallKeys.map((k, i) => ({
                    key: `sub_${k}`,
                    name: metricLabel(k),
                    color: axisColor(k, i),
                  }))}
                />
              </CardContent>
            </Card>

            {msKeys.length > 0 && (
              <Card sx={{ gridColumn: { lg: "1 / -1" } }}>
                <CardContent>
                  <Typography variant="h6" gutterBottom>
                    {joinLabels(msKeys, methodology?.definition.metrics)} (ms)
                  </Typography>
                  <SeriesChart
                    data={chartData}
                    unit="ms"
                    height={220}
                    lines={msKeys.map((k, i) => ({
                      key: `val_${k}`,
                      name: metricLabel(k),
                      color: axisColor(k, i),
                    }))}
                  />
                </CardContent>
              </Card>
            )}
          </Box>

          {/* Runs list (paginated) below the graphs */}
          <Card>
            <CardContent>
              <Stack
                direction="row"
                justifyContent="space-between"
                alignItems="center"
                spacing={1}
                sx={{ mb: 1 }}
              >
                <Typography variant="h6">Runs ({total})</Typography>
                <Chip
                  size="small"
                  variant={hideLegacy ? "outlined" : "filled"}
                  color={hideLegacy ? "default" : "primary"}
                  onClick={toggleLegacy}
                  label={hideLegacy ? "Show legacy runs" : "Hide legacy runs"}
                />
              </Stack>
              {hideLegacy && hiddenCount > 0 && (
                <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1 }}>
                  {hiddenCount} legacy run{hiddenCount === 1 ? "" : "s"} on this page hidden — scored
                  before the current methodology, so not comparable. The page count still includes them.
                </Typography>
              )}
              <TableContainer>
                <Table size="small">
                  <TableHead>
                    <TableRow>
                      <TableCell>ID</TableCell>
                      <TableCell>Time</TableCell>
                      <TableCell>Label</TableCell>
                      <TableCell>Status</TableCell>
                      <TableCell align="right">Overall</TableCell>
                      {headline.map((a) => (
                        <TableCell key={a.key} align="right">
                          {a.label}
                        </TableCell>
                      ))}
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {shownRuns.map((r) => (
                      <TableRow
                        key={r.id}
                        hover
                        sx={{ cursor: "pointer", opacity: r.legacy ? 0.6 : 1 }}
                        onClick={() => navigate(`/runs/${r.id}`)}
                      >
                        <TableCell>#{r.id}</TableCell>
                        <TableCell>{fmtDateTime(r.created_at)}</TableCell>
                        <TableCell>{r.label ?? "—"}</TableCell>
                        <TableCell>
                          <StatusChip
                            status={r.status}
                            etaMs={
                              isRunning(r.status)
                                ? runRemainingMs(
                                    r.started_at,
                                    r.iterations,
                                    estimate?.per_iteration_ms,
                                    now,
                                  )
                                : null
                            }
                          />
                        </TableCell>
                        {r.legacy ? (
                          <TableCell align="right" colSpan={1 + headline.length}>
                            <Tooltip title="Not comparable under the current methodology — re-grade or re-run.">
                              <Chip size="small" variant="outlined" label="legacy" />
                            </Tooltip>
                          </TableCell>
                        ) : (
                          <>
                            <TableCell align="right">
                              <Typography component="span" sx={{ fontWeight: 700, color: sopsColor(r.overall) }}>
                                {fmtScore(r.overall)}
                              </Typography>
                            </TableCell>
                            {headline.map((ax) => {
                              const v = axisOf(r, ax.key);
                              return (
                                <TableCell key={ax.key} align="right">
                                  <Typography component="span" sx={{ fontWeight: 600, color: sopsColor(v) }}>
                                    {fmtScore(v)}
                                  </Typography>
                                </TableCell>
                              );
                            })}
                          </>
                        )}
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableContainer>
              <TablePagination
                component="div"
                count={total}
                page={page}
                onPageChange={handlePage}
                rowsPerPage={rowsPerPage}
                onRowsPerPageChange={handleRowsPerPage}
                rowsPerPageOptions={[10, 25, 50, 100]}
              />
            </CardContent>
          </Card>
        </Box>
      )}
    </Box>
  );
}
