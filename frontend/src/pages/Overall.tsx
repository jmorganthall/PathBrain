// **Overall** — one ranking from both kinds of evidence.
//
// PathBrain had two ways of naming the best profile and a switch between them. The
// pooled Overall is every run a profile ever produced, medianed: thousands of iterations,
// a tiny error bar, and a weather bias nobody could read off it, because the firewall
// sits on one profile for hours and each profile samples its own slice of conditions.
// The ring is a duel round: one leg on A and one on B back to back, so the *difference*
// was measured under the same conditions — unbiased, and a handful of rounds wide.
//
// Neither is the truth; both are measurements of the same thing. This page shows the one
// fit over both (`overall_ranking.py`): every profile's Overall pulled from its pooled
// median toward what the ring measured head to head, weighted by how much each record can
// be trusted, with an error bar that comes from both. The reader gets the answer first,
// then what each record said alone, then what went in, then every profile, then the
// check that says whether the fit predicts the next night better than its parents.
import { useCallback, useEffect, useMemo, useState } from "react";
import { Link as RouterLink } from "react-router-dom";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Link from "@mui/material/Link";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import { useTheme } from "@mui/material/styles";
import CheckCircleIcon from "@mui/icons-material/CheckCircle";
import EmojiEventsIcon from "@mui/icons-material/EmojiEvents";
import HandshakeIcon from "@mui/icons-material/Handshake";
import MilitaryTechIcon from "@mui/icons-material/MilitaryTech";
import RefreshIcon from "@mui/icons-material/Refresh";
import StackedLineChartIcon from "@mui/icons-material/StackedLineChart";

import { api } from "../api/client";
import type { OverallCorner, OverallOut, OverallProfile } from "../api/types";
import { FoldCard, HelpTip } from "../components/Explain";
import StatTile from "../components/dashboard/StatTile";
import { fmtNum } from "../utils/format";
import { sopsColor } from "../theme";

const nameOf = (p: { name: string | null; label: string | null; fingerprint: string }) =>
  p.name || p.label || p.fingerprint.slice(0, 8);

const CHART_ROWS = 12;
const TABLE_ROWS = 25;

/** "+0.41" / "−0.12" / "0.00", with the sign always shown. */
function signed(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return "—";
  const s = fmtNum(Math.abs(v), digits);
  return v > 0 ? `+${s}` : v < 0 ? `−${s}` : s;
}

function profileHref(fp: string) {
  return `/profiles/${encodeURIComponent(fp)}`;
}

// ── The three corners ─────────────────────────────────────────────────────────────────

function CornerTile({
  corner,
  kind,
  title,
  what,
  icon,
  strong,
}: {
  corner: OverallCorner | null;
  kind: "pooled" | "ring" | "fused";
  title: string;
  what: string;
  icon: React.ReactNode;
  strong?: boolean;
}) {
  return (
    <Card
      variant="outlined"
      sx={{
        flex: 1,
        minWidth: 0,
        borderColor: strong ? "primary.main" : "divider",
        bgcolor: strong ? "action.selected" : "transparent",
      }}
    >
      <CardContent sx={{ py: 1.5, "&:last-child": { pb: 1.5 } }}>
        <Stack direction="row" spacing={1.25} alignItems="flex-start">
          <Box sx={{ flexShrink: 0, mt: 0.25 }}>{icon}</Box>
          <Box sx={{ minWidth: 0 }}>
            <Typography variant="overline" color="text.secondary" sx={{ lineHeight: 1.4 }}>
              {title}
              <HelpTip title={what} />
            </Typography>
            {corner ? (
              <>
                <Typography variant="h6" sx={{ lineHeight: 1.2 }} noWrap>
                  <Link component={RouterLink} to={profileHref(corner.fingerprint)} underline="hover" color="inherit">
                    {nameOf(corner)}
                  </Link>
                </Typography>
                <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
                  {kind === "pooled" &&
                    `pooled ${fmtNum(corner.pooled, 1)} · ${corner.iterations} iterations`}
                  {kind === "ring" &&
                    `${corner.rounds} round${corner.rounds === 1 ? "" : "s"} in the ring` +
                      (corner.rating != null ? ` · rating ${Math.round(corner.rating)}` : "")}
                  {kind === "fused" &&
                    `fused ${fmtNum(corner.fused, 1)} · ${corner.iterations} iterations + ${corner.rounds} round${corner.rounds === 1 ? "" : "s"}`}
                </Typography>
              </>
            ) : (
              <Typography variant="body2" color="text.disabled">
                nobody yet
              </Typography>
            )}
          </Box>
        </Stack>
      </CardContent>
    </Card>
  );
}

