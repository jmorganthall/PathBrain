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
import { useCallback, useMemo, useState } from "react";
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
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TablePagination from "@mui/material/TablePagination";
import TableRow from "@mui/material/TableRow";
import TableSortLabel from "@mui/material/TableSortLabel";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import ExploreIcon from "@mui/icons-material/Explore";
import ScienceIcon from "@mui/icons-material/Science";
import TrendingUpIcon from "@mui/icons-material/TrendingUp";
import WarningAmberIcon from "@mui/icons-material/WarningAmber";
import CompareArrowsIcon from "@mui/icons-material/CompareArrows";
import TerrainIcon from "@mui/icons-material/Terrain";
import { useTheme } from "@mui/material/styles";

import { api } from "../api/client";
import type {
  ExploreBasin,
  ExploreCandidate,
  ExploreConditionedCurve,
  ExploreCurve,
  ExploreLandscape,
  ExploreMatchedPairs,
} from "../api/types";
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
// ── Long tables: sorted so the highest-value rows land first, and paged so a page of
//    them is all anyone has to read ───────────────────────────────────────────────────
//
// These sections grow with the field, not with a fixed schema: a lever with twenty tested
// values has 190 possible one-lever transitions, and a hundred-and-fifty-profile field can
// hold dozens of local optima. Printed whole they're a wall nobody reads, which is the
// same as not reporting them. Each one below therefore leads with the rows that carry the
// most evidence, pages the rest, and stays sortable for anyone who wants a different cut.
const ROWS_PER_PAGE = 10;
// Lever charts shown before the "show all" toggle — enough to cover the levers that move
// the Overall, few enough that the section stays scannable.
const CURVES_SHOWN = 6;

type Dir = "asc" | "desc";

function usePagedSort<T>(rows: T[], initial: string, initialDir: Dir = "desc") {
  const [orderBy, setOrderBy] = useState(initial);
  const [dir, setDir] = useState<Dir>(initialDir);
  const [page, setPage] = useState(0);
  const onSort = useCallback(
    (key: string) => {
      setDir((d) => (orderBy === key ? (d === "asc" ? "desc" : "asc") : "desc"));
      setOrderBy(key);
      setPage(0);
    },
    [orderBy],
  );
  // A row count that shrinks under the current page (a refresh, a narrower field) must not
  // strand the reader on an empty page.
  const maxPage = Math.max(0, Math.ceil(rows.length / ROWS_PER_PAGE) - 1);
  const safePage = Math.min(page, maxPage);
  return { orderBy, dir, page: safePage, setPage, onSort };
}

// Nulls last in both directions — an unmeasured row is never "top".
function cmp(a: number | string | null | undefined, b: number | string | null | undefined, dir: Dir) {
  if (a == null && b == null) return 0;
  if (a == null) return 1;
  if (b == null) return -1;
  const d = typeof a === "string" && typeof b === "string" ? a.localeCompare(b) : (a as number) - (b as number);
  return dir === "asc" ? d : -d;
}

function SortHead({
  id,
  label,
  align,
  orderBy,
  dir,
  onSort,
  tip,
}: {
  id: string;
  label: string;
  align?: "right";
  orderBy: string;
  dir: Dir;
  onSort: (k: string) => void;
  tip?: string;
}) {
  const control = (
    <TableSortLabel active={orderBy === id} direction={orderBy === id ? dir : "asc"} onClick={() => onSort(id)}>
      {label}
    </TableSortLabel>
  );
  return (
    <TableCell align={align} sortDirection={orderBy === id ? dir : false} sx={{ whiteSpace: "nowrap" }}>
      {tip ? (
        <Tooltip title={tip} arrow enterDelay={300}>
          <span>{control}</span>
        </Tooltip>
      ) : (
        control
      )}
    </TableCell>
  );
}

function Pager({
  count,
  page,
  onPage,
}: {
  count: number;
  page: number;
  onPage: (p: number) => void;
}) {
  if (count <= ROWS_PER_PAGE) return null;
  return (
    <TablePagination
      component="div"
      count={count}
      page={page}
      rowsPerPage={ROWS_PER_PAGE}
      rowsPerPageOptions={[ROWS_PER_PAGE]}
      onPageChange={(_, p) => onPage(p)}
      sx={{ ".MuiTablePagination-toolbar": { minHeight: 40, pl: 0 } }}
    />
  );
}

