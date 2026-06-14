import { useEffect, useMemo, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import FormControl from "@mui/material/FormControl";
import InputLabel from "@mui/material/InputLabel";
import MenuItem from "@mui/material/MenuItem";
import Select from "@mui/material/Select";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";

import { api } from "../api/client";
import type { RunDetail, RunSummary } from "../api/types";
import Loading from "../components/Loading";
import EmptyState from "../components/EmptyState";
import { fmtDateTime, fmtNum } from "../utils/format";

type Verdict = "Improved" | "Regressed" | "Neutral";

interface Row {
  name: string;
  group: "Score" | "Subscore" | "Metric";
  a: number | null;
  b: number | null;
  higherIsBetter: boolean;
  verdict: Verdict;
}

function verdictFor(a: number | null, b: number | null, higherIsBetter: boolean): Verdict {
  if (a == null || b == null) return "Neutral";
  const diff = b - a;
  if (Math.abs(diff) < 1e-9) return "Neutral";
  const better = higherIsBetter ? diff > 0 : diff < 0;
  return better ? "Improved" : "Regressed";
}

const verdictColor: Record<Verdict, "success" | "error" | "default"> = {
  Improved: "success",
  Regressed: "error",
  Neutral: "default",
};

export default function Compare() {
  const [runs, setRuns] = useState<RunSummary[]>([]);
  const [aId, setAId] = useState<number | "">("");
  const [bId, setBId] = useState<number | "">("");
  const [aRun, setARun] = useState<RunDetail | null>(null);
  const [bRun, setBRun] = useState<RunDetail | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    (async () => {
      try {
        const h = await api.history(50);
        setRuns(h);
        if (h.length >= 2) {
          setBId(h[0].id);
          setAId(h[1].id);
        } else if (h.length === 1) {
          setBId(h[0].id);
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to load runs");
      } finally {
        setLoading(false);
      }
    })();
  }, []);

  useEffect(() => {
    if (aId === "") {
      setARun(null);
      return;
    }
    api.result(aId).then(setARun).catch((e) => setError(e.message));
  }, [aId]);

  useEffect(() => {
    if (bId === "") {
      setBRun(null);
      return;
    }
    api.result(bId).then(setBRun).catch((e) => setError(e.message));
  }, [bId]);

  const rows = useMemo<Row[]>(() => {
    if (!aRun?.score || !bRun?.score) return [];
    const out: Row[] = [];
    out.push({
      name: "SOPS",
      group: "Score",
      a: aRun.score.sops,
      b: bRun.score.sops,
      higherIsBetter: true,
      verdict: verdictFor(aRun.score.sops, bRun.score.sops, true),
    });

    const subKeys = Array.from(
      new Set([...Object.keys(aRun.score.subscores), ...Object.keys(bRun.score.subscores)])
    ).sort();
    for (const k of subKeys) {
      const a = aRun.score.subscores[k] ?? null;
      const b = bRun.score.subscores[k] ?? null;
      out.push({ name: k, group: "Subscore", a, b, higherIsBetter: true, verdict: verdictFor(a, b, true) });
    }

    const metKeys = Array.from(
      new Set([
        ...Object.keys(aRun.score.metric_values),
        ...Object.keys(bRun.score.metric_values),
      ])
    ).sort();
    for (const k of metKeys) {
      const a = aRun.score.metric_values[k] ?? null;
      const b = bRun.score.metric_values[k] ?? null;
      out.push({ name: k, group: "Metric", a, b, higherIsBetter: false, verdict: verdictFor(a, b, false) });
    }
    return out;
  }, [aRun, bRun]);

  if (loading) return <Loading label="Loading runs…" />;

  const runLabel = (r: RunSummary) =>
    `#${r.id}${r.label ? ` · ${r.label}` : ""} — ${fmtDateTime(r.created_at)}`;

  return (
    <Box>
      <Typography variant="h4" sx={{ mb: 3 }}>
        Compare Runs
      </Typography>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {runs.length < 2 ? (
        <Card>
          <CardContent>
            <EmptyState
              title="Need at least two runs"
              description="Run more benchmarks before you can compare them."
            />
          </CardContent>
        </Card>
      ) : (
        <>
          <Box
            sx={{
              display: "grid",
              gap: 2,
              gridTemplateColumns: { xs: "1fr", sm: "1fr 1fr" },
              mb: 3,
            }}
          >
            <FormControl fullWidth>
              <InputLabel id="run-a">Run A (baseline)</InputLabel>
              <Select
                labelId="run-a"
                label="Run A (baseline)"
                value={aId}
                onChange={(e) => setAId(e.target.value === "" ? "" : Number(e.target.value))}
              >
                {runs.map((r) => (
                  <MenuItem key={r.id} value={r.id}>
                    {runLabel(r)}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
            <FormControl fullWidth>
              <InputLabel id="run-b">Run B (compared)</InputLabel>
              <Select
                labelId="run-b"
                label="Run B (compared)"
                value={bId}
                onChange={(e) => setBId(e.target.value === "" ? "" : Number(e.target.value))}
              >
                {runs.map((r) => (
                  <MenuItem key={r.id} value={r.id}>
                    {runLabel(r)}
                  </MenuItem>
                ))}
              </Select>
            </FormControl>
          </Box>

          <Card>
            <CardContent>
              {rows.length === 0 ? (
                <Typography variant="body2" color="text.secondary">
                  Select two runs that both have scores to compare.
                </Typography>
              ) : (
                <TableContainer>
                  <Table size="small">
                    <TableHead>
                      <TableRow>
                        <TableCell>Metric</TableCell>
                        <TableCell>Type</TableCell>
                        <TableCell align="right">Run A</TableCell>
                        <TableCell align="right">Run B</TableCell>
                        <TableCell align="center">Change</TableCell>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {rows.map((row) => (
                        <TableRow key={`${row.group}-${row.name}`} hover>
                          <TableCell sx={{ fontWeight: row.group === "Score" ? 700 : 400 }}>
                            {row.name}
                          </TableCell>
                          <TableCell>
                            <Typography variant="caption" color="text.secondary">
                              {row.group}
                              {row.group === "Metric" ? " (lower better)" : " (higher better)"}
                            </Typography>
                          </TableCell>
                          <TableCell align="right">{fmtNum(row.a)}</TableCell>
                          <TableCell align="right">{fmtNum(row.b)}</TableCell>
                          <TableCell align="center">
                            <Chip
                              size="small"
                              color={verdictColor[row.verdict]}
                              variant={row.verdict === "Neutral" ? "outlined" : "filled"}
                              label={row.verdict}
                            />
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </TableContainer>
              )}
            </CardContent>
          </Card>
        </>
      )}
    </Box>
  );
}
