// Dueling Champions — the duel ladder's own view.
//
// Everything else in PathBrain ranks profiles *observationally*: pool a profile's whole
// history and compare averages. This page is the other epistemology — the controlled
// trial. Profiles here are ranked by what they BEAT, in interleaved A/B/A/B matchups
// where both sides met the same weather by construction. So the vocabulary is a fight
// card, not a leaderboard of means: a reigning champion with a reign length, a league
// table of W–L–D records, a head-to-head grid, and the match tape of every verdict with
// the sequential test that ended it.
//
// The duel never writes the firewall. Acting on a verdict is the crowning policy's job
// (top-bar "Follow best" → Crowning policy = Duel champion), surfaced here as a banner.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";
import Accordion from "@mui/material/Accordion";
import AccordionDetails from "@mui/material/AccordionDetails";
import AccordionSummary from "@mui/material/AccordionSummary";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import Collapse from "@mui/material/Collapse";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import Divider from "@mui/material/Divider";
import FormControlLabel from "@mui/material/FormControlLabel";
import LinearProgress from "@mui/material/LinearProgress";
import MenuItem from "@mui/material/MenuItem";
import Link from "@mui/material/Link";
import Snackbar from "@mui/material/Snackbar";
import Stack from "@mui/material/Stack";
import Switch from "@mui/material/Switch";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import TablePagination from "@mui/material/TablePagination";
import TableSortLabel from "@mui/material/TableSortLabel";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import StopIcon from "@mui/icons-material/Stop";
import MilitaryTechIcon from "@mui/icons-material/MilitaryTech";
import ScheduleIcon from "@mui/icons-material/Schedule";
import GavelIcon from "@mui/icons-material/Gavel";
import ExpandMoreIcon from "@mui/icons-material/ExpandMore";
import SportsMmaIcon from "@mui/icons-material/SportsMma";

import { api } from "../api/client";
import type { Theme } from "@mui/material/styles";
import type {
  DuelCard,
  DuelConfig,
  DuelHealth,
  DuelLive,
  DuelMatchup,
  DuelSession,
  DuelStanding,
  DuelStandings,
} from "../api/types";
import { fmtDateTime, fmtNum } from "../utils/format";

const isRunning = (d: DuelSession | null) =>
  !!d && !!d.status && ["pending", "running"].includes(d.status);

const pct = (v: number | null | undefined) =>
  v == null ? "—" : `${Math.round(v * 100)}%`;

// A margin is signed from the row's own point of view: + = it was the better profile.
const marginColor = (v: number | null | undefined) =>
  v == null || Math.abs(v) < 0.05 ? "text.secondary" : v > 0 ? "success.main" : "error.main";

const fmtMargin = (v: number | null | undefined) =>
  v == null ? "—" : `${v > 0 ? "+" : ""}${fmtNum(v, 2)}`;

const hhmm = (h: number, m: number) =>
  `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}`;

// A window is entered as two clock times, so its length is always shown back in plain
// hours/minutes — "22:15 → 01:45" is easy to set but hard to add up in your head.
// Offered lengths for a on-demand session. A duel is only useful in whole matches, and a
// match is a handful of rounds each costing two benchmark runs — so under ~30 minutes the
// window closes mid-match and the session decides nothing.
const DUEL_LENGTHS = [30, 60, 120, 240, 480, 720];

const fmtWindow = (minutes: number) => {
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return h ? `${h}h${m ? ` ${m}m` : ""}` : `${m}m`;
};

// Minutes from now until the next occurrence of a wall-clock time (tomorrow if it has
// already passed today), which is what an on-demand "duel until 06:00" means.
const minutesUntil = (clock: string): number => {
  const [h, m] = clock.split(":").map((x) => parseInt(x, 10));
  if (!Number.isFinite(h) || !Number.isFinite(m)) return 0;
  const now = new Date();
  const end = new Date(now);
  end.setHours(h, m, 0, 0);
  if (end <= now) end.setDate(end.getDate() + 1);
  return Math.max(1, Math.round((end.getTime() - now.getTime()) / 60000));
};

// The clock time `minutes` from now — seeds the "duel until" picker from the configured
// window so the default on-demand run matches the nightly one.
// "20:34" is what the <input type=time> holds; people read "8:34 PM".
const formatClock = (clock: string): string => {
  const [h, m] = clock.split(":").map((x) => parseInt(x, 10));
  if (!Number.isFinite(h) || !Number.isFinite(m)) return clock;
  const d = new Date();
  d.setHours(h, m, 0, 0);
  return d.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
};

const clockIn = (minutes: number): string => {
  const t = new Date(Date.now() + minutes * 60000);
  return hhmm(t.getHours(), t.getMinutes());
};

// "6h from now" alone is ambiguous at 7am — 1:08 PM today and 5:00 AM tomorrow are both
// "from now". Naming the day is what makes the number checkable at a glance.
const describeUntil = (clock: string): string => {
  const [h, m] = clock.split(":").map((x) => parseInt(x, 10));
  if (!Number.isFinite(h) || !Number.isFinite(m)) return "";
  // Derive the end instant from the CHOSEN clock, then format that — deriving it from
  // now+minutes instead re-rounds the seconds away and prints a time one minute off the
  // field you just set, which reads like the arithmetic is broken.
  const now = new Date();
  const end = new Date(now);
  end.setHours(h, m, 0, 0);
  if (end <= now) end.setDate(end.getDate() + 1);
  const minutes = Math.max(1, Math.round((end.getTime() - now.getTime()) / 60000));
  const day = end.getDate() === now.getDate() ? "today" : "tomorrow";
  const at = end.toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
  return `Runs about ${fmtWindow(minutes)} — until ${at} ${day}.`;
};

// Does the nightly window run past midnight into the next morning?
// How many matches of a session to render before asking. Enough to see the shape of the
// night (who has been defending, whether the belt moved) without printing the whole ledger.
const BOUTS_PER_SESSION = 8;

const crossesMidnight = (cfg: DuelConfig): boolean =>
  cfg.end_hour * 60 + cfg.end_minute <= cfg.hour * 60 + cfg.minute;

// One responsive column set for every settings row: single column on a phone, filling
// out as the screen allows. Replaces a flex-wrap row whose captions drifted away from
// the fields they described.
// Preset cards: one per line on a phone, side by side once there's room.
// Preset key → the name shown on its card, for the collapsed settings summary.
const PRESET_NAME: Record<string, string> = {
  snap: "Snap call",
  quick: "Quick call",
  balanced: "Balanced",
  strict: "Only when certain",
  custom: "Custom",
};

// Sortable standings. The server returns ladder order (points, then decisive-win rate,
// then round-win rate); clicking a header re-sorts client-side without refetching.
type StandingSortKey =
  | "rank"
  | "rating"
  | "rating_floor"
  | "name"
  | "wins"
  | "points"
  | "win_rate"
  | "pair_win_rate"
  | "median_margin"
  | "overall"
  | "pooled_rank"
  | "opponents"
  | "championships"
  | "last_dueled_at";

const standingValue = (r: DuelStanding, key: StandingSortKey): number | string | null => {
  switch (key) {
    case "rank":
      return r.rank;
    case "rating":
      return r.rating ?? null;
    case "rating_floor":
      return r.rating_floor ?? null;
    case "name":
      return (r.name || r.label || "").toLowerCase();
    case "wins":
      return r.wins;
    case "points":
      return r.points;
    case "win_rate":
      return r.win_rate;
    case "pair_win_rate":
      return r.pair_win_rate;
    case "median_margin":
      return r.median_margin;
    case "overall":
      return r.overall ?? null;
    case "pooled_rank":
      // Sorted by the score itself so "best first" means the same thing as every other
      // column; the printed number is just that ordering made explicit.
      return r.overall ?? null;
    case "opponents":
      return r.opponents;
    case "championships":
      return r.championships;
    case "last_dueled_at":
      return r.last_dueled_at ? Date.parse(r.last_dueled_at) : null;
  }
};

const compareStandings = (a: DuelStanding, b: DuelStanding, key: StandingSortKey, dir: "asc" | "desc") => {
  const va = standingValue(a, key);
  const vb = standingValue(b, key);
  // Nulls always sort last, whichever direction — an unmeasured profile is never "top".
  if (va == null && vb == null) return 0;
  if (va == null) return 1;
  if (vb == null) return -1;
  const cmp =
    typeof va === "string" && typeof vb === "string" ? va.localeCompare(vb) : (va as number) - (vb as number);
  return dir === "asc" ? cmp : -cmp;
};

// How a profile ranks on the pooled Overall, among the profiles that have duelled — the
// second opinion on the same rows, so "ring rank vs measured rank" is one glance.
const pooledRanking = (rows: DuelStanding[]): Map<string, number> => {
  const scored = rows
    .filter((r) => r.overall != null)
    .sort((a, b) => (b.overall as number) - (a.overall as number));
  return new Map(scored.map((r, i) => [r.fingerprint, i + 1]));
};

// The duel rating: a Bradley-Terry strength fitted to every round on the ledger, printed
// on the Elo scale. The error bar is not decoration — a rating built on eight rounds and
// one built on eight hundred are different claims, and a table that prints them
// identically invites the reader to act on the wrong one. It is the headline number but
// The table orders on this — the ring's own finding about who beat whom.
function RatingCell({ row }: { row: DuelStanding }) {
  if (row.rating == null) return <>—</>;
  const se = row.rating_se;
  const rounds = row.rating_pairs ?? 0;
  // WHY it's provisional, not just that it is. A bare "?" next to a #1 rating reads as a
  // typo; "1 opponent" reads as the actual caveat — that the whole rating rests on a single
  // edge of the comparison network, and is really a statement about that one opponent
  // rather than a position among all of them.
  const why =
    row.opponents < 2
      ? `${row.opponents} opponent`
      : rounds < 20
        ? `${rounds} round${rounds === 1 ? "" : "s"}`
        : "thin record";
  return (
    <Stack direction="row" spacing={0.5} alignItems="baseline" justifyContent="flex-end">
      <Tooltip
        title={
          `Fitted from ${rounds} interleaved round${rounds === 1 ? "" : "s"} against ` +
          `${row.opponents} opponent${row.opponents === 1 ? "" : "s"}` +
          (row.expected_pair_wins != null
            ? ` · won ${row.pair_wins} where the fit expected ${row.expected_pair_wins}`
            : "") +
          (row.rating != null
            ? ` · ranked on ${Math.round(row.rating)} (the fitted rating)`
            : "")
        }
      >
        <span>
          <Typography
            component="span"
            variant="body2"
            sx={{ fontVariantNumeric: "tabular-nums", opacity: row.rating_provisional ? 0.7 : 1 }}
          >
            {Math.round(row.rating)}
          </Typography>
          {se != null && (
            <Typography component="span" variant="caption" color="text.secondary">
              {" "}
              ±{Math.round(se)}
            </Typography>
          )}
        </span>
      </Tooltip>
      {row.rating_provisional && (
        <Tooltip
          title={
            row.opponents < 2
              ? "Provisional: it has faced one opponent, so its rating rests on a single comparison — it says this profile beat that one, not where it stands in the field. It is still ranked where the evidence puts it; race it against someone else to confirm."
              : "Provisional: too few rounds to separate it from the middle of the field yet. It is still ranked where the evidence puts it — more matches will pin it down."
          }
        >
          <Chip
            size="small"
            variant="outlined"
            color="warning"
            label={why}
            sx={{ height: 17, "& .MuiChip-label": { px: 0.6, fontSize: 10 } }}
          />
        </Tooltip>
      )}
    </Stack>
  );
}