function CurveCard({
  curve,
  conditioned,
  referenceName,
}: {
  curve: ExploreCurve;
  conditioned?: ExploreConditionedCurve;
  referenceName?: string;
}) {
  const theme = useTheme();
  // Both series on one pair of axes, because the comparison IS the finding: where they
  // disagree, the marginal curve is describing a different neighbourhood than yours.
  const near = new Map((conditioned?.curve ?? []).map((p) => [p.value, p.overall]));
  const data = curve.curve.map((p) => ({
    ...p,
    label: fmtValue(p.value, curve.unit),
    conditioned: near.get(p.value) ?? null,
  }));
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
          {curve.confounded && (
            <Tooltip
              title={
                "Some points on this curve are measuring two levers at once — the profiles " +
                "sitting at those values also differ systematically in another lever. " +
                (curve.imbalance ?? []).map((r) => r.detail).join(" ")
              }
            >
              <Chip size="small" color="warning" variant="outlined" icon={<WarningAmberIcon />} label="confounded" />
            </Tooltip>
          )}
        </Stack>
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 0.5 }}>
          Best at <b>{fmtValue(curve.best_value, curve.unit)}</b> ({fmtNum(curve.best_overall, 1)})
          {curve.best_at_edge ? " — the end of the tested range, so the optimum may lie beyond it" : ""}
          {curve.spearman != null ? ` · ρ ${curve.spearman.toFixed(2)}` : ""}
        </Typography>
        {!!conditioned && (
          <Typography variant="caption" sx={{ display: "block", mb: 0.5, color: "success.main" }}>
            Dashed: only profiles otherwise like {referenceName ?? "the reference"} — the
            neighbourhood you'd actually be moving through.
          </Typography>
        )}
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
                name="all profiles"
                stroke={theme.palette.primary.main}
                strokeWidth={2}
                dot={{ r: 3 }}
              />
              {!!conditioned && (
                <Line
                  type="monotone"
                  dataKey="conditioned"
                  name={`near ${referenceName ?? "the reference"}`}
                  stroke={theme.palette.success.main}
                  strokeWidth={2}
                  strokeDasharray="5 3"
                  connectNulls
                  dot={{ r: 3 }}
                />
              )}
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

        <Stack direction="row" spacing={0.75} sx={{ mt: 0.5 }} flexWrap="wrap" useFlexGap>
          {candidate.evidence.includes("measured directly on a matched pair") && (
            <Tooltip title="This exact move has been made before with everything else held identical, so the predicted change is a controlled measurement rather than an estimate.">
              <Chip size="small" color="success" variant="outlined" label="backed by a matched pair" sx={{ height: 20 }} />
            </Tooltip>
          )}
          {candidate.evidence.some((e) => e.startsWith("estimated")) && (
            <Tooltip title="No controlled comparison exists for this move, so the change is estimated from the marginal curve — which averages over profiles that differ in other levers too. Treat it as a guess.">
              <Chip size="small" variant="outlined" label="estimated from the curve" sx={{ height: 20 }} />
            </Tooltip>
          )}
          {candidate.multi_lever && (
            <Tooltip title="Moves two levers at once. The prediction adds their separate effects, and the local-optima analysis below is the demonstration that they may not add — so the band is deliberately wider.">
              <Chip size="small" color="warning" variant="outlined" icon={<WarningAmberIcon />} label="two levers — effects assumed to add" sx={{ height: 20 }} />
            </Tooltip>
          )}
        </Stack>
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


