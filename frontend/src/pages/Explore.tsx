// The exploration landscape — the one page that asks what we HAVEN'T tried.
//
// Every other view in PathBrain judges profiles that already exist: Settings Impact ranks
// them, the duel adjudicates them, the race promotes the under-sampled ones. None of them
// answers "what's untested that might beat all of this?", and with a hundred-odd profiles
// varying several levers at once that question isn't answerable by eye — the levers
// interact, the coverage is lumpy, and the interesting values are the ones nobody picked.
//
// So this page reads the measured field and shows four things, in the order you'd act on
// them: what to run next, what each lever does, where the holes are, and which levers have
// to be chosen together. It is entirely read-only — the only thing that writes anything is
// the "Test to minimum" button, which goes through the same supervised apply → benchmark →
// restore path as an AI suggestion.
import { useCallback, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip as RTooltip,
  XAxis,
  YAxis,
} from "recharts";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Divider from "@mui/material/Divider";
import LinearProgress from "@mui/material/LinearProgress";
import Link from "@mui/material/Link";
import Snackbar from "@mui/material/Snackbar";
import Stack from "@mui/material/Stack";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import ExploreIcon from "@mui/icons-material/Explore";
import ScienceIcon from "@mui/icons-material/Science";
import TrendingUpIcon from "@mui/icons-material/TrendingUp";
import { useTheme } from "@mui/material/styles";

import { api } from "../api/client";
import type { ExploreCandidate, ExploreCurve, ExploreLandscape } from "../api/types";
import { fmtNum } from "../utils/format";

const SHAPE_TIP: Record<string, string> = {
  "sweet spot":
    "Best somewhere in the middle — both extremes are worse. A single correlation reports this as 'no relationship', which is why the curve is drawn.",
  "higher is better": "Overall rises with this lever across the tested range.",
  "lower is better": "Overall falls as this lever rises across the tested range.",
  flat: "Moving this lever barely changes the Overall — spend the runs elsewhere.",
  unclear: "No consistent shape yet — more values would settle it.",
  "too few values": "Fewer than three distinct values measured on this lever.",
};

const SHAPE_COLOR: Record<string, "success" | "info" | "warning" | "default"> = {
  "sweet spot": "success",
  "higher is better": "info",
  "lower is better": "info",
  flat: "default",
  unclear: "warning",
  "too few values": "warning",
};

const fmtValue = (v: number, unit: string | null) =>
  `${Number.isInteger(v) ? v : v.toFixed(1)}${unit ?? ""}`;

// One lever's response curve. The chart is the point of this page: a lever can be flat on
// its correlation and still have an obvious peak in the middle, and no coefficient can say
// "best at 3000" — only the shape can.
function CurveCard({ curve }: { curve: ExploreCurve }) {
  const theme = useTheme();
  const data = curve.curve.map((p) => ({ ...p, label: fmtValue(p.value, curve.unit) }));
  return (
    <Card variant="outlined">
      <CardContent sx={{ pb: 1 }}>
        <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }} flexWrap="wrap" useFlexGap>
          <Typography variant="subtitle2" sx={{ flexGrow: 1 }}>
            {curve.pipe} · {curve.field_label}
          </Typography>
          <Tooltip title={SHAPE_TIP[curve.shape] ?? curve.shape}>
            <Chip size="small" label={curve.shape} color={SHAPE_COLOR[curve.shape] ?? "default"} />
          </Tooltip>
        </Stack>
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 0.5 }}>
          Best at <b>{fmtValue(curve.best_value, curve.unit)}</b> ({fmtNum(curve.best_overall, 1)})
          {curve.best_at_edge ? " — the end of the tested range, so the optimum may lie beyond it" : ""}
          {curve.spearman != null ? ` · ρ ${curve.spearman.toFixed(2)}` : ""}
        </Typography>
        <Box sx={{ height: 150 }}>
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={data} margin={{ top: 4, right: 8, left: -20, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke={theme.palette.divider} />
              <XAxis dataKey="label" tick={{ fontSize: 10 }} stroke={theme.palette.text.secondary} />
              <YAxis tick={{ fontSize: 10 }} domain={["dataMin - 1", "dataMax + 1"]} stroke={theme.palette.text.secondary} />
              <RTooltip
                contentStyle={{
                  background: theme.palette.background.paper,
                  border: `1px solid ${theme.palette.divider}`,
                  fontSize: 12,
                }}
                formatter={(v: number, name) => [fmtNum(v, 2), name === "overall" ? "median Overall" : name]}
              />
              <ReferenceLine
                x={fmtValue(curve.best_value, curve.unit)}
                stroke={theme.palette.success.main}
                strokeDasharray="4 2"
              />
              <Line
                type="monotone"
                dataKey="overall"
                stroke={theme.palette.primary.main}
                strokeWidth={2}
                dot={{ r: 3 }}
              />
            </LineChart>
          </ResponsiveContainer>
        </Box>
      </CardContent>
    </Card>
  );
}