// The gap between the two rankings, shown where the eye already is (next to the rank).
// Up = the ring rates it higher than its all-history score does.
function RankGap({ ringRank, pooledRank }: { ringRank: number; pooledRank?: number }) {
  if (!pooledRank || pooledRank === ringRank) return null;
  const up = ringRank < pooledRank;
  const gap = Math.abs(pooledRank - ringRank);
  return (
    <Tooltip
      title={
        up
          ? `Ranks ${gap} place${gap === 1 ? "" : "s"} higher in the ring than its measured Overall does — it beats profiles the raw record rates above it.`
          : `Ranks ${gap} place${gap === 1 ? "" : "s"} lower in the ring than its measured Overall does — the raw record likes it more than its opponents did.`
      }
    >
      <Typography
        variant="caption"
        sx={{ color: up ? "success.main" : "warning.main", whiteSpace: "nowrap" }}
      >
        {up ? "▲" : "▼"}
        {gap}
      </Typography>
    </Tooltip>
  );
}

function StandingHeader({
  id,
  label,
  align,
  orderBy,
  order,
  onSort,
  tip,
}: {
  id: StandingSortKey;
  label: string;
  align?: "right";
  orderBy: StandingSortKey;
  order: "asc" | "desc";
  onSort: (key: StandingSortKey) => void;
  tip?: string;
}) {
  const active = orderBy === id;
  const control = (
    <TableSortLabel active={active} direction={active ? order : "asc"} onClick={() => onSort(id)}>
      {label}
    </TableSortLabel>
  );
  return (
    // Headers stay on one line: 11 columns at a narrow width otherwise wrap mid-word
    // ("W– L–D"), and the table scrolls horizontally inside its own container anyway.
    <TableCell align={align} sortDirection={active ? order : false} sx={{ whiteSpace: "nowrap" }}>
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

// Why a profile is in the queue, in words.
const CARD_REASON: Record<string, string> = {
  "pooled-crown": "the all-history crown, coming to take the belt",
  contender: "near the crown — the matchup that can change the answer",
  "limited-data": "could beat the crown at its best, not measured enough yet",
  stale: "confident, but its data has gone stale",
  untested: "never measured under the current methodology",
};

const PRESET_GRID = {
  display: "grid",
  gap: 1.5,
  gridTemplateColumns: {
    xs: "1fr",
    sm: "repeat(2, minmax(0, 1fr))",
    lg: "repeat(4, minmax(0, 1fr))",
  },
} as const;

const FIELD_GRID = {
  display: "grid",
  gap: 1.5,
  gridTemplateColumns: {
    xs: "1fr",
    sm: "repeat(2, minmax(0, 1fr))",
    lg: "repeat(4, minmax(0, 1fr))",
  },
} as const;

// A labelled clock input with its explanation attached underneath — on a phone there is
// no hover, so the help has to be on the page, not in a tooltip.
function TimeField({
  label,
  value,
  onChange,
  disabled,
  helper,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  disabled?: boolean;
  helper?: string;
}) {
  return (
    <TextField
      size="small"
      type="time"
      label={label}
      value={value}
      disabled={disabled}
      helperText={helper}
      onChange={(e) => onChange(e.target.value)}
      InputLabelProps={{ shrink: true }}
      fullWidth
    />
  );
}

function NumField({
  label,
  value,
  onCommit,
  disabled,
  min = 0,
  step = 1,
  helper,
}: {
  label: string;
  value: number;
  onCommit: (v: number) => void;
  disabled?: boolean;
  min?: number;
  step?: number;
  // Plain-English explanation rendered under the field. Not a tooltip: a phone can't
  // hover, and these settings are exactly the ones that need explaining.
  helper?: string;
}) {
  // Local draft so typing doesn't fire a PUT per keystroke — committed on blur/Enter.
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);
  const commit = () => {
    const n = Number(draft);
    if (Number.isFinite(n) && n !== value) onCommit(Math.max(min, n));
    else setDraft(String(value));
  };
  return (
    <TextField
      size="small"
      type="number"
      label={label}
      value={draft}
      disabled={disabled}
      helperText={helper}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") (e.target as HTMLInputElement).blur();
      }}
      inputProps={{ min, step, inputMode: "numeric" }}
      fullWidth
    />
  );
}

// `median_delta` is stored challenger-minus-holder, which reads backwards next to a
// verdict ("t10ms wins" beside "Δ -3.00"), so the tape states the gap from the winner's
// side — and for a draw, from the challenger's, since neither side is "the winner".
function marginPhrase(m: DuelMatchup): string {
  if (m.median_delta == null) return "no usable margin";
  if (matchOutcome(m) !== "incumbent" && matchOutcome(m) !== "challenger")
    return `challenger Δ ${fmtNum(m.median_delta, 2)}`;
  return `won by ${fmtNum(Math.abs(m.median_delta), 2)} Overall pts`;
}

/**
 * A draw is a verdict — the ring fought this and says the two are equal. An ABORT is the
 * ladder failing to measure anything: the window closed, or the rounds came back with no
 * Overall to compare. They were the same word, so a ledger full of failures read as a
 * field of evenly matched profiles. Derived from the recorded reason so matches written
 * before the distinction existed still read correctly.
 */
function matchOutcome(m: DuelMatchup): "incumbent" | "challenger" | "draw" | "aborted" {
  if (m.verdict === "aborted") return "aborted";
  if (m.verdict === "draw") {
    const reason = (m.reason ?? "").trim().toLowerCase();
    if (reason.startsWith("aborted:") || reason.startsWith("window closed")) return "aborted";
    return "draw";
  }
  return m.verdict as "incumbent" | "challenger";
}

// The verdict of one match, phrased as a result rather than a raw enum.
function VerdictChip({ m }: { m: DuelMatchup }) {
  const result = matchOutcome(m);
  if (result === "aborted")
    return (
      <Tooltip title="No result — the ladder couldn't measure this match. It does not count as a draw, and the pair is not on rematch cooldown.">
        <Chip size="small" label="no result" variant="outlined" color="warning" />
      </Tooltip>
    );
  if (result === "draw")
    return <Chip size="small" label="draw" variant="outlined" color="default" />;
  const winner =
    result === "challenger"
      ? m.challenger_name || m.challenger_label
      : m.incumbent_name || m.incumbent_label;
  return (
    <Chip
      size="small"
      color={m.verdict === "challenger" ? "warning" : "success"}
      label={`${winner} wins`}
      icon={<SportsMmaIcon />}
    />
  );
}

// "Why is everything a draw?" — answered on the page instead of left to arithmetic.
//
// Each round won moves the sequential test up by ln(p1/0.5); each round lost moves it down
// by ln((1-p1)/0.5), a BIGGER step. So the round cap and the evidence bar interact: at
// p1=0.70 / alpha=0.05 with a 15-round cap, a winner needs 13 of 15 (87%) — a profile
// genuinely winning 80% of its rounds can never be declared the winner, however many
// nights it runs. That's invisible from the numbers, so the card states it outright and
// warns when the cap is set below the rule's reach.
function DecisionCost({ cfg }: { cfg: DuelConfig }) {
  const d = cfg.decision;
  if (!d) return null;
  if (cfg.method === "margins") {
    return (
      <Alert severity="info" icon={false} sx={{ mt: 2, py: 0.5 }}>
        <Typography variant="caption" component="div">
          <b>What it takes to win a match.</b> {d.streak_pairs ?? "—"} wins in a row ends it
          immediately. Short of that, matches are judged on the <b>size</b> of each
          round's margin, not just who won it — so a profile that wins by a consistent
          amount is called even when it drops the odd round. The fastest possible verdict is{" "}
          {d.sweep_pairs ?? "—"} consistently one-sided rounds. Testing after every round
          would inflate false alarms, so the threshold is tightened to{" "}
          {d.nominal_alpha?.toFixed(4)} (your {cfg.alpha} error rate ÷ {d.peek_penalty}),
          which holds real false verdicts near {Math.round(cfg.alpha * 100)}%. There is no
          cap at which a winner becomes unreachable — more rounds only ever help.
        </Typography>
      </Alert>
    );
  }
  const impossible = d.wins_needed == null;
  return (
    <Alert
      severity={d.restrictive ? "warning" : "info"}
      icon={false}
      sx={{ mt: 2, py: 0.5 }}
    >
      <Typography variant="caption" component="div">
        <b>What it takes to win a match.</b> The fastest possible verdict is a{" "}
        {d.sweep_pairs}-round clean sweep.{" "}
        {impossible ? (
          <>
            At your {cfg.max_pairs}-round cap <b>no result can ever be decisive</b> — every
            match will be recorded as a draw. Raise the cap above {d.sweep_pairs}, or lower
            the evidence bar.
          </>
        ) : (
          <>
            At your {cfg.max_pairs}-round cap a winner must take{" "}
            <b>
              {d.wins_needed} of {cfg.max_pairs}
            </b>{" "}
            ({Math.round((d.win_rate_needed ?? 0) * 100)}%). Anything closer is a draw
            {d.restrictive
              ? " — that's a near-sweep, so most matches will draw. Raise the round cap (40 gives a winner room at 70%), or lower the evidence bar below."
              : "."}
          </>
        )}
      </Typography>
    </Alert>
  );
}