// The strongest evidence the record can give: profiles differing in exactly ONE lever.
// Everything else is identical by construction, so the difference in Overall is that
// lever's effect with no confounding — a controlled experiment you already ran without
// meaning to. Where this disagrees with the marginal curve above, believe this.
function MatchedPairsCard({ rows }: { rows: ExploreMatchedPairs[] }) {
  // Flattened across levers, because the question is "what do we actually know?", not
  // "what do we know about quantum?" — and grouped by lever the strongest finding in the
  // field can sit halfway down the fourth group.
  const all = useMemo(
    () =>
      rows.flatMap((r) =>
        r.transitions.map((t) => ({
          ...t,
          key: `${r.key}:${t.from}-${t.to}`,
          lever: `${r.pipe} ${r.field_label}`,
          unit: r.unit,
          // Evidence, not effect size. Several pairs that all agree is a finding; one pair
          // with a dramatic number is an anecdote, and leading with the anecdote is how a
          // table this long sends you chasing an outlier.
          evidence: (t.consistent && t.pairs > 1 ? 1e6 : 0) + t.pairs * 1e3 + Math.abs(t.median_delta),
        })),
      ),
    [rows],
  );
  const { orderBy, dir, page, setPage, onSort } = usePagedSort(all, "evidence");
  const sorted = useMemo(() => {
    const pick = (r: (typeof all)[number]) =>
      orderBy === "lever"
        ? r.lever
        : orderBy === "delta"
          ? Math.abs(r.median_delta)
          : orderBy === "pairs"
            ? r.pairs
            : r.evidence;
    return [...all].sort((a, b) => cmp(pick(a), pick(b), dir));
  }, [all, orderBy, dir]);
  const shown = sorted.slice(page * ROWS_PER_PAGE, page * ROWS_PER_PAGE + ROWS_PER_PAGE);
  const solid = all.filter((r) => r.consistent && r.pairs > 1).length;

  return (
    <Card sx={{ mb: 2 }}>
      <CardContent>
        <Stack direction="row" spacing={1} alignItems="center">
          <CompareArrowsIcon color="primary" />
          <Typography variant="h6">What changing one lever actually did</Typography>
        </Stack>
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.5 }}>
          Every pair of profiles that differs in <b>exactly one lever</b> — everything else
          identical, so the gap between them is that lever's effect with nothing else mixed
          in. This is a controlled experiment already sitting in your record, and where it
          disagrees with a curve above, this is the one to believe. Sorted by{" "}
          <b>evidence</b> — moves confirmed by several agreeing pairs first, because one pair
          with a dramatic number is an anecdote.
        </Typography>
        {all.length === 0 ? (
          <Alert severity="info">
            No two profiles differ in exactly one lever, so the record contains no controlled
            comparison. Nothing can de-confound that except running one — measure a profile
            that changes a single setting from one you already have.
          </Alert>
        ) : (
          <>
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 0.5 }}>
              {all.length} measured move{all.length === 1 ? "" : "s"} across {rows.length} lever
              {rows.length === 1 ? "" : "s"}
              {solid > 0 ? ` · ${solid} confirmed by more than one agreeing pair` : ""}
            </Typography>
            <TableContainer>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <SortHead id="lever" label="Lever" orderBy={orderBy} dir={dir} onSort={onSort} />
                    <TableCell sx={{ whiteSpace: "nowrap" }}>Change</TableCell>
                    <SortHead
                      id="delta"
                      label="Δ Overall"
                      align="right"
                      orderBy={orderBy}
                      dir={dir}
                      onSort={onSort}
                      tip="Median change in Overall when this move was made with everything else held identical. Sorts by size of the effect, ignoring direction."
                    />
                    <SortHead
                      id="pairs"
                      label="Pairs"
                      align="right"
                      orderBy={orderBy}
                      dir={dir}
                      onSort={onSort}
                      tip="How many matched pairs made this exact move, and the range of outcomes across them."
                    />
                    <SortHead
                      id="evidence"
                      label="Evidence"
                      orderBy={orderBy}
                      dir={dir}
                      onSort={onSort}
                      tip="The default order: moves confirmed by several agreeing pairs first, then by pair count, then by effect size."
                    />
                  </TableRow>
                </TableHead>
                <TableBody>
                  {shown.map((t) => {
                    const better = t.median_delta > 0;
                    return (
                      <TableRow key={t.key} hover>
                        <TableCell sx={{ whiteSpace: "nowrap" }}>{t.lever}</TableCell>
                        <TableCell sx={{ fontFamily: "monospace", whiteSpace: "nowrap" }}>
                          {fmtValue(t.from, t.unit)} → {fmtValue(t.to, t.unit)}
                        </TableCell>
                        <TableCell
                          align="right"
                          sx={{ color: better ? "success.main" : "error.main", fontWeight: 600, whiteSpace: "nowrap" }}
                        >
                          {better ? "+" : ""}
                          {fmtNum(t.median_delta, 2)}
                        </TableCell>
                        <TableCell align="right" sx={{ whiteSpace: "nowrap" }}>
                          {t.pairs}
                          {t.pairs > 1 && (
                            <Typography component="span" variant="caption" color="text.secondary">
                              {" "}
                              ({fmtNum(t.worst, 1)}…{fmtNum(t.best, 1)})
                            </Typography>
                          )}
                        </TableCell>
                        <TableCell>
                          {t.consistent && t.pairs > 1 ? (
                            <Tooltip title="Every matched pair agreed on the direction — not an average over a mix of outcomes.">
                              <Chip size="small" color="success" variant="outlined" label="every pair agrees" sx={{ height: 18 }} />
                            </Tooltip>
                          ) : (
                            <Typography variant="caption" color="text.secondary">
                              {t.pairs === 1 ? "single pair" : "pairs disagree"}
                            </Typography>
                          )}
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </TableContainer>
            <Pager count={all.length} page={page} onPage={setPage} />
          </>
        )}
      </CardContent>
    </Card>
  );
}