// One proposed profile. Every candidate has to say what it changes and WHY that value is
// interesting — a suggestion nobody can explain is worth nothing, however good its score.
function CandidateCard({
  candidate,
  rank,
  bestOverall,
  onTest,
  testing,
}: {
  candidate: ExploreCandidate;
  rank: number;
  bestOverall: number | null;
  onTest: (c: ExploreCandidate) => void;
  testing: boolean;
}) {
  const navigate = useNavigate();
  const promising = candidate.beats_best_by > 0;
  return (
    <Card variant="outlined" sx={{ borderColor: promising ? "success.main" : "divider" }}>
      <CardContent>
        <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }} flexWrap="wrap" useFlexGap>
          <Chip size="small" label={`#${rank}`} />
          {promising ? (
            <Tooltip title="Its upside clears the best Overall measured so far — this is somewhere that could actually improve on what you have.">
              <Chip
                size="small"
                color="success"
                icon={<TrendingUpIcon />}
                label={`could beat best by ${fmtNum(candidate.beats_best_by, 1)}`}
              />
            </Tooltip>
          ) : (
            <Tooltip title="Worth measuring to close a hole in coverage, but on current evidence it's unlikely to beat what you already have.">
              <Chip size="small" variant="outlined" label="fills a hole" />
            </Tooltip>
          )}
          <Box sx={{ flexGrow: 1 }} />
          <Button
            size="small"
            variant="contained"
            startIcon={testing ? <CircularProgress size={14} /> : <ScienceIcon />}
            disabled={testing}
            onClick={() => onTest(candidate)}
          >
            Test to minimum
          </Button>
        </Stack>

        {candidate.changes.map((ch) => (
          <Typography key={ch.key} variant="body2" sx={{ mb: 0.25 }}>
            <b>
              {ch.pipe} {ch.field_label}
            </b>{" "}
            {fmtValue(ch.from, ch.unit)} → <b>{fmtValue(ch.to, ch.unit)}</b>
            <Typography component="span" variant="caption" color="text.secondary">
              {" "}
              — {ch.why}
            </Typography>
          </Typography>
        ))}

        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>
          Starting from{" "}
          <Link
            component="button"
            underline="hover"
            onClick={() => navigate(`/profiles/${encodeURIComponent(candidate.parent.fingerprint)}`)}
            sx={{ font: "inherit" }}
          >
            {candidate.parent.name || candidate.parent.label}
          </Link>{" "}
          (Overall {fmtNum(candidate.parent.overall, 1)})
        </Typography>

        <Stack direction="row" spacing={2} sx={{ mt: 1 }} flexWrap="wrap" useFlexGap>
          <Tooltip title="The parent's measured Overall, adjusted by what the response curve says about moving this lever. Beyond the tested range the curve is clamped rather than extrapolated — we don't guess out there, we admit we don't know and let the uncertainty carry it.">
            <Box>
              <Typography variant="caption" color="text.secondary">
                Predicted
              </Typography>
              <Typography variant="h6" sx={{ lineHeight: 1.2 }}>
                {fmtNum(candidate.predicted, 1)}
                <Typography component="span" variant="caption" color="text.secondary">
                  {" "}
                  ± {fmtNum(candidate.uncertainty, 1)}
                </Typography>
              </Typography>
            </Box>
          </Tooltip>
          <Tooltip title="Predicted plus the uncertainty — how good this could turn out to be. Candidates are ranked on this, not on the prediction: the question is where you might beat everything you have, not where you expect to do averagely well.">
            <Box>
              <Typography variant="caption" color="text.secondary">
                Upside
              </Typography>
              <Typography variant="h6" sx={{ lineHeight: 1.2, color: promising ? "success.main" : "text.primary" }}>
                {fmtNum(candidate.upside, 1)}
              </Typography>
            </Box>
          </Tooltip>
          {bestOverall != null && (
            <Box>
              <Typography variant="caption" color="text.secondary">
                Best measured
              </Typography>
              <Typography variant="h6" sx={{ lineHeight: 1.2 }}>
                {fmtNum(bestOverall, 1)}
              </Typography>
            </Box>
          )}
        </Stack>
      </CardContent>
    </Card>
  );
}