// One match on the tape: the two corners, the round scoreline, and how the sequential
// test ended it. The round bar is the actual evidence — each segment is one interleaved
// A/B round that one side won.
function BoutRow({ m }: { m: DuelMatchup }) {
  const total = Math.max(1, m.wins_incumbent + m.wins_challenger);
  const incShare = (m.wins_incumbent / total) * 100;
  return (
    <Box sx={{ py: 1 }}>
      <Stack
        direction={{ xs: "column", md: "row" }}
        spacing={1}
        alignItems={{ xs: "flex-start", md: "center" }}
        justifyContent="space-between"
      >
        <Box sx={{ minWidth: 0 }}>
          <Typography variant="body2" sx={{ fontWeight: 500 }}>
            {m.incumbent_name || m.incumbent_label}{" "}
            <Typography component="span" variant="caption" color="text.secondary">
              (holder)
            </Typography>{" "}
            vs {m.challenger_name || m.challenger_label}
          </Typography>
          {(m.incumbent_name || m.challenger_name) && (
            <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
              {m.incumbent_label} vs {m.challenger_label}
            </Typography>
          )}
        </Box>
        <Stack direction="row" spacing={1} alignItems="center">
          <Typography variant="caption" color="text.secondary">
            {m.wins_incumbent}–{m.wins_challenger} in {m.pairs} rounds · {marginPhrase(m)}
          </Typography>
          <VerdictChip m={m} />
        </Stack>
      </Stack>
      <Tooltip
        title={`Round wins — holder ${m.wins_incumbent}, challenger ${m.wins_challenger}. LLR ${fmtNum(
          m.llr_incumbent,
          2
        )} / ${fmtNum(m.llr_challenger, 2)}.`}
      >
        <Box
          sx={{
            mt: 0.5,
            display: "flex",
            height: 6,
            borderRadius: 3,
            overflow: "hidden",
            bgcolor: "action.hover",
          }}
        >
          <Box sx={{ width: `${incShare}%`, bgcolor: "success.main" }} />
          <Box sx={{ width: `${100 - incShare}%`, bgcolor: "warning.main" }} />
        </Box>
      </Tooltip>
      <Typography variant="caption" color="text.secondary">
        {m.reason}
      </Typography>
    </Box>
  );
}


// ── The match in progress ───────────────────────────────────────────────────────────────
//
// "round 4 (2-1)" told you a match was happening and almost nothing else: not whose wins
// those were, not by how much, not how close a verdict was. All three are the question.
// So: both names with their own tally, a bar whose fill is the split, the median margin
// signed from the challenger's side (which is what the verdict is actually decided on),
// and how far the match has to go before it can end.
function MatchScoreboard({ live }: { live: DuelLive }) {
  // `pairs` is the wire name (an API key, unchanged); `rounds` is what a person reads.
  const { incumbent, challenger, leader, pairs: rounds } = live;
  const decided = incumbent.wins + challenger.wins;
  // Split of the bar. Level (or nothing yet) sits at the midpoint rather than implying a
  // lead in either direction.
  const share = decided > 0 ? (challenger.wins / decided) * 100 : 50;
  const incName = incumbent.name || incumbent.label || "belt";
  const chaName = challenger.name || challenger.label || "challenger";
  const margin = live.median_margin;

  const side = (name: string, wins: number, ahead: boolean, align: "left" | "right") => (
    <Box sx={{ textAlign: align, minWidth: 0, flex: 1 }}>
      <Typography
        variant="body2"
        noWrap
        sx={{ fontWeight: ahead ? 700 : 500, color: ahead ? "text.primary" : "text.secondary" }}
      >
        {name}
      </Typography>
      <Typography
        variant="h4"
        sx={{ lineHeight: 1.1, color: ahead ? "primary.main" : "text.secondary", fontVariantNumeric: "tabular-nums" }}
      >
        {wins}
      </Typography>
    </Box>
  );

  return (
    <Box sx={{ mt: 1.5, p: 1.5, borderRadius: 1, border: 1, borderColor: "divider" }}>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
        <Chip size="small" label={`Match ${live.bout}`} />
        <Typography variant="caption" color="text.secondary" sx={{ flexGrow: 1 }}>
          round {rounds + 1} · {rounds} decided
        </Typography>
        <Tooltip title="Why this profile is in the ring at all.">
          <Typography variant="caption" color="text.secondary" noWrap sx={{ maxWidth: "50%" }}>
            {challenger.why}
          </Typography>
        </Tooltip>
      </Stack>

      <Stack direction="row" spacing={2} alignItems="flex-end">
        {side(`${incName} (belt)`, incumbent.wins, leader === "incumbent", "left")}
        <Typography variant="caption" color="text.secondary" sx={{ pb: 1 }}>
          vs
        </Typography>
        {side(chaName, challenger.wins, leader === "challenger", "right")}
      </Stack>

      {/* The split, as one bar: left is the belt, right is the challenger. */}
      <Box
        sx={{
          mt: 1, height: 8, borderRadius: 4, overflow: "hidden", display: "flex",
          bgcolor: "action.hover",
        }}
      >
        <Box sx={{ width: `${100 - share}%`, bgcolor: leader === "incumbent" ? "primary.main" : "text.disabled" }} />
        <Box sx={{ width: `${share}%`, bgcolor: leader === "challenger" ? "success.main" : "text.disabled" }} />
      </Box>

      <Stack direction="row" spacing={2} sx={{ mt: 1 }} flexWrap="wrap" useFlexGap>
        <Tooltip title="The median Overall-point gap across the rounds so far, signed from the challenger's side: positive means the challenger is ahead. This — not the round count — is what the verdict is decided on, because it keeps the size of each win instead of throwing it away.">
          <Box>
            <Typography variant="caption" color="text.secondary">
              Median margin
            </Typography>
            <Typography
              variant="body1"
              sx={{
                lineHeight: 1.3,
                color: margin == null ? "text.secondary" : margin > 0 ? "success.main" : margin < 0 ? "primary.main" : "text.primary",
              }}
            >
              {margin == null ? "—" : `${margin > 0 ? "+" : ""}${fmtNum(margin, 2)}`}
              {margin != null && (
                <Typography component="span" variant="caption" color="text.secondary">
                  {" "}
                  {margin > 0 ? chaName : margin < 0 ? incName : "level"}
                </Typography>
              )}
            </Typography>
          </Box>
        </Tooltip>

        <Tooltip title={`A match can't be called before ${live.min_pairs} rounds, and is a draw if it reaches ${live.max_pairs} undecided.`}>
          <Box>
            <Typography variant="caption" color="text.secondary">
              Rounds
            </Typography>
            <Typography variant="body1" sx={{ lineHeight: 1.3 }}>
              {rounds}
              <Typography component="span" variant="caption" color="text.secondary">
                {" "}
                / {live.min_pairs} min · {live.max_pairs} cap
              </Typography>
            </Typography>
          </Box>
        </Tooltip>

        <Tooltip title="An unbroken run of round wins ends the match on its own — a clean run is worth more than a long scrappy one, and this is how close either side is to it.">
          <Box>
            <Typography variant="caption" color="text.secondary">
              Streak
            </Typography>
            <Typography variant="body1" sx={{ lineHeight: 1.3 }}>
              {live.streak.length}/{live.streak.needed}
              {live.streak.side && (
                <Typography component="span" variant="caption" color="text.secondary">
                  {" "}
                  {live.streak.side === "challenger" ? chaName : incName}
                </Typography>
              )}
            </Typography>
          </Box>
        </Tooltip>

        {live.p_value != null && (
          <Tooltip title={`How unlikely this run of margins would be if the two profiles were actually equal. The match is called when it drops below ${live.alpha} (already corrected for checking after every round).`}>
            <Box>
              <Typography variant="caption" color="text.secondary">
                Evidence
              </Typography>
              <Typography
                variant="body1"
                sx={{ lineHeight: 1.3, color: live.p_value <= live.alpha ? "success.main" : "text.primary" }}
              >
                p {fmtNum(live.p_value, 3)}
                <Typography component="span" variant="caption" color="text.secondary">
                  {" "}
                  / {fmtNum(live.alpha, 3)} needed
                </Typography>
              </Typography>
            </Box>
          </Tooltip>
        )}
      </Stack>

      {/* Every round so far, in order — a steady lead and one lucky round look nothing alike,
          and the median alone can't tell them apart. */}
      {live.margins.length > 1 && (
        <Box sx={{ display: "flex", alignItems: "center", gap: "2px", height: 26, mt: 1 }}>
          {live.margins.map((m, i) => (
            <Tooltip key={i} title={`round ${rounds - live.margins.length + i + 1}: ${m > 0 ? "+" : ""}${fmtNum(m, 2)} ${m > 0 ? chaName : incName}`}>
              <Box
                sx={{
                  flex: 1, minWidth: 3, height: `${Math.min(100, 20 + Math.abs(m) * 30)}%`,
                  alignSelf: m > 0 ? "flex-start" : "flex-end",
                  borderRadius: 0.5,
                  bgcolor: m > 0 ? "success.main" : "primary.main",
                  opacity: 0.85,
                }}
              />
            </Tooltip>
          ))}
        </Box>
      )}
    </Box>
  );
}