function BasinsCard({ basins, maxOther }: { basins: ExploreBasin[]; maxOther?: number }) {
  const navigate = useNavigate();
  // A profile with one measured sibling is barely a peak — it beat the only thing it was
  // ever compared to. Counting those in the coupling alarm turns it into "39 of 40", which
  // is noise wearing a warning's clothes; the claim is only worth making about optima that
  // have actually been surrounded.
  const WELL_EVIDENCED = 2;
  const separated = basins.filter(
    (b) => (b.levers_from_better ?? 0) >= 2 && b.siblings >= WELL_EVIDENCED,
  ).length;
  const solid = basins.filter((b) => b.siblings >= WELL_EVIDENCED).length;
  const { orderBy, dir, page, setPage, onSort } = usePagedSort(basins, "overall");
  const sorted = useMemo(() => {
    const pick = (b: ExploreBasin) =>
      orderBy === "name"
        ? (b.name || b.label).toLowerCase()
        : orderBy === "siblings"
          ? b.siblings
          : orderBy === "levers"
            ? b.levers_from_better
            : b.overall;
    return [...basins].sort((a, b) => cmp(pick(a), pick(b), dir));
  }, [basins, orderBy, dir]);
  const shown = sorted.slice(page * ROWS_PER_PAGE, page * ROWS_PER_PAGE + ROWS_PER_PAGE);

  return (
    <Card sx={{ mb: 2 }}>
      <CardContent>
        <Stack direction="row" spacing={1} alignItems="center">
          <TerrainIcon color="primary" />
          <Typography variant="h6">Local optima</Typography>
        </Stack>
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.5 }}>
          Profiles that <b>no measured one-lever change improves on</b>. If the surface had a
          single optimum, the curves above would describe it and moving one setting at a time
          from anywhere would reach it. Several separated basins mean the levers are{" "}
          <b>coupled</b> — each optimum is a combination, tuning one setting at a time gets
          stuck in whichever basin you started in, and a curve that averages across basins
          describes none of them. Best first; sort by <b>levers from better</b> to see which
          ones a single change can't escape, or by <b>siblings beaten</b> to see which are
          actually demonstrated — a profile that beat the one variant it was ever compared
          to is a data point, not a peak.
        </Typography>
        {basins.length === 0 ? (
          <Typography variant="body2" color="text.secondary">
            No profile has a measured one-lever sibling yet, so nothing can be called a peak.
          </Typography>
        ) : (
          <>
            {separated > 0 ? (
              <Alert severity="warning" sx={{ mb: 1.5 }}>
                {separated} of the {solid} well-surrounded optima sit two or more levers from
                a better one — no single change gets you from those to anything better.
                Marginal curves cannot describe this surface, and one-lever-at-a-time search
                will not cross between them.
              </Alert>
            ) : (
              <Alert severity="info" sx={{ mb: 1.5 }}>
                {solid === 0
                  ? "No optimum here has been surrounded by more than one measured sibling yet, so none of them is demonstrated. Measure one-lever variants around the leaders and this becomes a real map."
                  : "No well-surrounded optimum is cut off from a better one — so far a one-lever-at-a-time search could still climb out of every basin that's actually been measured."}
              </Alert>
            )}
            <TableContainer>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <SortHead id="name" label="Profile" orderBy={orderBy} dir={dir} onSort={onSort} />
                    <SortHead
                      id="overall"
                      label="Overall"
                      align="right"
                      orderBy={orderBy}
                      dir={dir}
                      onSort={onSort}
                      tip="Its measured Overall. The default order — the best local optimum first."
                    />
                    <SortHead
                      id="siblings"
                      label="Siblings beaten"
                      align="right"
                      orderBy={orderBy}
                      dir={dir}
                      onSort={onSort}
                      tip="How many measured one-lever variants exist around it, all of which it beats. More siblings means more confidence that this really is a peak rather than an unexplored corner."
                    />
                    <SortHead
                      id="levers"
                      label="Levers from better"
                      align="right"
                      orderBy={orderBy}
                      dir={dir}
                      onSort={onSort}
                      tip="How many levers you'd have to change at once to reach a better profile. Two or more means no single change escapes this basin — the definition of coupled levers. Blank on the best profile, which has nothing better to reach."
                    />
                  </TableRow>
                </TableHead>
                <TableBody>
                  {shown.map((b) => (
                    <TableRow key={b.fingerprint} hover>
                      <TableCell>
                        <Link
                          component="button"
                          underline="hover"
                          onClick={() => navigate(`/profiles/${encodeURIComponent(b.fingerprint)}`)}
                          sx={{ font: "inherit", textAlign: "left", whiteSpace: "nowrap" }}
                          title={b.label}
                        >
                          {b.name || b.label}
                        </Link>
                        {b.name && (
                          <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
                            {b.label}
                          </Typography>
                        )}
                      </TableCell>
                      <TableCell align="right" sx={{ fontWeight: 600 }}>
                        {fmtNum(b.overall, 1)}
                      </TableCell>
                      <TableCell align="right">{b.siblings}</TableCell>
                      <TableCell align="right">
                        {b.levers_from_better == null ? (
                          <Chip size="small" color="success" label="best" sx={{ height: 20 }} />
                        ) : b.levers_from_better >= 2 ? (
                          <Tooltip title="No single lever change reaches anything better — this is a separate basin.">
                            <Chip
                              size="small"
                              color="warning"
                              variant="outlined"
                              label={b.levers_from_better}
                              sx={{ height: 20 }}
                            />
                          </Tooltip>
                        ) : (
                          b.levers_from_better
                        )}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableContainer>
            <Pager count={basins.length} page={page} onPage={setPage} />
            {!!maxOther && (
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
                The dashed curves above are conditioned the same way — profiles differing from
                the reference in the plotted lever plus at most {maxOther} other.
              </Typography>
            )}
          </>
        )}
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
  const [gapPage, setGapPage] = useState(0);
  const [allCurves, setAllCurves] = useState(false);

  // Lead with the levers whose measured values actually spread the Overall — a flat lever
  // is a chart of noise, and with two pipes x five fields there are more of those than
  // anyone will scroll past to reach the ones that matter.
  const shownCurves = useMemo(() => {
    const spread = (c: ExploreCurve) =>
      c.curve.length < 2 ? -1 : Math.max(...c.curve.map((p) => p.overall)) - Math.min(...c.curve.map((p) => p.overall));
    const ranked = [...(data?.curves ?? [])].sort((a, b) => spread(b) - spread(a));
    return allCurves ? ranked : ranked.slice(0, CURVES_SHOWN);
  }, [data, allCurves]);

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
                Solid: median Overall at each value measured, across <i>all</i> profiles —
                which marginalizes over whatever the other levers happened to be, and so
                answers "how do profiles with this value score?". Dashed: the same lever
                restricted to profiles otherwise like the reference — "what happens if I
                change <i>this</i> profile", which is usually the question you're asking.
                Where they disagree, the lever is confounded and the dashed line is the one
                about your neighbourhood. Download and Upload are kept separate throughout.
              </Typography>
              <Box
                sx={{
                  display: "grid",
                  gap: 1.5,
                  gridTemplateColumns: { xs: "1fr", md: "1fr 1fr", xl: "repeat(3, 1fr)" },
                }}
              >
                {shownCurves.map((c) => (
                  <CurveCard
                    key={c.key}
                    curve={c}
                    conditioned={data.conditioned_curves.find((cc) => cc.key === c.key)}
                    referenceName={data.reference?.name || data.reference?.label}
                  />
                ))}
              </Box>
              {data.curves.length > CURVES_SHOWN && (
                <Button size="small" sx={{ mt: 1 }} onClick={() => setAllCurves((v) => !v)}>
                  {allCurves
                    ? `Show only the ${CURVES_SHOWN} levers that move the Overall most`
                    : `Show all ${data.curves.length} levers (${data.curves.length - CURVES_SHOWN} flatter ones hidden)`}
                </Button>
              )}
            </CardContent>
          </Card>

          {/* ── De-confounded evidence ─────────────────────────────────────────── */}
          <MatchedPairsCard rows={data.matched_pairs} />
          <BasinsCard basins={data.basins} maxOther={data.condition_max_other_changes} />

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
                  {data.gaps
                    .slice(gapPage * ROWS_PER_PAGE, gapPage * ROWS_PER_PAGE + ROWS_PER_PAGE)
                    .map((g, i) => (
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
              <Pager count={data.gaps.length} page={gapPage} onPage={setGapPage} />
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
