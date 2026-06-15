import { useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
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
  const [series, setSeries] = useState<SeriesPoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      setLoading(true);
      setError(null);
      try {
        const [h, s] = await Promise.all([api.history(50), api.historySeries(100)]);
        setRuns(h);
        setSeries(s.points);
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to load history");
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  if (loading) return <Loading label="Loading history…" />;

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

      {runs.length === 0 ? (
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
          <Card>
            <CardContent>
              <Typography variant="h6" gutterBottom>
                Runs
              </Typography>
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
                    {runs.map((r) => (
                      <TableRow
                        key={r.id}
                        hover
                        sx={{ cursor: "pointer" }}
                        onClick={() => navigate(`/runs/${r.id}`)}
                      >
                        <TableCell>#{r.id}</TableCell>
                        <TableCell>{fmtDateTime(r.created_at)}</TableCell>
                        <TableCell>{r.label ?? "—"}</TableCell>
                        <TableCell>
                          <StatusChip status={r.status} />
                        </TableCell>
                        <TableCell align="right" sx={{ fontWeight: 600, color: sopsColor(r.sops) }}>
                          {fmtScore(r.sops)}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableContainer>
            </CardContent>
          </Card>

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
        </Box>
      )}
    </Box>
  );
}