// ── The chart: fused Overall ± SE, pooled beside it, for the top of the table ───────────
//
// A dot-and-whisker per profile on one shared axis. The filled dot is the fused Overall
// with its bar; the hollow ring is where the pooled record alone had it, so the pull the
// ring applied is the visible distance between them. Shape carries the identity as well
// as colour, the values live in the table beneath, and every row has a hover.

function DotWhisker({ rows, tieSigma }: { rows: OverallProfile[]; tieSigma: number }) {
  const theme = useTheme();
  const lo = Math.min(...rows.map((r) => Math.min(r.fused - r.fused_se * tieSigma, r.pooled ?? r.fused)));
  const hi = Math.max(...rows.map((r) => Math.max(r.fused + r.fused_se * tieSigma, r.pooled ?? r.fused)));
  const pad = Math.max(0.25, (hi - lo) * 0.08);
  const min = lo - pad;
  const max = hi + pad;
  const x = (v: number) => `${((v - min) / (max - min)) * 100}%`;
  const leader = rows[0];
  const ticks = useMemo(() => {
    const span = max - min;
    const step = span > 8 ? 2 : span > 4 ? 1 : span > 2 ? 0.5 : 0.25;
    const out: number[] = [];
    for (let v = Math.ceil(min / step) * step; v <= max; v += step) out.push(Number(v.toFixed(2)));
    return out;
  }, [min, max]);

  return (
    <Box sx={{ position: "relative", pl: { xs: 0, sm: 0 } }}>
      {/* axis */}
      <Box sx={{ position: "relative", height: 18, ml: { xs: "120px", sm: "180px" }, mr: 1 }}>
        {ticks.map((t) => (
          <Typography
            key={t}
            variant="caption"
            color="text.disabled"
            sx={{ position: "absolute", left: x(t), transform: "translateX(-50%)", fontSize: 10 }}
          >
            {fmtNum(t, t % 1 === 0 ? 0 : 2)}
          </Typography>
        ))}
      </Box>
      {rows.map((r, i) => {
        const tied = r.tied_with_leader;
        const isLeader = i === 0;
        const title = `${nameOf(r)} — fused ${fmtNum(r.fused, 2)} ± ${fmtNum(r.fused_se, 2)}` +
          (r.pooled != null ? ` · pooled ${fmtNum(r.pooled, 2)} over ${r.iterations} iterations` : "") +
          (r.rounds ? ` · ${r.rounds} rounds moved it ${signed(r.ring_pull)}` : " · never fought in the ring") +
          (!isLeader && r.gap_to_leader != null ? ` · ${fmtNum(r.gap_to_leader, 2)} behind the leader` : "");
        return (
          <Tooltip key={r.fingerprint} title={title} placement="top" arrow>
            <Box
              sx={{
                display: "flex",
                alignItems: "center",
                height: 26,
                borderTop: i === 0 ? 1 : 0,
                borderColor: "divider",
                "&:hover": { bgcolor: "action.hover" },
              }}
            >
              <Box sx={{ width: { xs: 120, sm: 180 }, flexShrink: 0, pr: 1, display: "flex", alignItems: "center", gap: 0.5, minWidth: 0 }}>
                <Typography variant="caption" color="text.secondary" sx={{ width: 22, flexShrink: 0, textAlign: "right" }}>
                  {r.fused_rank ?? i + 1}
                </Typography>
                <Typography
                  variant="body2"
                  noWrap
                  component={RouterLink}
                  to={profileHref(r.fingerprint)}
                  sx={{ color: "inherit", textDecoration: "none", "&:hover": { textDecoration: "underline" }, minWidth: 0 }}
                >
                  {nameOf(r)}
                </Typography>
                {isLeader && <EmojiEventsIcon sx={{ fontSize: 14, color: "warning.main", flexShrink: 0 }} />}
              </Box>
              <Box sx={{ position: "relative", flex: 1, height: "100%", mr: 1 }}>
                {/* the leader's value, as a reference line through every row */}
                {leader && (
                  <Box
                    sx={{
                      position: "absolute", left: x(leader.fused), top: 0, bottom: 0,
                      borderLeft: "1px dashed", borderColor: "divider",
                    }}
                  />
                )}
                {/* whisker: ± tie_sigma × SE, the bar "tied" is judged on */}
                <Box
                  sx={{
                    position: "absolute",
                    left: x(r.fused - r.fused_se * tieSigma),
                    width: `calc(${x(r.fused + r.fused_se * tieSigma)} - ${x(r.fused - r.fused_se * tieSigma)})`,
                    top: "50%", height: 2, mt: "-1px",
                    bgcolor: isLeader || tied ? theme.palette.primary.main : theme.palette.text.disabled,
                    opacity: 0.6,
                  }}
                />
                {/* pooled: a hollow ring */}
                {r.pooled != null && (
                  <Box
                    sx={{
                      position: "absolute", left: x(r.pooled), top: "50%",
                      width: 9, height: 9, ml: "-4.5px", mt: "-4.5px", borderRadius: "50%",
                      border: 2, borderColor: theme.palette.text.secondary, bgcolor: "background.paper",
                    }}
                  />
                )}
                {/* fused: a filled dot */}
                <Box
                  sx={{
                    position: "absolute", left: x(r.fused), top: "50%",
                    width: 10, height: 10, ml: "-5px", mt: "-5px", borderRadius: "50%",
                    bgcolor: isLeader || tied ? theme.palette.primary.main : theme.palette.text.secondary,
                    boxShadow: `0 0 0 2px ${theme.palette.background.paper}`,
                  }}
                />
              </Box>
            </Box>
          </Tooltip>
        );
      })}
      <Stack direction="row" spacing={2} sx={{ mt: 1, ml: { xs: 0, sm: "180px" } }} flexWrap="wrap" useFlexGap>
        <Typography variant="caption" color="text.secondary">
          <Box component="span" sx={{ display: "inline-block", width: 9, height: 9, borderRadius: "50%", bgcolor: "primary.main", mr: 0.5, verticalAlign: "middle" }} />
          fused Overall, bar = ±{fmtNum(tieSigma, 0)} standard errors (the "tied" test)
        </Typography>
        <Typography variant="caption" color="text.secondary">
          <Box component="span" sx={{ display: "inline-block", width: 7, height: 7, borderRadius: "50%", border: 2, borderColor: "text.secondary", mr: 0.5, verticalAlign: "middle" }} />
          where the pooled record alone had it
        </Typography>
        <Typography variant="caption" color="text.secondary">
          dashed line = the leader
        </Typography>
      </Stack>
    </Box>
  );
}

