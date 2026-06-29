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
import EmptyState from "../components/EmptyState";
import { fmtDateTime, fmtScore, runRemainingMs } from "../utils/format";
import { useNow } from "../utils/useNow";
import { sopsColor } from "../theme";

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
  const hiddenCount = runs.length - shownRuns.length;

  return (
    <Box>
      <Typography variant="h4" sx={{ mb: 3 }}>
        History
      </Typography>

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
                  Overall, Responsiveness, Smoothness &amp; Speed
                </Typography>
                <SeriesChart
                  data={series}
                  yDomain={[0, 100]}
                  lines={[
                    { key: "overall", name: "Overall", color: "#eceff1" },
                    { key: "responsiveness", name: "Responsiveness", color: "#ffa726" },
                    { key: "smoothness", name: "Smoothness", color: "#ab47bc" },
                    { key: "speed", name: "Speed", color: "#4dd0e1" },
                  ]}
                />
              </CardContent>
            </Card>

            <Card>
              <CardContent>
                <Typography variant="h6" gutterBottom>
                  Latency (ms)
                </Typography>
                <SeriesChart
                  data={series}
                  unit="ms"
                  lines={[
                    { key: "dns_ms", name: "DNS", color: "#7c4dff" },
                    { key: "tcp_ms", name: "TCP", color: "#4dd0e1" },
                    { key: "tls_ms", name: "TLS", color: "#ffb74d" },
                    { key: "ttfb_ms", name: "TTFB", color: "#66bb6a" },
                  ]}
                />
              </CardContent>
            </Card>

            <Card sx={{ gridColumn: { lg: "1 / -1" } }}>
              <CardContent>
                <Typography variant="h6" gutterBottom>
                  Jitter (ms)
                </Typography>
                <SeriesChart
                  data={series}
                  unit="ms"
                  height={220}
                  lines={[{ key: "jitter_ms", name: "Jitter", color: "#ef5350" }]}
                />
              </CardContent>
            </Card>
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
                  label={hideLegacy ? "Show legacy (archive)" : "Hide legacy"}
                />
              </Stack>
              {hideLegacy && hiddenCount > 0 && (
                <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1 }}>
                  {hiddenCount} legacy run{hiddenCount === 1 ? "" : "s"} on this page hidden — scored
                  before the current rubric, not comparable.
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
                      <TableCell align="right">Respons.</TableCell>
                      <TableCell align="right">Smoothness</TableCell>
                      <TableCell align="right">Speed</TableCell>
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
                          <TableCell align="right" colSpan={4}>
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
                            <TableCell align="right">
                              <Typography component="span" sx={{ fontWeight: 600, color: sopsColor(r.responsiveness) }}>
                                {fmtScore(r.responsiveness)}
                              </Typography>
                            </TableCell>
                            <TableCell align="right">
                              <Typography component="span" sx={{ fontWeight: 600, color: sopsColor(r.smoothness) }}>
                                {fmtScore(r.smoothness)}
                              </Typography>
                            </TableCell>
                            <TableCell align="right">
                              <Typography component="span" sx={{ fontWeight: 600, color: sopsColor(r.speed) }}>
                                {fmtScore(r.speed)}
                              </Typography>
                            </TableCell>
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