export default function Duels() {
  const navigate = useNavigate();
  const [cfg, setCfg] = useState<DuelConfig | null>(null);
  const [status, setStatus] = useState<DuelSession | null>(null);
  const [table, setTable] = useState<DuelStandings | null>(null);
  const [ledger, setLedger] = useState<DuelSession[]>([]);
  const [policy, setPolicy] = useState<"pooled" | "duel" | null>(null);
  // On-demand runs are also set by end time ("duel until 06:00"); the minutes the API
  // wants are derived from the clock at the moment you press the button.
  const [untilClock, setUntilClock] = useState<string | null>(null);
  const [liveFp, setLiveFp] = useState<string | null>(null);
  const [health, setHealth] = useState<DuelHealth | null>(null);
  const [askDuration, setAskDuration] = useState(false);
  const [dialogMinutes, setDialogMinutes] = useState(120);
  const [card, setCard] = useState<DuelCard | null>(null);
  const [cardBusy, setCardBusy] = useState(false);
  const [showAdvanced, setShowAdvanced] = useState(false);
  const [showRules, setShowRules] = useState(false);
  const [loadingStandings, setLoadingStandings] = useState(true);
  const [loadingLedger, setLoadingLedger] = useState(true);
  const [orderBy, setOrderBy] = useState<StandingSortKey>("rank");
  const [order, setOrder] = useState<"asc" | "desc">("asc");
  // A long ladder is both unreadable and slow to paint (every row carries several
  // tooltips), so the table pages like Settings Impact rather than rendering everything.
  const [page, setPage] = useState(0);
  const [rowsPerPage, setRowsPerPage] = useState(25);
  // How many sessions of the match tape to render — the rest are a click away.
  const [tapeShown, setTapeShown] = useState(5);
  // A session is a *night* of matches, and on a continuous ladder that is dozens of them.
  // Paging the tape by session was only half the job: five sessions rendered whole is
  // still hundreds of rows, which is the same as not being able to read any of them. Each
  // session shows its most recent matches and expands on request.
  const [expandedTape, setExpandedTape] = useState<Set<number>>(new Set());
  // Same for the session in progress, which grows all night.
  const [liveShown, setLiveShown] = useState(BOUTS_PER_SESSION);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);

  const active = isRunning(status);

  const loadStatus = useCallback(async () => {
    try {
      const s = await api.duelStatus();
      setStatus(s.status ? s : null);
    } catch {
      /* transient */
    }
  }, []);

  const loadAll = useCallback(() => {
    // Fired independently rather than awaited together: the controls and the live status
    // come back in milliseconds while the standings and the tape take longer, and
    // Promise.all made the whole page wait for the slowest of them — showing an empty
    // scaffold that read as "no data" until everything landed. It also meant one failing
    // endpoint blanked the entire page; now each section fails on its own.
    const report = (e: unknown) => setError(e instanceof Error ? e.message : String(e));

    void api
      .duelConfig()
      .then((c) => {
        setCfg(c);
        setUntilClock((u) => u ?? clockIn(c.duration_minutes));
      })
      .catch(report);

    void api
      .duelStatus()
      .then((s) => setStatus(s.status ? s : null))
      .catch(() => undefined); // status is a nice-to-have; never block the page on it

    setLoadingStandings(true);
    void api
      .duelStandings()
      .then(setTable)
      .catch(report)
      .finally(() => setLoadingStandings(false));

    void api
      .duelHealth()
      .then(setHealth)
      .catch(() => undefined); // a diagnostic must never be why the page fails

    setLoadingLedger(true);
    void api
      .duelHistory(20)
      .then((h) =>
        setLedger(h.duels.filter((d) => (d.matchups?.length ?? 0) > 0 || d.status === "failed"))
      )
      .catch(report)
      .finally(() => setLoadingLedger(false));
  }, []);

  useEffect(() => {
    void loadAll();
    api
      .crownFollow()
      .then((cf) => {
        setPolicy(cf.config.policy);
        // Which profile the firewall is actually on right now. The standings are a table
        // of records; without this there is nothing on the page saying which of them you
        // are currently running, which is the first thing you want to know.
        setLiveFp(cf.status.last_result?.live_fingerprint ?? null);
      })
      .catch(() => undefined);
  }, [loadAll]);

  // Poll the live stage while a duel is running; refresh the whole view when it ends.
  useEffect(() => {
    if (!active) {
      if (pollRef.current) {
        window.clearInterval(pollRef.current);
        pollRef.current = null;
        void loadAll();
      }
      return;
    }
    pollRef.current = window.setInterval(loadStatus, 3000);
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
      pollRef.current = null;
    };
  }, [active, loadStatus, loadAll]);

  const patch = async (body: Partial<DuelConfig>) => {
    setBusy(true);
    setError(null);
    try {
      setCfg(
        await api.duelConfigSave({
          ...body,
          // Bind the schedule to the zone it was set from (like the baseline test).
          timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
        })
      );
      setToast("Duel settings saved");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  // The line-up costs a full ranking pass, so it's fetched when asked for, not on load.
  const loadCard = async () => {
    setCardBusy(true);
    try {
      setCard(await api.duelCard());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setCardBusy(false);
    }
  };

  const startNow = async (minutes: number) => {
    setBusy(true);
    setError(null);
    setAskDuration(false);
    try {
      setStatus(await api.duelStart(minutes));
      setToast(`Duel started · running for ${fmtWindow(minutes)}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const cancelNow = async () => {
    try {
      await api.duelCancel();
      setToast("Cancelling after the current round — your settings are restored either way");
      await loadStatus();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const rawStandings = table?.standings ?? [];
  const standings = useMemo(
    () => [...rawStandings].sort((a, b) => compareStandings(a, b, orderBy, order)),
    [rawStandings, orderBy, order]
  );
  const pooledRank = useMemo(() => pooledRanking(rawStandings), [rawStandings]);
  const pagedStandings = useMemo(
    () => standings.slice(page * rowsPerPage, page * rowsPerPage + rowsPerPage),
    [standings, page, rowsPerPage]
  );
  // First click on a new column sorts "best first" (descending), except the two columns
  // where ascending IS best: the ladder rank and the alphabetical name.
  const handleSort = (key: StandingSortKey) => {
    setPage(0); // re-sorting should land you on the first page
    if (orderBy === key) {
      setOrder((o) => (o === "asc" ? "desc" : "asc"));
      return;
    }
    setOrderBy(key);
    setOrder(key === "rank" || key === "name" ? "asc" : "desc");
  };
  const champion = table?.champion ?? null;

  // The head-to-head grid only makes sense for profiles that have actually met, so it's
  // capped at the top of the table (a full N×N over every profile ever dueled is mostly
  // empty cells).
  const gridRows = useMemo(() => standings.slice(0, 8), [standings]);
  const hasGrid = gridRows.length >= 2 && !!table?.head_to_head;

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
          <Typography variant="h5">Dueling Champions</Typography>
          <Typography variant="body2" color="text.secondary">
            Head-to-head adjudication, with one job: <b>keep attacking the best profile we
            have</b>. The ring's current #1 defends every match, against whichever profile the
            ledger says is most likely to beat <i>it</i> — re-decided before each match, so a
            profile that wins takes the belt and defends next. Both sides trade equal-length
            runs A/B/B/A, so they meet the same weather, and a sequential test ends each match
            the moment it's decided. Ranked by what a profile <b>beat</b> — not by what it
            averaged.
          </Typography>
        </Box>
        <Stack direction="row" spacing={1}>
          {active ? (
            <Button color="warning" variant="outlined" startIcon={<StopIcon />} onClick={cancelNow}>
              Cancel duel
            </Button>
          ) : (
            <Tooltip title="Choose how long this session should run.">
              <Button
                variant="contained"
                startIcon={<PlayArrowIcon />}
                onClick={() => {
                  // Seed with the nightly window's length, but ASK. The button used to
                  // inherit that length silently and start immediately, so it read as an
                  // arbitrary "5h 59m" — the nightly window is an agreement about when the
                  // ladder may run unattended, which is a different decision from how long
                  // you want it to run right now.
                  setDialogMinutes(cfg?.duration_minutes ?? 120);
                  setAskDuration(true);
                }}
                disabled={busy}
                sx={{ whiteSpace: "nowrap", flexShrink: 0 }}
              >
                Duel now
              </Button>
            </Tooltip>
          )}
        </Stack>
      </Stack>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {/* Is the ladder actually measuring anything? A duel spends two benchmark runs per
          round, and a round with no Overall on either side is discarded — three in a row
          abort the match. That used to be recorded as a draw, so a ladder burning its
          nights on unusable rounds was indistinguishable from a field of evenly matched
          profiles. Shown only when it is actually happening. */}
      {health && health.aborted > 0 && (health.aborted_share ?? 0) >= 0.1 && (
        <Alert severity="warning" sx={{ mb: 2 }}>
          <b>
            {Math.round((health.aborted_share ?? 0) * 100)}% of matches produced no result
          </b>{" "}
          ({health.aborted} of {health.matches} across {health.sessions_analyzed} sessions
          {health.unusable_rounds > 0
            ? `, ${health.unusable_rounds} round${health.unusable_rounds === 1 ? "" : "s"} discarded`
            : ""}
          ). These are not draws — the ladder could not measure them, so nothing was
          adjudicated and the pairs stay eligible to race again.
          {health.reasons.length > 0 && (
            <Box component="ul" sx={{ m: 0, mt: 1, pl: 2.5 }}>
              {health.reasons.slice(0, 4).map((r) => (
                <li key={r.reason}>
                  <Typography variant="caption">
                    {r.reason} — {r.legs} run{r.legs === 1 ? "" : "s"}
                  </Typography>
                </li>
              ))}
            </Box>
          )}
          {health.reasons.length === 0 && (
            <Typography variant="caption" display="block" sx={{ mt: 0.5 }}>
              No causes recorded yet — matches fought before the diagnosis existed count
              here but can't explain themselves. The next session will say why.
            </Typography>
          )}
        </Alert>
      )}

      {cfg && !active && (
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          <b>Press "Duel now"</b> and it runs until{" "}
          {formatClock(untilClock ?? clockIn(cfg.duration_minutes))} (about{" "}
          {fmtWindow(minutesUntil(untilClock ?? clockIn(cfg.duration_minutes)))}), trading
          {cfg.iterations_per_round ?? 3} iteration(s) a side. The champion defends against the top {cfg.contender_top_n}{" "}
          {cfg.contenders === "leaders" ? "profiles nearest the crown" : "heirs"}, one at a time;
          a match ends after {cfg.decision?.streak_pairs ?? "—"} straight wins or a clear run of
          margins, then the next challenger steps up. As many matches as fit in the window.
          {cfg.crown_rule !== "rating_floor" && (
            <>
              {" "}
              <b>The title is lineal</b>: you take the belt by beating the profile that holds
              it — provided your whole shared record with it then favours you on both matches
              and rounds, so one good night against a profile that has beaten you before is a
              win rather than a title.
            </>
          )}
        </Typography>
      )}

      {/* ── Settings, right under the button that uses them ──────────────────────── */}
      <Accordion
        expanded={showRules}
        onChange={(_, open) => setShowRules(open)}
        disableGutters
        sx={{ mb: 2, "&:before": { display: "none" }, borderRadius: 1, overflow: "hidden" }}
      >
        <AccordionSummary expandIcon={<ExpandMoreIcon />}>
          <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
            <GavelIcon fontSize="small" color="action" />
            <Typography variant="subtitle2">Rules of the ring</Typography>
            {/* Say the hierarchy outright. "Bout" and "pair" were two words for two nested
                things and neither said which contained the other; a session holds matches,
                a match is decided over rounds, and a round is the atomic unit of evidence. */}
            <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 0.5 }}>
              A <b>session</b> is a night of <b>matches</b>. A <b>match</b> is two profiles
              head to head, decided over <b>rounds</b> — and one round is a single
              interleaved pair of runs, one on each profile, back to back so they share
              their conditions.
            </Typography>
            {/* The summary states the settings themselves, so the common case — checking
                what's set — needs no click at all. */}
            <Typography variant="caption" color="text.secondary">
              {cfg
                ? `${PRESET_NAME[cfg.preset] ?? cfg.preset} · ${
                    cfg.decision?.streak_pairs ?? "—"
                  } rounds in a row ends it · ${
                    cfg.continuous
                      ? `running continuously (${fmtWindow(cfg.duration_minutes)} sessions)`
                      : cfg.enabled
                        ? `nightly ${hhmm(cfg.hour, cfg.minute)}–${hhmm(cfg.end_hour, cfg.end_minute)}`
                        : "nightly schedule off"
                  }`
                : "loading…"}
            </Typography>
          </Stack>
        </AccordionSummary>
        <AccordionDetails sx={{ pt: 0 }}>
          <Box>
          <Typography variant="caption" color="text.secondary">
            When duels happen and how a match is called. Every setting here is a judgement
            about evidence: how long to keep fighting, how much proof to demand, and how
            big a difference has to be before it counts as a win.
          </Typography>

          {/* ── When ───────────────────────────────────────────────────────────────── */}
          <Divider sx={{ my: 2 }} />
          <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1.5 }}>
            <ScheduleIcon fontSize="small" color="action" />
            <Typography variant="subtitle2">Nightly window</Typography>
            <FormControlLabel
              sx={{ ml: 0.5 }}
              control={
                <Switch
                  size="small"
                  checked={cfg?.enabled ?? false}
                  disabled={!cfg || busy}
                  onChange={(e) => void patch({ enabled: e.target.checked })}
                />
              }
              label={
                <Typography variant="body2" color="text.secondary">
                  {cfg?.enabled ? "Armed" : "Off"}
                  {cfg?.timezone ? ` · ${cfg.timezone}` : ""}
                </Typography>
              }
            />
            <Tooltip title="Keep duelling around the clock instead of once a night — the standings keep accruing evidence, so a better profile can surface at any hour. Sessions still take turns with your other benchmarks and always restore your settings.">
              <FormControlLabel
                control={
                  <Switch
                    size="small"
                    checked={cfg?.continuous ?? false}
                    disabled={!cfg || busy}
                    onChange={(e) => void patch({ continuous: e.target.checked })}
                  />
                }
                label={
                  <Typography variant="body2" color="text.secondary">
                    Run continuously
                  </Typography>
                }
              />
            </Tooltip>
          </Stack>
          {cfg?.continuous && (
            <Typography variant="caption" color="info.main" sx={{ display: "block", mb: 1 }}>
              Running continuously — the times below are ignored until you switch it off.
              Each session lasts {fmtWindow(cfg.duration_minutes)}, with a{" "}
              {cfg.continuous_gap_minutes}-minute pause between them for your other runs.
            </Typography>
          )}
          <Box sx={FIELD_GRID}>
            <TimeField
              label="Start"
              value={cfg ? hhmm(cfg.hour, cfg.minute) : "03:00"}
              disabled={!cfg || busy}
              helper="Duelling begins at this time, in your timezone."
              onChange={(v) => {
                const [h, m] = v.split(":").map((x) => parseInt(x, 10));
                if (!cfg || !Number.isFinite(h) || !Number.isFinite(m)) return;
                // Send both ends together: the backend derives the length from the round,
                // so moving the start keeps the finish time you chose.
                void patch({ hour: h, minute: m, end_hour: cfg.end_hour, end_minute: cfg.end_minute });
              }}
            />
            <TimeField
              label="Finish"
              value={cfg ? hhmm(cfg.end_hour, cfg.end_minute) : "05:00"}
              disabled={!cfg || busy}
              helper={
                cfg
                  ? `Stops here — ${fmtWindow(cfg.duration_minutes)} of duelling${
                      crossesMidnight(cfg) ? ", ending the next morning" : ""
                    }.`
                  : "Duelling stops at this time."
              }
              onChange={(v) => {
                const [h, m] = v.split(":").map((x) => parseInt(x, 10));
                if (!cfg || !Number.isFinite(h) || !Number.isFinite(m)) return;
                void patch({ hour: cfg.hour, minute: cfg.minute, end_hour: h, end_minute: m });
              }}
            />
            <TimeField
              label="Duel now until"
              value={untilClock ?? clockIn(cfg?.duration_minutes ?? 120)}
              disabled={busy || active}
              helper={describeUntil(untilClock ?? clockIn(cfg?.duration_minutes ?? 120))}
              onChange={setUntilClock}
            />
          </Box>

          {/* ── Who wins ───────────────────────────────────────────────────────────── */}
          <Divider sx={{ my: 2 }} />
          <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 0.5 }}>
            <GavelIcon fontSize="small" color="action" />
            <Typography variant="subtitle2">How sure before calling a winner</Typography>
          </Stack>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.5 }}>
            One choice. Win enough rounds back to back and the match ends there; win most of
            them convincingly and it ends too. Shorter streaks decide sooner and get it wrong
            more often — worth it on a ladder that runs every night, since the next match
            corrects it and the standings are what you read.
          </Typography>
          <Box sx={PRESET_GRID}>
            {(cfg?.presets ?? []).map((preset) => {
              const selected = cfg?.preset === preset.key;
              return (
                <Box
                  key={preset.key}
                  onClick={() => !busy && void patch({ preset: preset.key })}
                  sx={{
                    p: 1.5,
                    borderRadius: 2,
                    border: 2,
                    borderColor: selected ? "primary.main" : "divider",
                    bgcolor: selected ? "action.selected" : "transparent",
                    cursor: busy ? "default" : "pointer",
                    "&:hover": { borderColor: busy ? undefined : "primary.light" },
                  }}
                >
                  <Typography variant="subtitle2">{preset.label}</Typography>
                  <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
                    {preset.summary}
                  </Typography>
                  <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
                    {preset.detail}
                  </Typography>
                </Box>
              );
            })}
          </Box>
          {cfg?.preset === "custom" && (
            <Typography variant="caption" color="warning.main" sx={{ display: "block", mt: 1 }}>
              Custom settings (min {cfg.min_pairs} · max {cfg.max_pairs} rounds · error rate{" "}
              {cfg.alpha}) — pick an option above to go back to a standard one.
            </Typography>
          )}

          {/* Everything below is the same question expressed in its parts. Kept for
              anyone who wants it, out of the way of everyone who doesn't. */}
          <Button
            size="small"
            onClick={() => setShowAdvanced((v) => !v)}
            sx={{ mt: 1.5, textTransform: "none" }}
          >
            {showAdvanced ? "Hide" : "Show"} advanced settings
          </Button>
          <Collapse in={showAdvanced}>
            <Box sx={{ ...FIELD_GRID, mt: 1.5 }}>
              <NumField
                label="Min rounds"
                value={cfg?.min_pairs ?? 8}
                disabled={!cfg || busy}
                min={2}
                onCommit={(v) => void patch({ min_pairs: Math.round(v) })}
                helper="Fewest head-to-heads before anyone can be declared the winner."
              />
              <NumField
                label="Max rounds"
                value={cfg?.max_pairs ?? 30}
                disabled={!cfg || busy}
                min={2}
                onCommit={(v) => void patch({ max_pairs: Math.round(v) })}
                helper="Give up after this many and call it a draw, so one matchup can't eat the night."
              />
              <NumField
                label="Error rate"
                value={cfg?.alpha ?? 0.05}
                disabled={!cfg || busy}
                step={0.01}
                onCommit={(v) => void patch({ alpha: Math.min(0.49, Math.max(0.001, v)) })}
                helper="How often you'll accept a wrong verdict (0.05 = 1 in 20)."
              />
              <TextField
                select
                size="small"
                label="How the champion is decided"
                value={cfg?.crown_rule ?? "lineal"}
                disabled={!cfg || busy}
                onChange={(e) => void patch({ crown_rule: e.target.value as DuelConfig["crown_rule"] })}
                helperText={
                  (cfg?.crown_rule ?? "lineal") === "rating_floor"
                    ? "The ring's #1 by proven rating. Honest about evidence, but the title rarely changes hands: the holder defends every match, so a challenger struggles to build the second opponent its error bar needs."
                    : "Beat the holder and lead its whole shared record on both matches and rounds. This decides who wears the belt; the standings order separately, on the fitted rating."
                }
              >
                <MenuItem value="lineal">Lineal title (beat the holder)</MenuItem>
                <MenuItem value="rating_floor">Ring's #1 (proven rating)</MenuItem>
              </TextField>
              <NumField
                label="Contenders to race"
                value={cfg?.contender_top_n ?? 8}
                disabled={!cfg || busy}
                min={1}
                onCommit={(v) => void patch({ contender_top_n: Math.round(v) })}
                helper="How many of the top profiles the champion defends against, closest first. The rest still get a turn afterwards."
              />
              <NumField
                label="Pause between sessions"
                value={cfg?.continuous_gap_minutes ?? 5}
                disabled={!cfg || busy}
                onCommit={(v) => void patch({ continuous_gap_minutes: v })}
                helper="Minutes to leave the pipeline free between continuous sessions, for monitoring and manual runs."
              />
              <NumField
                label="Wins in a row that end it"
                value={cfg?.streak_wins ?? 0}
                disabled={!cfg || busy}
                onCommit={(v) => void patch({ streak_wins: Math.round(v) })}
                helper="0 = work it out from the error rate. Set it (min 2) to call a match the moment one side takes that many rounds back to back."
              />
              <NumField
                label="Ignore wins smaller than"
                value={cfg?.min_margin ?? 0}
                disabled={!cfg || busy}
                step={0.5}
                onCommit={(v) => void patch({ min_margin: v })}
                helper="Overall points; 0 = a consistent win counts however small (matching the crown). Raise it to record hair-thin wins as draws instead."
              />
              <NumField
                label="Rematch after (hours)"
                value={cfg?.rematch_hours ?? 6}
                disabled={!cfg || busy}
                onCommit={(v) => void patch({ rematch_hours: v })}
                helper="Hours before the same two profiles can fight again. Short on purpose: the cooldown exists so a settled question doesn't eat the window, not to retire a pairing — and the leaders are fought first, so a long one empties the ring of exactly the profiles you care about. A match that produced no result never counts. It orders the queue rather than skipping: a cooled contender is raced last among its equals."
              />
              <NumField
                label="Champion goes stale after (days)"
                value={cfg?.champion_freshness_days ?? 7}
                disabled={!cfg || busy}
                onCommit={(v) => void patch({ champion_freshness_days: v })}
                helper="How old the duel champion may get before the crowning policy stops acting on it and falls back to the pooled crown. A separate question from the rematch cooldown, which it used to share a setting with."
              />
              <NumField
                label="Iterations per leg of a round"
                value={cfg?.iterations_per_round ?? 3}
                disabled={!cfg || busy}
                min={1}
                onCommit={(v) => void patch({ iterations_per_round: Math.round(v) })}
                helper="The ring's resolving power, and the only setting here that changes what a duel can SEE. A round compares two single measurements, so it carries the noise of both — measured on a real link, ~2.3 points per run becomes ~3.3 per round, against true edges between top profiles of 0.17-0.30 points. Medianing k iterations divides that by √k: at a 0.3-point edge, rounds needed for a confident call fall from 468 (k=1) to 156 (k=3) to 94 (k=5). A round costs k times as long, so this is roughly break-even on wall clock and a large win on verdicts actually reached."
              />
              <NumField
                label="Rating prior (virtual rounds)"
                value={cfg?.rating_prior_pairs ?? 4}
                disabled={!cfg || busy}
                onCommit={(v) => void patch({ rating_prior_pairs: v })}
                helper="Virtual rounds against an average opponent, added to every record before fitting. This is the lever for thin records topping the table: raising it pulls a one-match record toward the field instead of letting an error bar overturn a result. Measured against a 3-0 snap vs a deep winning record — 4: snap rates 1696 vs 1581; 16: 1569 vs 1584."
              />
              <NumField
                label="Settle before measuring"
                value={cfg?.settle_seconds ?? 3}
                disabled={!cfg || busy}
                min={0}
                onCommit={(v) => void patch({ settle_seconds: Math.round(v) })}
                helper="Seconds to let the link settle after each profile is written to the firewall, before its run is measured. Both sides wait equally, so this never favours anyone — it keeps queue-rebuild noise out of the rounds. 0 = measure immediately."
              />
              {cfg?.method === "pair_wins" && (
                <NumField
                  label="Edge to detect"
                  value={cfg?.p1 ?? 0.7}
                  disabled={!cfg || busy}
                  min={0.51}
                  step={0.05}
                  onCommit={(v) => void patch({ p1: Math.min(0.99, v) })}
                  helper="Round-wins rule only: how lopsided a win to look for (0.7 = wins 70% of rounds)."
                />
              )}
            </Box>
            <Stack direction="row" spacing={1} alignItems="center" sx={{ mt: 1.5 }} flexWrap="wrap" useFlexGap>
              <Typography variant="caption" color="text.secondary">
                Judge matches by:
              </Typography>
              <Tooltip title="Judge by HOW MUCH each round was won (signed-rank on the margins). Against a true 1-point edge this calls the winner about 2.4x as often as counting round wins.">
                <Chip
                  size="small"
                  label="By margin"
                  color={cfg?.method === "margins" ? "primary" : "default"}
                  variant={cfg?.method === "margins" ? "filled" : "outlined"}
                  onClick={() => void patch({ method: "margins" })}
                  disabled={!cfg || busy}
                />
              </Tooltip>
              <Tooltip title="Judge by WHO won each round, ignoring the size of the margin (a sign test). Distribution-free, but it discards most of the evidence.">
                <Chip
                  size="small"
                  label="By round wins"
                  color={cfg?.method === "pair_wins" ? "primary" : "default"}
                  variant={cfg?.method === "pair_wins" ? "filled" : "outlined"}
                  onClick={() => void patch({ method: "pair_wins" })}
                  disabled={!cfg || busy}
                />
              </Tooltip>
            </Stack>
            {cfg && <DecisionCost cfg={cfg} />}
          </Collapse>

          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 2 }}>
            Duel runs join the pooled record like any other runs; duel <b>verdicts</b> live
            only here. The engine never writes a winner to the firewall — it always restores
            your pre-duel settings.
          </Typography>
          </Box>
        </AccordionDetails>
      </Accordion>

      {/* ── The belt ─────────────────────────────────────────────────────────────── */}
      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Stack
            direction={{ xs: "column", sm: "row" }}
            spacing={2}
            alignItems={{ xs: "flex-start", sm: "center" }}
            justifyContent="space-between"
          >
            <Stack direction="row" spacing={1.5} alignItems="center">
              <MilitaryTechIcon color={champion ? "warning" : "disabled"} sx={{ fontSize: 40 }} />
              <Box>
                <Typography variant="overline" color="text.secondary">
                  Reigning duel champion
                </Typography>
                {champion ? (
                  <>
                    <Typography variant="h6" sx={{ lineHeight: 1.2 }}>
                      <Link
                        component="button"
                        underline="hover"
                        onClick={() =>
                          navigate(`/profiles/${encodeURIComponent(champion.fingerprint)}`)
                        }
                        sx={{ font: "inherit" }}
                      >
                        {champion.name || champion.label || champion.fingerprint}
                      </Link>
                    </Typography>
                    <Typography variant="caption" color="text.secondary">
                      {/* Under the lineal rule the belt is a record of results, so say how
                          it was won and how often it has been kept — a champion that has
                          defended eleven times and one that took the title in its first
                          match are both "champion", and that difference is the whole
                          story. */}
                      {champion.rule === "rating_floor"
                        ? "Top of the ladder"
                        : (champion.defences ?? 0) > 0
                          ? `Held the title through ${champion.defences} defence${
                              champion.defences === 1 ? "" : "s"
                            }`
                          : "Took the title in its most recent match"}
                      {champion.consecutive_sessions > 0
                        ? ` · ${champion.consecutive_sessions} consecutive session${
                            champion.consecutive_sessions === 1 ? "" : "s"
                          }`
                        : ""}
                      {champion.duel_id != null ? ` · duel #${champion.duel_id}` : ""}
                      {champion.finished_at ? ` · ${fmtDateTime(champion.finished_at)}` : ""}
                      {champion.decisive ? "" : " · draws only, nothing proven"}
                      {champion.provisional ? " · provisional record" : ""}
                    </Typography>
                    {/* The two verdicts, one line apart. The belt says who beat whom; the
                        ranking says what a record has demonstrated. They are allowed to
                        disagree, and when they do that IS the finding — so the card states
                        the disagreement rather than leaving the reader to spot it. */}
                    {champion.rule !== "rating_floor" && champion.rank != null && (
                      <Typography variant="caption" color="text.secondary" display="block">
                        {champion.rank === 1
                          ? "Also #1 on proven rating — both verdicts agree."
                          : `Ranked #${champion.rank} on proven rating: it beat the holder, but ` +
                            `other records have demonstrated more across the field.`}
                      </Typography>
                    )}
                  </>
                ) : loadingStandings ? (
                  <Typography variant="body2" color="text.secondary">
                    Loading the ladder…
                  </Typography>
                ) : (
                  <Typography variant="body2" color="text.secondary">
                    No duel has crowned one yet — run a duel to start the ladder.
                  </Typography>
                )}
              </Box>
            </Stack>
            <Stack spacing={0.5} alignItems={{ xs: "flex-start", sm: "flex-end" }}>
              <Chip
                size="small"
                color={policy === "duel" ? "warning" : "default"}
                variant={policy === "duel" ? "filled" : "outlined"}
                label={
                  policy === "duel"
                    ? "Crowning policy: duel champion"
                    : "Crowning policy: pooled crown"
                }
              />
              <Typography variant="caption" color="text.secondary">
                {policy === "duel"
                  ? "Follow best acts on these verdicts."
                  : "These verdicts are recorded only — switch the policy in the top-bar Follow best popover to act on them."}
              </Typography>
              {table && (
                <Typography variant="caption" color="text.secondary">
                  {table.matchups_analyzed} match{table.matchups_analyzed === 1 ? "" : "s"} (
                  {table.decisive_matchups} decisive) across {table.sessions_analyzed} session
                  {table.sessions_analyzed === 1 ? "" : "s"}
                </Typography>
              )}
            </Stack>
          </Stack>

          {active && (
            <Box sx={{ mt: 2 }}>
              <LinearProgress />
              {/* The scoreboard when a match is actually under way; the stage sentence is
                  the fallback for the moments between matches (applying a profile, ranking
                  the field, restoring settings) where there is no score to show. */}
              {status?.live ? (
                <MatchScoreboard live={status.live} />
              ) : (
                <Typography variant="body2" sx={{ mt: 0.5 }}>
                  {status?.stage || "starting…"}
                </Typography>
              )}
              <Typography variant="caption" color="text.secondary">
                {status?.iterations_run ?? 0} iteration(s) run ·{" "}
                {status?.matchups?.length ?? 0} verdict(s) so far · your pre-duel settings are
                restored when the window closes
              </Typography>
              {/* The belt changes hands mid-session — it is the ring's #1, re-read from the
                  ledger between matches — so show who holds it right now rather than waiting
                  for the session to end. */}
              {status?.champion_label && (
                <Typography variant="body2" sx={{ mt: 0.5 }}>
                  In the ring with the belt: <b>{status.champion_label}</b>
                  <Typography component="span" variant="caption" color="text.secondary">
                    {" "}
                    — provisional until the session ends; automation still acts only on
                    finished sessions.
                  </Typography>
                </Typography>
              )}
              {/* How the belt got where it is. The defender is whoever the ledger ranks #1
                  at that moment, so by match six the name holding it can be neither the
                  profile that walked in with it nor the pooled crown — which reads as
                  "random profiles" unless you can see the chain that got them there. */}
              {!!status?.matchups?.length && (
                <Box sx={{ mt: 1 }}>
                  <Typography variant="caption" color="text.secondary">
                    This session so far:
                  </Typography>
                  {status.matchups.length > liveShown && (
                    <Button
                      size="small"
                      sx={{ display: "block", mb: 0.5 }}
                      onClick={() => setLiveShown((n) => n + 20)}
                    >
                      Show earlier matches ({status.matchups.length - liveShown} more)
                    </Button>
                  )}
                  {status.matchups.slice(-liveShown).map((m, sliceIdx) => {
                    // The list is windowed to the most recent matches, so the lookahead
                    // below has to index the FULL array — reading `sliceIdx + 1` would look
                    // up the wrong match (and read the belt off it) as soon as anything was
                    // hidden.
                    const i = status.matchups!.length - Math.min(liveShown, status.matchups!.length) + sliceIdx;
                    // Winning a match is NOT the same as taking the belt: under the lineal
                    // rule the belt moves only when the winner leads the holder on their
                    // whole shared record, so beating the champion once when it has beaten
                    // you once leaves the title where it is. Saying "takes the belt" on
                    // every challenger win is how the tape ended up contradicting the
                    // standings. Read the actual belt from who defends the NEXT match (or,
                    // for the newest match, from the session's current holder).
                    const next = status.matchups?.[i + 1];
                    const beltAfter = next ? next.incumbent : status.champion_fingerprint;
                    const beltAfterName = next
                      ? next.incumbent_name || next.incumbent_label
                      : status.champion_label;
                    const winner =
                      m.verdict === "challenger"
                        ? m.challenger_name || m.challenger_label
                        : m.verdict === "incumbent"
                          ? m.incumbent_name || m.incumbent_label
                          : null;
                    // Three outcomes, not two. Asking only "did the old holder keep it?"
                    // credits the belt to whoever won this match whenever the defender
                    // changes — but the defender is re-read before every match, so it can
                    // pass to a profile that wasn't in this match at all (an unreachable
                    // champion, for one, is stood in for). That reads as random unless it
                    // is said out loud.
                    let beltNote = "";
                    if (beltAfter && beltAfter === m.challenger) {
                      beltNote = " · and takes the belt";
                    } else if (beltAfter && beltAfter !== m.incumbent && beltAfterName) {
                      beltNote = ` · ${beltAfterName} defends next`;
                    } else if (beltAfter && m.verdict === "challenger") {
                      beltNote =
                        " · belt stays — it doesn't lead the champion on their whole record yet";
                    }
                    return (
                      <Typography
                        key={i}
                        variant="caption"
                        color="text.secondary"
                        sx={{ display: "block", fontFamily: "monospace" }}
                      >
                        {i + 1}. {m.incumbent_name || m.incumbent_label} vs{" "}
                        {m.challenger_name || m.challenger_label}
                        {m.challenger_why ? ` (${m.challenger_why})` : ""} →{" "}
                        {winner ? `${winner} wins the match` : "draw"}
                        {beltNote}
                      </Typography>
                    );
                  })}
                </Box>
              )}
            </Box>
          )}
          {!active && status?.status === "failed" && status.error && (
            <Alert severity="warning" sx={{ mt: 2 }}>
              Last duel failed: {status.error}
            </Alert>
          )}
        </CardContent>
      </Card>

      {/* ── The ladder ───────────────────────────────────────────────────────────── */}
      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Typography variant="h6">Ladder standings</Typography>
          <Typography variant="caption" color="text.secondary">
            <b>Rating</b> is a strength fitted to every round ever fought, so <i>who</i> you
            beat is what moves you — beating the profile at the top is worth far more than
            beating the one at the bottom, and losing to the best costs little. 1500 is the
            middle of the field and ± is the error bar.{" "}
            <b>Duel rank</b> is the ring standing and the default order here, and it sorts on
            the <b>Rating</b>: whoever wins the duel wins the duel. It used to sort on{" "}
            <b>Proven</b> instead, which could rank a profile <i>below one it had beaten</i> —
            a challenger on 1687 ±146 (proven 1541) sat under the leader it beat on 1563 ±17
            (proven 1546), five points of floor across error bars eight times wider than the
            gap. A ladder built to adjudicate head to head should not rank the loser above the
            winner.
            The cost is that a thin record can lead: it keeps its amber tag (<i>1 opponent</i>,{" "}
            <i>8 rounds</i>) so you can see it, and the lever for it is the rating prior in the
            advanced settings, which pulls a thin record toward the field rather than letting an
            error bar overturn a result. <b>Proven</b> stays as its own sortable column for the
            other question — what a record has <i>demonstrated</i> rather than what it won.
            Points and win rate are the plain ledger, kept beside it. <b>Overall</b> and{" "}
            <b>Overall rank</b> are the pooled all-history score for the same profile, so the
            two verdicts sit side by side — the ▲▼ next to a rank is how far they disagree.
            Click any column to re-sort.
          </Typography>
          {loadingStandings && standings.length === 0 ? (
            <Box sx={{ mt: 1.5 }}>
              <LinearProgress />
              <Typography variant="caption" color="text.secondary">
                Reading the ledger…
              </Typography>
            </Box>
          ) : standings.length === 0 ? (
            <Alert severity="info" sx={{ mt: 1.5 }}>
              No matches on the ledger yet. A duel needs a confident pooled crown to defend and
              at least one reachable heir to challenge it.
            </Alert>
          ) : (
            <TableContainer sx={{ mt: 1.5 }}>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <StandingHeader id="rank" label="Duel rank" orderBy={orderBy} order={order} onSort={handleSort} tip="Standing in the ring — the page's default order, by the fitted Rating: whoever wins the duel wins the duel. It used to order on Proven (rating minus an error bar), which could rank a profile below one it had beaten. Nothing pooled goes into it." />
                    <StandingHeader id="name" label="Profile" orderBy={orderBy} order={order} onSort={handleSort} />
                    <StandingHeader id="rating" label="Rating" align="right" orderBy={orderBy} order={order} onSort={handleSort} tip="Strength fitted to every round this profile has ever fought (Bradley–Terry, on the Elo scale — 1500 is the middle of the field, +400 means winning 10 rounds for every 1 lost against an average opponent). Beating a strong profile moves it a lot and beating a weak one barely at all, so who you beat is what changes your standing. ± is the error bar; a thin record is marked provisional. This is the headline number, not the sort — see Proven." />
                    <StandingHeader id="rating_floor" label="Proven" align="right" orderBy={orderBy} order={order} onSort={handleSort} tip="Rating minus one error bar — what the record has DEMONSTRATED rather than what it suggests. A different question from the ranking, kept as its own sortable column: a thin record fits a wide bar and scores low here even when it won its match. Sort by this to ask 'who has proved it?' rather than 'who won?'" />
                    <StandingHeader id="wins" label="W–L–D" align="right" orderBy={orderBy} order={order} onSort={handleSort} tip="Match record across every duel session: wins–losses–draws. Sorts by wins." />
                    <StandingHeader id="points" label="Pts" align="right" orderBy={orderBy} order={order} onSort={handleSort} tip="Match points: 3 for a win, 1 for a draw." />
                    <StandingHeader id="win_rate" label="Win rate" align="right" orderBy={orderBy} order={order} onSort={handleSort} tip="Share of DECIDED matchups won (draws excluded)." />
                    <StandingHeader id="pair_win_rate" label="Rounds" align="right" orderBy={orderBy} order={order} onSort={handleSort} tip="Individual interleaved A/B rounds won — the raw evidence under the verdicts. Sorts by round-win rate." />
                    <StandingHeader id="median_margin" label="Margin" align="right" orderBy={orderBy} order={order} onSort={handleSort} tip="Median Overall-point gap in this profile's own favour, across its matches." />
                    <StandingHeader id="overall" label="Overall" align="right" orderBy={orderBy} order={order} onSort={handleSort} tip="The POOLED all-history score — what this profile measured across every run, not just its matches. Sort by it to see where the ring and the raw record disagree." />
                    <StandingHeader id="pooled_rank" label="Overall rank" align="right" orderBy={orderBy} order={order} onSort={handleSort} tip="Where this profile sits on the pooled all-history score, among the profiles that have duelled. Compare with Duel rank: a big gap means the ring and the raw record disagree about it." />
                    <StandingHeader id="opponents" label="Opponents" align="right" orderBy={orderBy} order={order} onSort={handleSort} tip="Distinct profiles faced." />
                    <StandingHeader id="championships" label="Titles" align="right" orderBy={orderBy} order={order} onSort={handleSort} tip="Sessions ended holding the belt." />
                    <StandingHeader id="last_dueled_at" label="Last match" align="right" orderBy={orderBy} order={order} onSort={handleSort} />
                  </TableRow>
                </TableHead>
                <TableBody>
                  {pagedStandings.map((r: DuelStanding) => (
                    <TableRow
                      key={r.fingerprint}
                      hover
                      sx={{
                        ...(r.is_champion ? { bgcolor: "action.selected" } : null),
                        // A gentle marker for the profile the firewall is on — an accent
                        // rule down the edge rather than a fill, so it reads at a glance
                        // and still composes with the champion's row highlight.
                        ...(r.fingerprint === liveFp
                          ? {
                              boxShadow: (t: Theme) => `inset 3px 0 0 ${t.palette.info.main}`,
                            }
                          : null),
                      }}
                    >
                      <TableCell>
                        <Stack direction="row" spacing={0.75} alignItems="baseline">
                          <Typography variant="body2">{r.rank}</Typography>
                          <RankGap ringRank={r.rank} pooledRank={pooledRank.get(r.fingerprint)} />
                        </Stack>
                      </TableCell>
                      <TableCell sx={{ minWidth: 210 }}>
                        <Stack direction="row" spacing={0.5} alignItems="center">
                          {r.is_champion && (
                            <MilitaryTechIcon color="warning" sx={{ fontSize: 18 }} />
                          )}
                          <Link
                            component="button"
                            underline="hover"
                            onClick={() =>
                              navigate(`/profiles/${encodeURIComponent(r.fingerprint)}`)
                            }
                            sx={{ font: "inherit", textAlign: "left", whiteSpace: "nowrap" }}
                            title={r.label}
                          >
                            {r.name || r.label}
                          </Link>
                          {r.fingerprint === liveFp && (
                            <Chip
                              size="small"
                              label="live"
                              color="info"
                              variant="outlined"
                              title="This profile is currently on the firewall."
                              sx={{ height: 18, "& .MuiChip-label": { px: 0.75, fontSize: 11 } }}
                            />
                          )}
                        </Stack>
                        {r.name && (
                          <Typography
                            variant="caption"
                            color="text.secondary"
                            sx={{ display: "block" }}
                          >
                            {r.label}
                          </Typography>
                        )}
                        {(r.beaten.length > 0 || r.lost_to.length > 0) && (
                          <Typography variant="caption" color="text.secondary">
                            {r.beaten.length > 0 && `beat ${r.beaten.join(", ")}`}
                            {r.beaten.length > 0 && r.lost_to.length > 0 && " · "}
                            {r.lost_to.length > 0 && `lost to ${r.lost_to.join(", ")}`}
                          </Typography>
                        )}
                      </TableCell>
                      <TableCell align="right">
                        <RatingCell row={r} />
                      </TableCell>
                      {/* What the record has demonstrated — and what the rank is built on,
                          so it is printed rather than left implied in a sort order. */}
                      <TableCell align="right" sx={{ fontVariantNumeric: "tabular-nums" }}>
                        {r.rating_floor == null ? "—" : Math.round(r.rating_floor)}
                      </TableCell>
                      <TableCell align="right">
                        {r.wins}–{r.losses}–{r.draws}
                      </TableCell>
                      <TableCell align="right">{r.points}</TableCell>
                      <TableCell align="right">{pct(r.win_rate)}</TableCell>
                      <TableCell align="right">
                        <Tooltip title={`${r.pair_wins} won / ${r.pair_losses} lost`}>
                          <span>
                            {r.pair_wins}–{r.pair_losses}{" "}
                            <Typography component="span" variant="caption" color="text.secondary">
                              ({pct(r.pair_win_rate)})
                            </Typography>
                          </span>
                        </Tooltip>
                      </TableCell>
                      <TableCell align="right" sx={{ color: marginColor(r.median_margin) }}>
                        {fmtMargin(r.median_margin)}
                      </TableCell>
                      <TableCell align="right">
                        <Tooltip
                          title={
                            r.overall == null
                              ? "No comparable runs under the current methodology."
                              : `Pooled across ${r.pooled_iterations ?? 0} iteration(s) of all-history data.`
                          }
                        >
                          <span>{fmtNum(r.overall, 1)}</span>
                        </Tooltip>
                      </TableCell>
                      <TableCell align="right">
                        <Typography variant="body2" color="text.secondary">
                          {pooledRank.get(r.fingerprint) ?? "—"}
                        </Typography>
                      </TableCell>
                      <TableCell align="right">{r.opponents}</TableCell>
                      <TableCell align="right">{r.championships || "—"}</TableCell>
                      <TableCell align="right">
                        <Typography variant="caption" color="text.secondary">
                          {fmtDateTime(r.last_dueled_at)}
                        </Typography>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
              {standings.length > rowsPerPage && (
                <TablePagination
                  component="div"
                  count={standings.length}
                  page={page}
                  onPageChange={(_, p) => setPage(p)}
                  rowsPerPage={rowsPerPage}
                  onRowsPerPageChange={(e) => {
                    setRowsPerPage(parseInt(e.target.value, 10));
                    setPage(0);
                  }}
                  rowsPerPageOptions={[25, 50, 100]}
                />
              )}
            </TableContainer>
          )}
        </CardContent>
      </Card>

      {/* ── Head-to-head grid ────────────────────────────────────────────────────── */}
      {hasGrid && (
        <Card sx={{ mb: 2 }}>
          <CardContent>
            <Typography variant="h6">Head-to-head</Typography>
            <Typography variant="caption" color="text.secondary">
              Each cell is the row profile's record against the column profile (W–L–D).
              Blank = they've never met — the ladder only rounds the holder with the next
              heir, so the grid fills in as reigns change.
            </Typography>
            <TableContainer sx={{ mt: 1.5 }}>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell />
                    {gridRows.map((c) => (
                      <TableCell key={c.fingerprint} align="center">
                        <Tooltip title={c.label}>
                          <Typography variant="caption" noWrap sx={{ maxWidth: 110, display: "block" }}>
                            {c.name || c.label}
                          </Typography>
                        </Tooltip>
                      </TableCell>
                    ))}
                  </TableRow>
                </TableHead>
                <TableBody>
                  {gridRows.map((r) => (
                    <TableRow key={r.fingerprint} hover>
                      <TableCell sx={{ whiteSpace: "nowrap" }}>
                        <Tooltip title={r.label}>
                          <Typography variant="caption">{r.name || r.label}</Typography>
                        </Tooltip>
                      </TableCell>
                      {gridRows.map((c) => {
                        if (c.fingerprint === r.fingerprint)
                          return (
                            <TableCell key={c.fingerprint} align="center" sx={{ bgcolor: "action.hover" }} />
                          );
                        const cell = table?.head_to_head?.[r.fingerprint]?.[c.fingerprint];
                        if (!cell)
                          return (
                            <TableCell key={c.fingerprint} align="center">
                              <Typography variant="caption" color="text.disabled">
                                —
                              </Typography>
                            </TableCell>
                          );
                        const color =
                          cell.wins > cell.losses
                            ? "success.main"
                            : cell.losses > cell.wins
                              ? "error.main"
                              : "text.secondary";
                        return (
                          <TableCell key={c.fingerprint} align="center">
                            <Tooltip
                              title={`${cell.pairs} rounds · median Δ ${fmtMargin(cell.median_margin)} in ${r.name || r.label}'s favour`}
                            >
                              <Typography variant="caption" sx={{ color }}>
                                {cell.wins}–{cell.losses}–{cell.draws}
                              </Typography>
                            </Tooltip>
                          </TableCell>
                        );
                      })}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableContainer>
          </CardContent>
        </Card>
      )}

      {/* ── The card: who fights whom, in order ──────────────────────────────────── */}
      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Stack
            direction={{ xs: "column", sm: "row" }}
            justifyContent="space-between"
            alignItems={{ xs: "flex-start", sm: "center" }}
            spacing={1}
          >
            <Box>
              <Typography variant="h6">Who's fighting</Typography>
              <Typography variant="caption" color="text.secondary">
                The line-up a duel started now would work through, in order — not a random
                draw. The champion is the current pooled crown; challengers are the profiles
                nearest it, strongest first.
              </Typography>
            </Box>
            <Button size="small" variant="outlined" onClick={() => void loadCard()} disabled={cardBusy}>
              {cardBusy ? "Working it out…" : card ? "Refresh" : "Show the line-up"}
            </Button>
          </Stack>

          {card?.reason && (
            <Alert severity="info" sx={{ mt: 1.5 }}>
              {card.reason}
            </Alert>
          )}

          {card?.incumbent && (
            <Box sx={{ mt: 1.5 }}>
              <Typography variant="body2">
                <MilitaryTechIcon sx={{ fontSize: 16, verticalAlign: "text-bottom", color: "warning.main" }} />{" "}
                <b>{card.incumbent.name || card.incumbent.label}</b> defends
                {card.incumbent.overall != null
                  ? ` (Overall ${fmtNum(card.incumbent.overall, 1)})`
                  : ""}
                {" "}as the ring's current #1. Beating it takes the belt, and the next match
                is re-decided from the ledger — so the order below is what a duel starting
                now would run <i>if nothing upsets it</i>, not a fixed card.
              </Typography>
              {card.contenders === "ring" && (
                <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
                  Challengers are ordered by <b>the ring's own findings</b> — each one's
                  optimistic ceiling on its head-to-head rating, so the match most likely to
                  unseat the belt runs first
                  {card.incumbent_rating != null
                    ? ` (the bar to clear is the belt's ${Math.round(card.incumbent_rating)})`
                    : ""}
                  . A profile the ladder has already beaten waits its turn rather than being
                  struck off. The pooled score decides nothing here except the order among
                  profiles that have never been in the ring.
                </Typography>
              )}
              {card.incumbent.why && (
                <Typography variant="caption" color="text.secondary">
                  {card.incumbent.why}.
                </Typography>
              )}
              <TableContainer sx={{ mt: 1 }}>
                <Table size="small">
                  <TableHead>
                    <TableRow>
                      <TableCell>#</TableCell>
                      <TableCell>Challenger</TableCell>
                      <TableCell align="right">Overall</TableCell>
                      <TableCell align="right">Iterations</TableCell>
                      <TableCell>Why it's in the queue</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {card.queue.map((c) => (
                      <TableRow key={c.fingerprint} hover>
                        <TableCell>{c.position}</TableCell>
                        <TableCell>
                          <Link
                            component="button"
                            underline="hover"
                            onClick={() => navigate(`/profiles/${encodeURIComponent(c.fingerprint)}`)}
                            sx={{ font: "inherit", textAlign: "left" }}
                            title={c.label ?? undefined}
                          >
                            {c.name || c.label}
                          </Link>
                        </TableCell>
                        <TableCell align="right">{fmtNum(c.overall, 1)}</TableCell>
                        <TableCell align="right">{c.iterations ?? "—"}</TableCell>
                        <TableCell>
                          <Typography variant="caption" color="text.secondary">
                            {/* Under the "ring" model the queue is ordered by the ladder's
                                own findings, so the reason should be stated in those terms
                                — the pooled label is the fallback for the older modes. */}
                            {c.ring_why || CARD_REASON[c.reason] || c.reason}
                            {c.on_cooldown
                              ? ` · re-raced (settled within the last ${card.rematch_hours ?? 6}h, so it goes last among its equals)`
                              : ""}
                          </Typography>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableContainer>
              {(card.total ?? 0) > card.queue.length && (
                <Typography variant="caption" color="text.secondary">
                  …and {(card.total ?? 0) - card.queue.length} more behind them, if the window lasts.
                </Typography>
              )}
            </Box>
          )}
        </CardContent>
      </Card>

      {/* ── The tape ─────────────────────────────────────────────────────────────── */}
      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Typography variant="h6">Match tape</Typography>
          <Typography variant="caption" color="text.secondary">
            Every matchup, newest session first — the round scoreline, the margin, and what
            ended it (a boundary crossing, mutual futility, the round cap, or the window
            closing). In each bar,{" "}
            <Box component="span" sx={{ color: "success.main" }}>
              green is the holder's round wins
            </Box>{" "}
            and{" "}
            <Box component="span" sx={{ color: "warning.main" }}>
              amber the challenger's
            </Box>
            .
          </Typography>
          {loadingLedger && ledger.length === 0 ? (
            <Box sx={{ mt: 1.5 }}>
              <LinearProgress />
            </Box>
          ) : ledger.length === 0 ? (
            <Alert severity="info" sx={{ mt: 1.5 }}>
              No duel sessions recorded yet.
            </Alert>
          ) : (
            <Stack spacing={1.5} sx={{ mt: 1.5 }}>
              {ledger.slice(0, tapeShown).map((d) => (
                <Box key={d.id}>
                  <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
                    <Typography variant="subtitle2">Duel #{d.id}</Typography>
                    <Chip
                      size="small"
                      variant="outlined"
                      label={d.status ?? "—"}
                      color={
                        d.status === "complete"
                          ? "success"
                          : d.status === "failed"
                            ? "error"
                            : d.status === "cancelled"
                              ? "warning"
                              : "info"
                      }
                    />
                    <Typography variant="caption" color="text.secondary">
                      {d.trigger} · {fmtDateTime(d.finished_at ?? d.started_at)} ·{" "}
                      {d.iterations_run} iteration(s)
                      {d.champion_label ? ` · champion: ${d.champion_label}` : ""}
                    </Typography>
                  </Stack>
                  {d.error && (
                    <Typography variant="caption" color="error.main">
                      {d.error}
                    </Typography>
                  )}
                  <Divider sx={{ my: 0.5 }} />
                  {(() => {
                    const all = d.matchups ?? [];
                    const open = expandedTape.has(d.id);
                    const hidden = open ? 0 : Math.max(0, all.length - BOUTS_PER_SESSION);
                    // The most RECENT matches, not the first: a session's latest bouts are
                    // the ones that decided where the belt ended up.
                    const shown = open ? all : all.slice(hidden);
                    return (
                      <>
                        {hidden > 0 && (
                          <Button
                            size="small"
                            sx={{ mb: 0.5 }}
                            onClick={() =>
                              setExpandedTape((prev) => new Set(prev).add(d.id))
                            }
                          >
                            Show {hidden} earlier match{hidden === 1 ? "" : "es"} in this session
                          </Button>
                        )}
                        {/* The API caps how many matches it sends per session, so say when
                            there are more than the tape can reach — otherwise a session
                            that ran forty matches silently reads as if it ran twenty-five. */}
                        {open && (d.matchups_total ?? all.length) > all.length && (
                          <Typography variant="caption" color="text.secondary" display="block">
                            Showing the {all.length} most recent of{" "}
                            {d.matchups_total} matches in this session.
                          </Typography>
                        )}
                        {shown.map((m, i) => (
                          <BoutRow key={hidden + i} m={m} />
                        ))}
                      </>
                    );
                  })()}
                </Box>
              ))}
            </Stack>
          )}
          {ledger.length > tapeShown && (
            <Button size="small" sx={{ mt: 1 }} onClick={() => setTapeShown((n) => n + 10)}>
              Show earlier sessions ({ledger.length - tapeShown} more)
            </Button>
          )}
        </CardContent>
      </Card>

      {/* How long should this run? Asked, not assumed. */}
      <Dialog open={askDuration} onClose={() => setAskDuration(false)} maxWidth="xs" fullWidth>
        <DialogTitle>Run a duel session for how long?</DialogTitle>
        <DialogContent>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            The champion defends back to back for the whole window, one match at a time, and
            the session stops at the end of the match in progress. Your settings are restored
            either way — a duel adjudicates, it never promotes.
          </Typography>
          <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap sx={{ mb: 2 }}>
            {DUEL_LENGTHS.map((m) => (
              <Chip
                key={m}
                label={fmtWindow(m)}
                onClick={() => setDialogMinutes(m)}
                color={dialogMinutes === m ? "primary" : "default"}
                variant={dialogMinutes === m ? "filled" : "outlined"}
              />
            ))}
          </Stack>
          <TextField
            fullWidth
            size="small"
            type="number"
            label="Minutes"
            value={dialogMinutes}
            onChange={(e) => setDialogMinutes(Math.max(1, Math.round(Number(e.target.value) || 0)))}
            helperText={
              dialogMinutes < 30
                ? "Short windows often close mid-match, which decides nothing."
                : `Finishes about ${formatClock(clockIn(dialogMinutes))}.`
            }
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setAskDuration(false)}>Cancel</Button>
          <Button
            variant="contained"
            startIcon={<PlayArrowIcon />}
            disabled={busy || dialogMinutes < 1}
            onClick={() => void startNow(dialogMinutes)}
          >
            Start · {fmtWindow(dialogMinutes)}
          </Button>
        </DialogActions>
      </Dialog>

      <Snackbar
        open={!!toast}
        autoHideDuration={4000}
        onClose={() => setToast(null)}
        message={toast ?? ""}
      />
    </Box>
  );
}
