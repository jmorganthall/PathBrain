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
import type { RunSummary, SeriesPoint } from "../api/types";
import StatusChip from "../components/StatusChip";
import SeriesChart from "../components/SeriesChart";
import Loading from "../components/Loading";
import EmptyState from "../components/EmptyState";
import { fmtDateTime, fmtScore } from "../utils/format";
import { sopsColor } from "../theme";

export default function History() {
  const navigate = useNavigate();
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [total, setTotal] = useState(0);
  const [series, setSeries] = useState<SeriesPoint[]>([]);
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
                  SOPS
                </Typography>
                <SeriesChart
                  data={series}
                  yDomain={[0, 100]}
                  lines={[{ key: "sops", name: "SOPS", color: "#4dd0e1" }]}
                  band={{ lowKey: "sops_min", highKey: "sops_max", color: "#4dd0e1", name: "± range" }}
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
                      <TableCell align="right">SOPS</TableCell>
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
                          <StatusChip status={r.status} />
                        </TableCell>
                        <TableCell align="right">
                          {r.legacy ? (
                            <Tooltip title="Scored before the current rubric — not comparable.">
                              <Chip size="small" variant="outlined" label="legacy" />
                            </Tooltip>
                          ) : (
                            <Typography
                              component="span"
                              sx={{ fontWeight: 600, color: sopsColor(r.sops) }}
                            >
                              {fmtScore(r.sops)}
                            </Typography>
                          )}
                        </TableCell>
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