export default function Explore() {
  const [data, setData] = useState<ExploreLandscape | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [testing, setTesting] = useState<string | null>(null);
  const [snack, setSnack] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await api.exploreLandscape(3));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not map the landscape.");
    } finally {
      setLoading(false);
    }
  }, []);

  const test = useCallback(async (c: ExploreCandidate) => {
    const id = c.changes.map((ch) => ch.key).join("|");
    setTesting(id);
    try {
      const label = c.changes
        .map((ch) => `${ch.pipe} ${ch.field_label} ${fmtValue(ch.to, ch.unit)}`)
        .join(", ");
      await api.testSettings({ settings: c.settings, label: `Explore: ${label}` });
      setSnack("Testing — apply, benchmark to the confidence minimum, then restore. Watch the jobs menu.");
    } catch (e) {
      setSnack(e instanceof Error ? e.message : "Could not start the test.");
    } finally {
      setTesting(null);
    }
  }, []);

  return (
    <Box>
      <Stack direction="row" spacing={1.5} alignItems="center" sx={{ mb: 1 }}>
        <ExploreIcon color="primary" sx={{ fontSize: 32 }} />
        <Box sx={{ flexGrow: 1 }}>
          <Typography variant="h4">Explore</Typography>
          <Typography variant="caption" color="text.secondary">
            What the shaper's parameter space looks like, where the holes are, and which
            untested profiles are most likely to beat everything you've measured.
          </Typography>
        </Box>
        <Button variant="contained" onClick={() => void load()} disabled={loading}>
          {loading ? "Mapping…" : data ? "Refresh" : "Map the space"}
        </Button>
      </Stack>

      {loading && !data && (
        <Box sx={{ mb: 2 }}>
          <LinearProgress />
          <Typography variant="caption" color="text.secondary">
            Reading every profile and its measured Overall…
          </Typography>
        </Box>
      )}
      {error && (
        <Alert severity="error" sx={{ mb: 2 }}>
          {error}
        </Alert>
      )}
      {!data && !loading && !error && (
        <Alert severity="info">
          This reads every scored profile to map the parameter space, so it runs on demand
          rather than on page load. Press <b>Map the space</b>.
        </Alert>
      )}

      {data?.reason && <Alert severity="info">{data.reason}</Alert>}

      {data && !data.reason && (
        <>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 2 }}>
            Modelled from <b>{data.profiles_modelled}</b> profile
            {data.profiles_modelled === 1 ? "" : "s"}
            {data.confident_only ? " that have reached the iteration minimum" : " (including thin ones — nothing confident to model yet)"} ·
            best Overall measured <b>{fmtNum(data.best_overall, 1)}</b> · {data.axes.length} levers
          </Typography>

          {/* ── What to run next ───────────────────────────────────────────────── */}
          <Card sx={{ mb: 2 }}>
            <CardContent>
              <Typography variant="h6">Next profiles to test</Typography>
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.5 }}>
                Each one is an existing profile with a lever moved to a value nobody has
                tried, chosen for a reason it can state. They're ranked by <b>upside</b> —
                predicted score plus what we don't know — because the question is where you
                might <i>beat</i> everything measured, not where you'd score respectably.
              </Typography>
              {data.candidates.length === 0 ? (
                <Alert severity="info">
                  No untested combination stands out yet — the levers are either well
                  covered or too flat to distinguish. Collect more runs, or widen a sweep.
                </Alert>
              ) : (
                <Stack spacing={1.5}>
                  {data.candidates.map((c, i) => (
                    <CandidateCard
                      key={c.changes.map((ch) => `${ch.key}:${ch.to}`).join("|")}
                      candidate={c}
                      rank={i + 1}
                      bestOverall={data.best_overall}
                      onTest={(x) => void test(x)}
                      testing={testing === c.changes.map((ch) => ch.key).join("|")}
                    />
                  ))}
                </Stack>
              )}
            </CardContent>
          </Card>

          {/* ── What each lever does ───────────────────────────────────────────── */}
          <Card sx={{ mb: 2 }}>
            <CardContent>
              <Typography variant="h6">What each lever does</Typography>
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.5 }}>
                Median Overall at each value actually measured, per lever, with the Download
                and Upload legs kept separate — they're different knobs and they don't
                behave the same. The dashed line marks the best value tested.
              </Typography>
              <Box
                sx={{
                  display: "grid",
                  gap: 1.5,
                  gridTemplateColumns: { xs: "1fr", md: "1fr 1fr", xl: "repeat(3, 1fr)" },
                }}
              >
                {data.curves.map((c) => (
                  <CurveCard key={c.key} curve={c} />
                ))}
              </Box>
            </CardContent>
          </Card>

          {/* ── Holes in coverage ──────────────────────────────────────────────── */}
          <Card sx={{ mb: 2 }}>
            <CardContent>
              <Typography variant="h6">Holes in coverage</Typography>
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1 }}>
                A <b>gap</b> is a blank between two values that were tested — the answer is
                bracketed, so one run settles it. An <b>edge</b> is the best value being the
                end of the range, where the optimum isn't bracketed at all and the only way
                to find out is to go further.
              </Typography>
              {data.gaps.length === 0 ? (
                <Typography variant="body2" color="text.secondary">
                  No meaningful holes — every lever is sampled evenly across its range.
                </Typography>
              ) : (
                <Stack divider={<Divider flexItem />} spacing={0.75}>
                  {data.gaps.map((g, i) => (
                    <Box key={`${g.key}-${g.kind}-${i}`} sx={{ pt: i ? 0.75 : 0 }}>
                      <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
                        <Chip
                          size="small"
                          label={g.kind}
                          color={g.kind === "edge" ? "warning" : "default"}
                          variant={g.kind === "edge" ? "filled" : "outlined"}
                          sx={{ height: 20 }}
                        />
                        <Typography variant="body2">
                          {g.pipe} {g.field_label}
                        </Typography>
                        <Typography variant="caption" color="text.secondary">
                          try {fmtValue(g.suggest, g.unit)}
                        </Typography>
                      </Stack>
                      <Typography variant="caption" color="text.secondary">
                        {g.detail}
                      </Typography>
                    </Box>
                  ))}
                </Stack>
              )}
            </CardContent>
          </Card>

          {/* ── Levers that have to be chosen together ─────────────────────────── */}
          {data.interactions.length > 0 && (
            <Card sx={{ mb: 2 }}>
              <CardContent>
                <Typography variant="h6">Levers that interact</Typography>
                <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.5 }}>
                  Split each lever at its median and compare the four corners. A large
                  contrast means the better half of one lever <i>depends</i> on which half of
                  the other you're in — the question no per-lever view can answer, and the
                  reason to look at the two pipes side by side. Pairs whose contrast is
                  small next to the spread between the corners aren't listed: they act
                  independently, which is the ordinary case and needs no panel.
                </Typography>
                <Stack spacing={1.5}>
                  {data.interactions.slice(0, 4).map((it) => (
                    <Box key={`${it.a}|${it.b}`}>
                      <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
                        <Typography variant="subtitle2">
                          {it.a_label} × {it.b_label}
                        </Typography>
                        <Chip
                          size="small"
                          label={`contrast ${fmtNum(it.contrast, 1)}`}
                          color={Math.abs(it.contrast) >= 2 ? "warning" : "default"}
                          variant="outlined"
                          sx={{ height: 20 }}
                        />
                      </Stack>
                      <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 0.5 }}>
                        {it.summary}
                      </Typography>
                      <Box
                        sx={{
                          display: "grid",
                          gridTemplateColumns: "auto 1fr 1fr",
                          gap: 0.5,
                          maxWidth: 460,
                          fontSize: 12,
                        }}
                      >
                        <Box />
                        <Typography variant="caption" align="center" color="text.secondary">
                          {it.b_label} low
                        </Typography>
                        <Typography variant="caption" align="center" color="text.secondary">
                          {it.b_label} high
                        </Typography>
                        {[true, false].map((aHigh) => (
                          <Box key={String(aHigh)} sx={{ display: "contents" }}>
                            <Typography variant="caption" color="text.secondary" sx={{ pr: 1 }}>
                              {it.a_label} {aHigh ? "high" : "low"}
                            </Typography>
                            {[false, true].map((bHigh) => {
                              const cell = it.cells.find((c) => c.a_high === aHigh && c.b_high === bHigh);
                              const best = Math.max(...it.cells.map((c) => c.overall));
                              const isBest = cell && cell.overall === best;
                              return (
                                <Tooltip
                                  key={String(bHigh)}
                                  title={`${cell?.profiles ?? 0} profile(s) in this corner`}
                                >
                                  <Box
                                    sx={{
                                      textAlign: "center",
                                      py: 0.5,
                                      borderRadius: 1,
                                      border: 1,
                                      borderColor: isBest ? "success.main" : "divider",
                                      bgcolor: isBest ? "action.selected" : "transparent",
                                    }}
                                  >
                                    {fmtNum(cell?.overall, 1)}
                                  </Box>
                                </Tooltip>
                              );
                            })}
                          </Box>
                        ))}
                      </Box>
                    </Box>
                  ))}
                </Stack>
              </CardContent>
            </Card>
          )}
        </>
      )}

      <Snackbar
        open={!!snack}
        autoHideDuration={6000}
        onClose={() => setSnack(null)}
        message={snack ?? ""}
      />
    </Box>
  );
}
