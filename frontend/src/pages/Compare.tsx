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
import type { RunScore, RunSummary } from "../api/types";
import Loading from "../components/Loading";
import PageHeader from "../components/PageHeader";
import EmptyState from "../components/EmptyState";
import { fmtDateTime, fmtNum } from "../utils/format";
import { useMethodology } from "../utils/methodology";

type Verdict = "Improved" | "Regressed" | "Neutral";

interface Row {
  name: string;
  group: string; // "Score", an axis label, or "Measured"
  unit?: string;
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
  const methodology = useMethodology();
  // Each run's Scores under every methodology it has one for; the rows use the current one.
  const [aScores, setAScores] = useState<RunScore[] | null>(null);
  const [bScores, setBScores] = useState<RunScore[] | null>(null);
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
      setAScores(null);
      return;
    }
    api.runScores(aId).then((r) => setAScores(r.scores)).catch((e) => setError(e.message));
  }, [aId]);

  useEffect(() => {
    if (bId === "") {
      setBScores(null);
      return;
    }
    api.runScores(bId).then((r) => setBScores(r.scores)).catch((e) => setError(e.message));
  }, [bId]);

  const version = methodology?.version;
  const aScore = aScores?.find((x) => x.methodology_version === version) ?? null;
  const bScore = bScores?.find((x) => x.methodology_version === version) ?? null;

  // Rows follow the current methodology: the Overall, each axis that scores something with
  // its metrics' 0-100 scores, then the raw measurements with their units and direction.
  const rows = useMemo<Row[]>(() => {
    if (!methodology || !aScore || !bScore) return [];
    const out: Row[] = [];
    const push = (name: string, group: string, a: number | null, b: number | null, hib: boolean, unit?: string) =>
      out.push({ name, group, a, b, unit, higherIsBetter: hib, verdict: verdictFor(a, b, hib) });

    push("Overall", "Score", aScore.axis_scores.overall ?? null, bScore.axis_scores.overall ?? null, true);
    const scored = methodology.definition.metrics.filter((m) => m.weight > 0);
    for (const axis of methodology.definition.axes) {
      const inAxis = scored.filter((m) => m.axis === axis.key);
      if (inAxis.length === 0) continue;
      push(axis.label, "Score", aScore.axis_scores[axis.key] ?? null, bScore.axis_scores[axis.key] ?? null, true);
      for (const m of inAxis) {
        push(m.label, axis.label, aScore.subscores[m.key] ?? null, bScore.subscores[m.key] ?? null, true);
      }
    }
    for (const m of scored) {
      push(
        m.label,
        "Measured",
        aScore.metric_values[m.key] ?? null,
        bScore.metric_values[m.key] ?? null,
        m.higher_is_better,
        m.unit
      );
    }
    return out;
  }, [methodology, aScore, bScore]);
  const unscored = (aScores != null && !aScore) || (bScores != null && !bScore);

  if (loading) return <Loading label="Loading runs…" />;

  const runLabel = (r: RunSummary) =>
    `#${r.id}${r.label ? ` · ${r.label}` : ""} — ${fmtDateTime(r.created_at)}`;

  return (
    <Box>
      <PageHeader
        title="Compare runs"
        lead="Two runs side by side, metric by metric. The last 50 runs are offered; to compare profiles rather than runs, use Profile standings."
        compact
      />

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

          {unscored && (
            <Alert severity="warning" sx={{ mb: 2 }}>
              One of these runs has no score under {version ?? "the current methodology"}, so
              there's nothing like-for-like to compare. Pick two current runs, or re-grade history
              from the Methodology page.
            </Alert>
          )}

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
                        <TableCell>Group</TableCell>
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
                            </Typography>
                          </TableCell>
                          <TableCell align="right">
                            {fmtNum(row.a)}
                            {row.unit && row.a != null ? ` ${row.unit}` : ""}
                          </TableCell>
                          <TableCell align="right">
                            {fmtNum(row.b)}
                            {row.unit && row.b != null ? ` ${row.unit}` : ""}
                          </TableCell>
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