// ── The page ──────────────────────────────────────────────────────────────────────────

export default function Overall() {
  const [data, setData] = useState<OverallOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await api.overall({ backtest: true }));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not load the Overall ranking.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const inputs = data && "slack" in data.inputs ? data.inputs : null;
  const corners = data && "fused" in data.corners ? data.corners : null;
  const best = data?.best ?? null;
  const rows = useMemo(
    () => (data?.profiles ?? []).filter((p) => !p.is_sqm_off),
    [data],
  );
  const chartRows = rows.filter((r) => r.eligible).slice(0, CHART_ROWS);
  const tableRows = showAll ? rows : rows.slice(0, TABLE_ROWS);
  const slackTau = inputs?.slack.tau ?? null;

  return (
    <Box>
      <Stack
        direction={{ xs: "column", sm: "row" }}
        justifyContent="space-between"
        alignItems={{ xs: "flex-start", sm: "center" }}
        spacing={1}
        sx={{ mb: 2 }}
      >
        <Box>
          <Typography variant="h4" sx={{ fontWeight: 700 }}>
            Overall
          </Typography>
          <Typography variant="body2" color="text.secondary">
            One ranking from both kinds of evidence: what every profile measured over time, corrected by what it did head to head.
            <HelpTip title="The pooled record says where each profile sits on its own, over thousands of iterations taken under whatever the weather was. The ring says how far apart two profiles are when measured back to back under the same conditions. This page fits the two together, weighting each by how much it can be trusted, and crowns off the result." />
          </Typography>
        </Box>
        <Button size="small" startIcon={<RefreshIcon />} onClick={() => void load()} disabled={loading}>
          Refresh
        </Button>
      </Stack>

      {loading && !data && (
        <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 2 }}>
          <CircularProgress size={20} />
          <Typography variant="body2" color="text.secondary">
            Fitting the pooled record and the ring together…
          </Typography>
        </Stack>
      )}
      {error && (
        <Alert severity="error" sx={{ mb: 2 }}>
          {error}
        </Alert>
      )}

      {data && !best && (
        <Alert severity="info" sx={{ mb: 2 }}>
          {data.verdict}
        </Alert>
      )}

      {data && best && (
        <>
          {/* ── The answer ────────────────────────────────────────────────────── */}
          <Card sx={{ mb: 2, borderLeft: 4, borderColor: "primary.main", opacity: loading ? 0.7 : 1 }}>
            <CardContent>
              <Stack direction={{ xs: "column", sm: "row" }} spacing={2} alignItems={{ sm: "center" }}>
                <Stack direction="row" spacing={1.5} alignItems="center" sx={{ minWidth: 0, flex: 1 }}>
                  <EmojiEventsIcon sx={{ color: "warning.main", fontSize: 40, flexShrink: 0 }} />
                  <Box sx={{ minWidth: 0 }}>
                    <Typography variant="overline" color="text.secondary" sx={{ lineHeight: 1.4 }}>
                      Run this profile
                    </Typography>
                    <Typography variant="h5" sx={{ lineHeight: 1.2 }}>
                      <Link component={RouterLink} to={profileHref(best.fingerprint)} underline="hover" color="inherit">
                        {nameOf(best)}
                      </Link>
                    </Typography>
                    <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
                      {best.iterations} iterations on the pooled record
                      {best.rounds
                        ? ` · ${best.rounds} round${best.rounds === 1 ? "" : "s"} against ${best.opponents} opponent${best.opponents === 1 ? "" : "s"} in the ring`
                        : " · never fought in the ring"}
                    </Typography>
                  </Box>
                </Stack>
                <Stack direction="row" spacing={2} alignItems="center" sx={{ flexShrink: 0 }}>
                  <Box sx={{ textAlign: "center" }}>
                    <Typography variant="h4" sx={{ fontWeight: 700, color: sopsColor(best.fused), lineHeight: 1 }}>
                      {fmtNum(best.fused, 1)}
                    </Typography>
                    <Typography variant="caption" color="text.secondary">
                      ± {fmtNum(best.fused_se, 2)} Overall
                    </Typography>
                  </Box>
                  {data.on_firewall != null && (
                    <Tooltip
                      title={
                        data.on_firewall
                          ? "The firewall is on this profile now."
                          : "The firewall is on a different profile. Arm Follow best (crowning policy: fused) to keep it on this one, or apply it from the profile page."
                      }
                    >
                      <Chip
                        size="small"
                        color={data.on_firewall ? "success" : "default"}
                        variant={data.on_firewall ? "filled" : "outlined"}
                        icon={data.on_firewall ? <CheckCircleIcon /> : undefined}
                        label={data.on_firewall ? "running now" : `running ${data.live ? nameOf(data.live) : "something else"}`}
                        sx={{ maxWidth: 220 }}
                      />
                    </Tooltip>
                  )}
                </Stack>
              </Stack>

              <Typography variant="body1" sx={{ mt: 1.5 }}>
                {data.verdict}
              </Typography>

              <Stack direction="row" spacing={1} sx={{ mt: 1.5 }} flexWrap="wrap" useFlexGap alignItems="center">
                {data.lead != null && data.noise_bar != null && (
                  <Tooltip
                    title={
                      data.clear
                        ? "The lead is larger than the combined noise of both records — a real ordering."
                        : "The lead is inside the combined noise, so the ordering is not demonstrated. The higher fused Overall still wins; it just hasn't been shown to."
                    }
                  >
                    <Chip
                      size="small"
                      color={data.clear ? "success" : "default"}
                      variant={data.clear ? "filled" : "outlined"}
                      label={`${signed(data.lead)} over ${data.runner_up ? nameOf(data.runner_up) : "next"} · noise ±${fmtNum(data.noise_bar, 2)}`}
                    />
                  </Tooltip>
                )}
                {data.rounds_to_settle != null && data.rounds_to_settle > 0 && (
                  <Tooltip title="Head-to-head rounds between the top two that would take the gap's error bar under the tie threshold, if the gap holds. This is what a duel night buys.">
                    <Chip size="small" variant="outlined" component={RouterLink} to="/duels" clickable label={`~${data.rounds_to_settle} round${data.rounds_to_settle === 1 ? "" : "s"} would settle it`} />
                  </Tooltip>
                )}
                {data.vs_sqm_off != null && (
                  <Tooltip title="How much better the crown measures than the best unshaped (SQM off) baseline — what shaping itself is worth, next to what picking a profile is worth.">
                    <Chip
                      size="small"
                      variant="outlined"
                      color={data.vs_sqm_off > 0 ? "success" : "warning"}
                      label={`${data.vs_sqm_off > 0 ? "+" : ""}${fmtNum(data.vs_sqm_off, 1)}% vs no shaper`}
                    />
                  </Tooltip>
                )}
              </Stack>

              {data.tied_count > 0 && (
                <Box sx={{ mt: 1.5, pt: 1.5, borderTop: 1, borderColor: "divider" }}>
                  <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 0.75 }}>
                    Tied with it on all the evidence ({data.tied_count}) — running any of these is defensible:
                  </Typography>
                  <Stack direction="row" spacing={0.75} flexWrap="wrap" useFlexGap>
                    {data.tied.map((p) => (
                      <Tooltip
                        key={p.fingerprint}
                        title={`fused ${fmtNum(p.fused, 2)} ± ${fmtNum(p.fused_se, 2)} · ${fmtNum(p.gap_to_leader, 2)} behind` +
                          (p.rounds_to_separate ? ` · ~${p.rounds_to_separate} round${p.rounds_to_separate === 1 ? "" : "s"} to separate` : "")}
                      >
                        <Chip size="small" variant="outlined" clickable component={RouterLink} to={profileHref(p.fingerprint)} label={nameOf(p)} />
                      </Tooltip>
                    ))}
                    {data.tied_count > data.tied.length && (
                      <Chip size="small" variant="outlined" label={`+${data.tied_count - data.tied.length} more`} />
                    )}
                  </Stack>
                </Box>
              )}
            </CardContent>
          </Card>

          {/* ── The three corners ─────────────────────────────────────────────── */}
          {corners && (
            <Card sx={{ mb: 2 }}>
              <CardContent>
                <Stack direction="row" spacing={1} alignItems="center" justifyContent="space-between" sx={{ mb: 1 }}>
                  <Box>
                    <Typography variant="h6">Three ways to read the same field</Typography>
                    <Typography variant="caption" color="text.secondary">
                      What each record says on its own, and what they say together. The fit is what automation follows.
                    </Typography>
                  </Box>
                  {corners.agree ? (
                    <Chip size="small" color="success" icon={<HandshakeIcon />} label="all three agree" sx={{ flexShrink: 0 }} />
                  ) : (
                    <Chip size="small" variant="outlined" label="they disagree" sx={{ flexShrink: 0 }} />
                  )}
                </Stack>
                <Stack direction={{ xs: "column", md: "row" }} spacing={1.5}>
                  <CornerTile
                    corner={corners.pooled}
                    kind="pooled"
                    title="Pooled record alone"
                    what="The highest median Overall over every run a profile ever produced. Precise (thousands of iterations) but each profile's runs sampled its own weather, so a lead here can be conditions rather than settings."
                    icon={<EmojiEventsIcon sx={{ color: "warning.main", fontSize: 30 }} />}
                  />
                  <CornerTile
                    corner={corners.ring}
                    kind="ring"
                    title="Ring alone"
                    what="The ring's #1 by fitted head-to-head rating: who beat whom, back to back, under shared weather. Free of the weather confound, but a night is a handful of rounds, each ±1.5 points, so it is wide."
                    icon={<MilitaryTechIcon sx={{ color: "info.main", fontSize: 30 }} />}
                  />
                  <CornerTile
                    corner={corners.fused}
                    kind="fused"
                    title="Together"
                    what="Both records fitted as measurements of one quantity: each pooled median trusted to its own error bar plus the measured slack, each ring round trusted to the ring's measured noise. The answer above."
                    icon={<StackedLineChartIcon sx={{ color: "primary.main", fontSize: 30 }} />}
                    strong
                  />
                </Stack>
                {!corners.agree && corners.pooled && corners.fused && corners.pooled.fingerprint !== corners.fused.fingerprint && (
                  <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1.5 }}>
                    The pooled record had <b>{nameOf(corners.pooled)}</b> on top; once the ring's rounds are heard, <b>{nameOf(corners.fused)}</b> is. That is the ring correcting a pooled lead that was worth less than it looked.
                  </Typography>
                )}
                {!corners.agree && corners.ring && corners.fused && corners.ring.fingerprint !== corners.fused.fingerprint && corners.pooled?.fingerprint === corners.fused.fingerprint && (
                  <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1.5 }}>
                    The ring alone puts <b>{nameOf(corners.ring)}</b> on top, but on {corners.ring.rounds} round{corners.ring.rounds === 1 ? "" : "s"} its claim is too thin to outweigh <b>{nameOf(corners.fused)}</b>'s pooled record. More rounds between them is what would change that.
                  </Typography>
                )}
              </CardContent>
            </Card>
          )}

          {/* ── What went in ──────────────────────────────────────────────────── */}
          {inputs && (
            <Card sx={{ mb: 2 }}>
              <CardContent>
                <Typography variant="h6" sx={{ mb: 0.5 }}>
                  What went in
                </Typography>
                <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.5 }}>
                  Every number here is read from data already on disk. Nothing is re-measured to build this page.
                </Typography>
                <Stack direction={{ xs: "column", md: "row" }} spacing={1.5} sx={{ mb: 2 }}>
                  <StatTile
                    label="Pooled record"
                    value={inputs.pooled_profiles}
                    unit="profiles"
                    caption={`${inputs.pooled_iterations.toLocaleString()} iterations, medianed per profile`}
                    help="Each profile's median Overall over every comparable run, with a standard error from its own spread (IQR/√n). The anchors of the fit."
                  />
                  <StatTile
                    label="Ring"
                    value={inputs.ring_rounds}
                    unit="rounds"
                    caption={`${inputs.ring_matches} matches · ${inputs.ring_sessions} sessions · ${inputs.ring_profiles} profiles fought`}
                    help={
                      `Every usable round on the duel ledger under this methodology, as a paired difference. ` +
                      (inputs.excluded_other_methodology ? `${inputs.excluded_other_methodology} match(es) fought under another methodology were left out (their margins are on another scale). ` : "") +
                      (inputs.weather_shifted_rounds ? `${inputs.weather_shifted_rounds} round(s) saw a weather shift between legs and still count.` : "")
                    }
                  />
                  <StatTile
                    label="Round noise"
                    value={`±${fmtNum(inputs.sigma_round, 2)}`}
                    unit="pts"
                    caption={inputs.sigma_basis === "measured"
                      ? `measured over ${inputs.noise?.rounds ?? "?"} rounds in ${inputs.noise?.matchups ?? "?"} matches`
                      : "fallback — too few rounds to measure the ring's own noise yet"}
                    tone={inputs.sigma_basis === "measured" ? "good" : "warn"}
                    help="What one ring round is worth: the spread of successive margins within a match, which cancels the match's true edge and leaves pure measurement noise. Each round counts 1/σ² in the fit."
                  />
                  <StatTile
                    label="Pooled slack"
                    value={`±${fmtNum(slackTau, 2)}`}
                    unit="pts"
                    caption={
                      inputs.slack.basis === "measured"
                        ? `measured from ${inputs.slack.pairs} fought pairs`
                        : `default — needs ${inputs.slack.needed_pairs} fought pairs to measure (${inputs.slack.pairs} so far)`
                    }
                    tone={inputs.slack.basis === "measured" ? "good" : "warn"}
                    help="How far a pooled median can sit from the truth for reasons more iterations never fix: weather, time of day, the instrument drifting. Measured from the pairs the ring has fought — the pooled difference and the ring's difference are two readings of one gap, and how much they disagree beyond both error bars is exactly this. It is never set by hand: a weight a person chooses on the evidence is what this ranking replaces."
                  />
                </Stack>

              </CardContent>
            </Card>
          )}

          {/* ── The chart ─────────────────────────────────────────────────────── */}
          {chartRows.length > 1 && (
            <Card sx={{ mb: 2 }}>
              <CardContent>
                <Typography variant="h6" sx={{ mb: 0.5 }}>
                  The top of the table
                </Typography>
                <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.5 }}>
                  Where each profile lands once both records are heard, and where the pooled record alone had it. Profiles whose bar reaches the leader's line are tied with it.
                </Typography>
                <DotWhisker rows={chartRows} tieSigma={data.tie_sigma} />
              </CardContent>
            </Card>
          )}

          {/* ── What the ring changed ─────────────────────────────────────────── */}
          {data.movers.length > 0 && (
            <Card sx={{ mb: 2 }}>
              <CardContent>
                <Typography variant="h6" sx={{ mb: 0.5 }}>
                  What the ring changed
                </Typography>
                <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1 }}>
                  The profiles whose place moved most once their head-to-head rounds were counted. A big move on few rounds is a claim; on many rounds it is a finding.
                </Typography>
                <Stack spacing={0.5}>
                  {data.movers.map((m) => (
                    <Typography key={m.fingerprint} variant="body2">
                      <Link component={RouterLink} to={profileHref(m.fingerprint)} underline="hover" color="inherit">
                        <b>{nameOf(m)}</b>
                      </Link>{" "}
                      {m.moved! > 0 ? "climbed" : "dropped"} from #{m.pooled_rank} to #{m.fused_rank} after {m.rounds} round{m.rounds === 1 ? "" : "s"} against {m.opponents} opponent{m.opponents === 1 ? "" : "s"}
                      {m.ring_pull != null ? ` (${signed(m.ring_pull)} points)` : ""}.
                    </Typography>
                  ))}
                </Stack>
              </CardContent>
            </Card>
          )}

          {/* ── Every profile ─────────────────────────────────────────────────── */}
          <Card sx={{ mb: 2 }}>
            <CardContent>
              <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 1 }}>
                <Box>
                  <Typography variant="h6">Every profile</Typography>
                  <Typography variant="caption" color="text.secondary">
                    Ranked on the fused Overall. Profiles under {data.min_iterations} iterations and under 8 rounds are listed but cannot hold the crown.
                  </Typography>
                </Box>
                {rows.length > TABLE_ROWS && (
                  <Button size="small" onClick={() => setShowAll((v) => !v)}>
                    {showAll ? `Top ${TABLE_ROWS}` : `Show all ${rows.length}`}
                  </Button>
                )}
              </Stack>
              <TableContainer>
                <Table size="small">
                  <TableHead>
                    <TableRow>
                      <TableCell align="right">#</TableCell>
                      <TableCell>Profile</TableCell>
                      <TableCell align="right">
                        Overall
                        <HelpTip title="The fused Overall ± its standard error — both records together." inline />
                      </TableCell>
                      <TableCell align="right">
                        Pooled
                        <HelpTip title="The median over every run, and how many iterations it rests on." inline />
                      </TableCell>
                      <TableCell align="right">
                        Ring
                        <HelpTip title="Rounds fought · distinct opponents. Zero means the ring has never tested this profile." inline />
                      </TableCell>
                      <TableCell align="right">
                        Pull
                        <HelpTip title="fused − pooled: how far the ring moved this profile from its pooled median. Positive = it did better head to head than its record suggested." inline />
                      </TableCell>
                      <TableCell align="right">
                        Moved
                        <HelpTip title="Places climbed (▲) or dropped (▼) against the pooled order." inline />
                      </TableCell>
                      <TableCell align="right">
                        Behind
                        <HelpTip title="Gap to the leader, and roughly how many head-to-head rounds would separate them if the gap holds. Blank = already separated." inline />
                      </TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {tableRows.map((r) => {
                      const isBest = best && r.fingerprint === best.fingerprint;
                      return (
                        <TableRow key={r.fingerprint} hover sx={{ opacity: r.eligible ? 1 : 0.6 }}>
                          <TableCell align="right">{r.fused_rank ?? "—"}</TableCell>
                          <TableCell sx={{ maxWidth: 260 }}>
                            <Stack direction="row" spacing={0.75} alignItems="center" sx={{ minWidth: 0 }}>
                              <Link component={RouterLink} to={profileHref(r.fingerprint)} underline="hover" color="inherit" noWrap>
                                {nameOf(r)}
                              </Link>
                              {isBest && <EmojiEventsIcon sx={{ fontSize: 15, color: "warning.main" }} />}
                              {r.tied_with_leader && <Chip size="small" variant="outlined" label="tied" sx={{ height: 18 }} />}
                              {!r.eligible && (
                                <Tooltip title={`Not enough evidence to hold the crown: ${r.iterations} iterations, ${r.rounds} rounds.`}>
                                  <Chip size="small" variant="outlined" label="thin" sx={{ height: 18 }} />
                                </Tooltip>
                              )}
                            </Stack>
                            {r.name && r.label && (
                              <Typography variant="caption" color="text.disabled" noWrap sx={{ display: "block", maxWidth: 260 }}>
                                {r.label}
                              </Typography>
                            )}
                          </TableCell>
                          <TableCell align="right" sx={{ whiteSpace: "nowrap" }}>
                            <b style={{ color: sopsColor(r.fused) }}>{fmtNum(r.fused, 2)}</b>
                            <Typography component="span" variant="caption" color="text.secondary"> ± {fmtNum(r.fused_se, 2)}</Typography>
                          </TableCell>
                          <TableCell align="right" sx={{ whiteSpace: "nowrap" }}>
                            {r.pooled != null ? fmtNum(r.pooled, 2) : "—"}
                            <Typography component="span" variant="caption" color="text.secondary"> · {r.iterations}</Typography>
                          </TableCell>
                          <TableCell align="right" sx={{ whiteSpace: "nowrap" }}>
                            {r.rounds ? (
                              <>
                                {r.rounds}
                                <Typography component="span" variant="caption" color="text.secondary"> · {r.opponents}</Typography>
                              </>
                            ) : (
                              <Typography component="span" variant="caption" color="text.disabled">never</Typography>
                            )}
                          </TableCell>
                          <TableCell align="right" sx={{ whiteSpace: "nowrap", color: r.ring_pull ? (r.ring_pull > 0 ? "success.main" : "error.main") : "text.disabled" }}>
                            {r.rounds ? signed(r.ring_pull) : "—"}
                          </TableCell>
                          <TableCell align="right" sx={{ whiteSpace: "nowrap" }}>
                            {r.moved ? (
                              <Typography component="span" variant="body2" sx={{ color: r.moved > 0 ? "success.main" : "error.main" }}>
                                {r.moved > 0 ? "▲" : "▼"}{Math.abs(r.moved)}
                              </Typography>
                            ) : (
                              <Typography component="span" variant="caption" color="text.disabled">—</Typography>
                            )}
                          </TableCell>
                          <TableCell align="right" sx={{ whiteSpace: "nowrap" }}>
                            {isBest ? (
                              <Typography component="span" variant="caption" color="text.disabled">leader</Typography>
                            ) : r.gap_to_leader != null ? (
                              <>
                                {fmtNum(r.gap_to_leader, 2)}
                                {r.rounds_to_separate ? (
                                  <Typography component="span" variant="caption" color="text.secondary"> · ~{r.rounds_to_separate} round{r.rounds_to_separate === 1 ? "" : "s"}</Typography>
                                ) : null}
                              </>
                            ) : (
                              <Typography component="span" variant="caption" color="text.disabled">—</Typography>
                            )}
                          </TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              </TableContainer>
            </CardContent>
          </Card>

          {/* ── Does it predict better than its parents? ──────────────────────── */}
          <Card sx={{ mb: 2 }}>
            <CardContent>
              <Typography variant="h6" sx={{ mb: 0.5 }}>
                Which verdict predicts the next night?
              </Typography>
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.5 }}>
                For each recent duel session, the ranking is rebuilt without it and asked to predict that night's margins. Lower is better. This is the check that says whether fusing the records is worth anything, measured on your own ledger.
              </Typography>
              {data.backtest ? (
                <>
                  <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} sx={{ mb: 1.5 }}>
                    {(["pooled", "ring", "fused"] as const).map((k) => (
                      <StatTile
                        key={k}
                        label={k === "pooled" ? "Pooled alone" : k === "ring" ? "Ring alone" : "Together"}
                        value={fmtNum(data.backtest!.mae[k], 2)}
                        unit="pts off"
                        tone={data.backtest!.best === k ? "good" : k === "fused" && data.backtest!.standing === "close" ? "info" : "idle"}
                        caption={data.backtest!.best === k ? "closest" : k === "fused" && data.backtest!.standing === "close" ? "within noise of the closest" : undefined}
                        help="Mean absolute error between the predicted and measured margin of each held-out match, weighted by rounds."
                      />
                    ))}
                  </Stack>
                  <Alert severity={data.backtest.standing === "worse" ? "warning" : data.backtest.standing === "best" ? "success" : "info"} variant="outlined">
                    {data.backtest.sentence}
                  </Alert>
                </>
              ) : (
                <Typography variant="body2" color="text.disabled">
                  Needs at least two duel sessions with rounds under this methodology.
                </Typography>
              )}
            </CardContent>
          </Card>

          {/* ── How it works ──────────────────────────────────────────────────── */}
          <FoldCard title="How this ranking is built" summary="Plain words, no formulas hidden.">
            <Stack spacing={1}>
              <Typography variant="body2">
                <b>Two records, one quantity.</b> Each profile has a true Overall we are trying to find. The pooled record measures each profile's <i>level</i> very precisely but under whatever the weather was. The ring measures the <i>difference</i> between two profiles under the same weather, but only a handful of times. Both are noisy readings of the same numbers.
              </Typography>
              <Typography variant="body2">
                <b>The fit.</b> Every profile starts at its pooled median, trusted to its own error bar plus the pooled slack. Every ring round then pulls the two profiles it compared toward the margin it measured, trusted to the ring's own round noise. The answer is the set of Overalls that disagrees least with all of it at once, and the error bars come out of the same arithmetic.
              </Typography>
              <Typography variant="body2">
                <b>The slack is the whole argument, as a number.</b> With no slack, thousands of iterations make a pooled median immovable and the ring can never change the order — that was the old pooled crown. With a huge slack, only the ring speaks — that was the old duel champion. The slack is measured from the pairs the ring has fought: the pooled record and the ring read the same gap, the ring's reading is unbiased, so how much they disagree beyond their error bars is how far pooled medians drift from the truth.
              </Typography>
              <Typography variant="body2">
                <b>What it does not do.</b> It never invents a number: a profile the ring never fought sits exactly where its pooled record puts it. It does not choose who the ring fights next — the ladder keeps reading the pooled crown so it stays an independent check. And when the evidence cannot separate the top profiles, it says so and names them, rather than crowning by a hair.
              </Typography>
            </Stack>
          </FoldCard>
        </>
      )}
    </Box>
  );
}
