// Dueling Champions — the duel ladder's own view.
//
// Everything else in PathBrain ranks profiles *observationally*: pool a profile's whole
// history and compare averages. This page is the other epistemology — the controlled
// trial. Profiles here are ranked by what they BEAT, in interleaved A/B/A/B matchups
// where both sides met the same weather by construction. So the vocabulary is a fight
// card, not a leaderboard of means: a reigning champion with a reign length, a league
// table of W–L–D records, a head-to-head grid, and the bout tape of every verdict with
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
import Divider from "@mui/material/Divider";
import FormControlLabel from "@mui/material/FormControlLabel";
import LinearProgress from "@mui/material/LinearProgress";
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
import type {
  DuelCard,
  DuelConfig,
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
// then pair-win rate); clicking a header re-sorts client-side without refetching.
type StandingSortKey =
  | "rank"
  | "rating"
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

// The duel rating: a Bradley-Terry strength fitted to every pair on the ledger, printed
// on the Elo scale. The error bar is not decoration — a rating built on eight pairs and
// one built on eight hundred are different claims, and a table that prints them
// identically invites the reader to act on the wrong one.
function RatingCell({ row }: { row: DuelStanding }) {
  if (row.rating == null) return <>—</>;
  const se = row.rating_se;
  return (
    <Tooltip
      title={
        `Fitted from ${row.rating_pairs ?? 0} interleaved pair${row.rating_pairs === 1 ? "" : "s"}` +
        (row.expected_pair_wins != null
          ? ` · won ${row.pair_wins} where the fit expected ${row.expected_pair_wins}`
          : "") +
        (row.rating_provisional
          ? " · provisional: too few pairs to separate it from the middle of the field yet"
          : "")
      }
    >
      <span>
        <Typography
          component="span"
          variant="body2"
          sx={{ fontVariantNumeric: "tabular-nums", opacity: row.rating_provisional ? 0.6 : 1 }}
        >
          {Math.round(row.rating)}
        </Typography>
        {se != null && (
          <Typography component="span" variant="caption" color="text.secondary">
            {" "}
            ±{Math.round(se)}
          </Typography>
        )}
        {row.rating_provisional && (
          <Typography component="span" variant="caption" color="text.secondary">
            {" "}
            ?
          </Typography>
        )}
      </span>
    </Tooltip>
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
  if (m.verdict === "draw") return `challenger Δ ${fmtNum(m.median_delta, 2)}`;
  return `won by ${fmtNum(Math.abs(m.median_delta), 2)} Overall pts`;
}

// The verdict of one bout, phrased as a result rather than a raw enum.
function VerdictChip({ m }: { m: DuelMatchup }) {
  if (m.verdict === "draw")
    return <Chip size="small" label="draw" variant="outlined" color="default" />;
  const winner =
    m.verdict === "challenger"
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
// Each pair won moves the sequential test up by ln(p1/0.5); each pair lost moves it down
// by ln((1-p1)/0.5), a BIGGER step. So the pair cap and the evidence bar interact: at
// p1=0.70 / alpha=0.05 with a 15-pair cap, a winner needs 13 of 15 (87%) — a profile
// genuinely winning 80% of its pairs can never be declared the winner, however many
// nights it runs. That's invisible from the numbers, so the card states it outright and
// warns when the cap is set below the rule's reach.
function DecisionCost({ cfg }: { cfg: DuelConfig }) {
  const d = cfg.decision;
  if (!d) return null;
  if (cfg.method === "margins") {
    return (
      <Alert severity="info" icon={false} sx={{ mt: 2, py: 0.5 }}>
        <Typography variant="caption" component="div">
          <b>What it takes to win a bout.</b> {d.streak_pairs ?? "—"} wins in a row ends it
          immediately. Short of that, bouts are judged on the <b>size</b> of each
          pair's margin, not just who won it — so a profile that wins by a consistent
          amount is called even when it drops the odd pair. The fastest possible verdict is{" "}
          {d.sweep_pairs ?? "—"} consistently one-sided pairs. Testing after every pair
          would inflate false alarms, so the threshold is tightened to{" "}
          {d.nominal_alpha?.toFixed(4)} (your {cfg.alpha} error rate ÷ {d.peek_penalty}),
          which holds real false verdicts near {Math.round(cfg.alpha * 100)}%. There is no
          cap at which a winner becomes unreachable — more pairs only ever help.
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
        <b>What it takes to win a bout.</b> The fastest possible verdict is a{" "}
        {d.sweep_pairs}-pair clean sweep.{" "}
        {impossible ? (
          <>
            At your {cfg.max_pairs}-pair cap <b>no result can ever be decisive</b> — every
            bout will be recorded as a draw. Raise the cap above {d.sweep_pairs}, or lower
            the evidence bar.
          </>
        ) : (
          <>
            At your {cfg.max_pairs}-pair cap a winner must take{" "}
            <b>
              {d.wins_needed} of {cfg.max_pairs}
            </b>{" "}
            ({Math.round((d.win_rate_needed ?? 0) * 100)}%). Anything closer is a draw
            {d.restrictive
              ? " — that's a near-sweep, so most bouts will draw. Raise the pair cap (40 gives a winner room at 70%), or lower the evidence bar below."
              : "."}
          </>
        )}
      </Typography>
    </Alert>
  );
}

// One bout on the tape: the two corners, the pair scoreline, and how the sequential
// test ended it. The pair bar is the actual evidence — each segment is one interleaved
// A/B pair that one side won.
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
            {m.wins_incumbent}–{m.wins_challenger} in {m.pairs} pairs · {marginPhrase(m)}
          </Typography>
          <VerdictChip m={m} />
        </Stack>
      </Stack>
      <Tooltip
        title={`Pair wins — holder ${m.wins_incumbent}, challenger ${m.wins_challenger}. LLR ${fmtNum(
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
  // How many sessions of the bout tape to render — the rest are a click away.
  const [tapeShown, setTapeShown] = useState(5);
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
      .then((cf) => setPolicy(cf.config.policy))
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

  const startNow = async () => {
    setBusy(true);
    setError(null);
    try {
      setStatus(await api.duelStart(untilClock ? minutesUntil(untilClock) : undefined));
      setToast("Duel started");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const cancelNow = async () => {
    try {
      await api.duelCancel();
      setToast("Cancelling after the current pair — your settings are restored either way");
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
            Head-to-head adjudication: the crown and its heirs trade one-iteration runs
            A/B/A/B, so both sides meet the same weather. A sequential test ends each bout
            the moment it's decided, the winner stays on, and the next challenger steps up.
            Ranked by what a profile <b>beat</b> — not by what it averaged.
          </Typography>
        </Box>
        <Stack direction="row" spacing={1}>
          {active ? (
            <Button color="warning" variant="outlined" startIcon={<StopIcon />} onClick={cancelNow}>
              Cancel duel
            </Button>
          ) : (
            <Tooltip
              title={
                cfg
                  ? `Runs until ${untilClock ?? clockIn(cfg.duration_minutes)} (about ${fmtWindow(
                      minutesUntil(untilClock ?? clockIn(cfg.duration_minutes))
                    )}), one iteration a side, as many bouts as fit. Change the finish time under "Duel now until".`
                  : "Start a duel now"
              }
            >
              <Button
                variant="contained"
                startIcon={<PlayArrowIcon />}
                onClick={() => void startNow()}
                disabled={busy}
                sx={{ whiteSpace: "nowrap", flexShrink: 0 }}
              >
                Duel now
                {cfg
                  ? ` · ${fmtWindow(minutesUntil(untilClock ?? clockIn(cfg.duration_minutes)))}`
                  : ""}
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

      {cfg && !active && (
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          <b>Press "Duel now"</b> and it runs until{" "}
          {formatClock(untilClock ?? clockIn(cfg.duration_minutes))} (about{" "}
          {fmtWindow(minutesUntil(untilClock ?? clockIn(cfg.duration_minutes)))}), trading one
          iteration a side. The champion defends against the top {cfg.contender_top_n}{" "}
          {cfg.contenders === "leaders" ? "profiles nearest the crown" : "heirs"}, one at a time;
          a bout ends after {cfg.decision?.streak_pairs ?? "—"} straight wins or a clear run of
          margins, then the next challenger steps up. As many bouts as fit in the window.
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
            {/* The summary states the settings themselves, so the common case — checking
                what's set — needs no click at all. */}
            <Typography variant="caption" color="text.secondary">
              {cfg
                ? `${PRESET_NAME[cfg.preset] ?? cfg.preset} · ${
                    cfg.decision?.streak_pairs ?? "—"
                  } in a row ends it · ${
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
            When duels happen and how a bout is called. Every setting here is a judgement
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
                // Send both ends together: the backend derives the length from the pair,
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
            One choice. Win enough pairs back to back and the bout ends there; win most of
            them convincingly and it ends too. Shorter streaks decide sooner and get it wrong
            more often — worth it on a ladder that runs every night, since the next bout
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
              Custom settings (min {cfg.min_pairs} · max {cfg.max_pairs} pairs · error rate{" "}
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
                label="Min pairs"
                value={cfg?.min_pairs ?? 8}
                disabled={!cfg || busy}
                min={2}
                onCommit={(v) => void patch({ min_pairs: Math.round(v) })}
                helper="Fewest head-to-heads before anyone can be declared the winner."
              />
              <NumField
                label="Max pairs"
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
                helper="0 = work it out from the error rate. Set it (min 2) to call a bout the moment one side takes that many pairs back to back."
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
                label="Rematch after"
                value={cfg?.rematch_days ?? 7}
                disabled={!cfg || busy}
                onCommit={(v) => void patch({ rematch_days: Math.round(v) })}
                helper="Days before the same two profiles can fight again. It orders the queue — a cooled contender is raced last among its equals, never skipped in favour of an untested profile."
              />
              <NumField
                label="Settle before measuring"
                value={cfg?.settle_seconds ?? 3}
                disabled={!cfg || busy}
                min={0}
                onCommit={(v) => void patch({ settle_seconds: Math.round(v) })}
                helper="Seconds to let the link settle after each profile is written to the firewall, before its run is measured. Both sides wait equally, so this never favours anyone — it keeps queue-rebuild noise out of the pairs. 0 = measure immediately."
              />
              {cfg?.method === "pair_wins" && (
                <NumField
                  label="Edge to detect"
                  value={cfg?.p1 ?? 0.7}
                  disabled={!cfg || busy}
                  min={0.51}
                  step={0.05}
                  onCommit={(v) => void patch({ p1: Math.min(0.99, v) })}
                  helper="Pair-wins rule only: how lopsided a win to look for (0.7 = wins 70% of pairs)."
                />
              )}
            </Box>
            <Stack direction="row" spacing={1} alignItems="center" sx={{ mt: 1.5 }} flexWrap="wrap" useFlexGap>
              <Typography variant="caption" color="text.secondary">
                Judge bouts by:
              </Typography>
              <Tooltip title="Judge by HOW MUCH each pair was won (signed-rank on the margins). Against a true 1-point edge this calls the winner about 2.4x as often as counting pair wins.">
                <Chip
                  size="small"
                  label="By margin"
                  color={cfg?.method === "margins" ? "primary" : "default"}
                  variant={cfg?.method === "margins" ? "filled" : "outlined"}
                  onClick={() => void patch({ method: "margins" })}
                  disabled={!cfg || busy}
                />
              </Tooltip>
              <Tooltip title="Judge by WHO won each pair, ignoring the size of the margin (a sign test). Distribution-free, but it discards most of the evidence.">
                <Chip
                  size="small"
                  label="By pair wins"
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
                      Held for {champion.consecutive_sessions} consecutive session
                      {champion.consecutive_sessions === 1 ? "" : "s"} · crowned in duel #
                      {champion.duel_id} · {fmtDateTime(champion.finished_at)}
                      {champion.decisive ? "" : " · inherited by draws only"}
                    </Typography>
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
                  {table.matchups_analyzed} bout{table.matchups_analyzed === 1 ? "" : "s"} (
                  {table.decisive_matchups} decisive) across {table.sessions_analyzed} session
                  {table.sessions_analyzed === 1 ? "" : "s"}
                </Typography>
              )}
            </Stack>
          </Stack>

          {active && (
            <Box sx={{ mt: 2 }}>
              <LinearProgress />
              <Typography variant="body2" sx={{ mt: 0.5 }}>
                {status?.stage || "starting…"}
              </Typography>
              <Typography variant="caption" color="text.secondary">
                {status?.iterations_run ?? 0} iteration(s) run ·{" "}
                {status?.matchups?.length ?? 0} verdict(s) so far · your pre-duel settings are
                restored when the window closes
              </Typography>
              {/* The belt changes hands mid-session — the winner stays on — so show who
                  holds it right now rather than waiting for the session to end. */}
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
              {/* How the belt got where it is. The winner stays on, so by bout six the two
                  names in the ring can be neither the profile that walked in with the belt
                  nor the pooled crown — which reads as "random profiles" unless you can see
                  the chain that got them there. */}
              {!!status?.matchups?.length && (
                <Box sx={{ mt: 1 }}>
                  <Typography variant="caption" color="text.secondary">
                    This session so far:
                  </Typography>
                  {status.matchups.map((m, i) => (
                    <Typography
                      key={i}
                      variant="caption"
                      color="text.secondary"
                      sx={{ display: "block", fontFamily: "monospace" }}
                    >
                      {i + 1}. {m.incumbent_name || m.incumbent_label} vs{" "}
                      {m.challenger_name || m.challenger_label}
                      {m.challenger_why ? ` (${m.challenger_why})` : ""} →{" "}
                      {m.verdict === "challenger"
                        ? `${m.challenger_name || m.challenger_label} takes the belt`
                        : m.verdict === "incumbent"
                          ? "belt held"
                          : "draw"}
                    </Typography>
                  ))}
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
            <b>Duel rank</b> is the ring standing and the default order here, by{" "}
            <b>Rating</b>: a strength fitted to every pair ever fought, so <i>who</i> you beat
            is what moves you — beating the profile at the top is worth far more than beating
            the one at the bottom, and losing to the best costs little. 1500 is the middle of
            the field; ± is the error bar and <b>?</b> marks a record too thin to trust yet.
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
              No bouts on the ledger yet. A duel needs a confident pooled crown to defend and
              at least one reachable heir to challenge it.
            </Alert>
          ) : (
            <TableContainer sx={{ mt: 1.5 }}>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <StandingHeader id="rank" label="Duel rank" orderBy={orderBy} order={order} onSort={handleSort} tip="Standing in the ring — the page's default order, by duel rating. Nothing pooled goes into it." />
                    <StandingHeader id="name" label="Profile" orderBy={orderBy} order={order} onSort={handleSort} />
                    <StandingHeader id="rating" label="Rating" align="right" orderBy={orderBy} order={order} onSort={handleSort} tip="Strength fitted to every pair this profile has ever fought (Bradley–Terry, on the Elo scale — 1500 is the middle of the field, +400 means winning 10 pairs for every 1 lost against an average opponent). Beating a strong profile moves it a lot and beating a weak one barely at all, so who you beat is what changes your standing. ± is the error bar; a thin record is marked provisional." />
                    <StandingHeader id="wins" label="W–L–D" align="right" orderBy={orderBy} order={order} onSort={handleSort} tip="Match record across every duel session: wins–losses–draws. Sorts by wins." />
                    <StandingHeader id="points" label="Pts" align="right" orderBy={orderBy} order={order} onSort={handleSort} tip="Match points: 3 for a win, 1 for a draw." />
                    <StandingHeader id="win_rate" label="Win rate" align="right" orderBy={orderBy} order={order} onSort={handleSort} tip="Share of DECIDED matchups won (draws excluded)." />
                    <StandingHeader id="pair_win_rate" label="Pairs" align="right" orderBy={orderBy} order={order} onSort={handleSort} tip="Individual interleaved A/B pairs won — the raw evidence under the verdicts. Sorts by pair-win rate." />
                    <StandingHeader id="median_margin" label="Margin" align="right" orderBy={orderBy} order={order} onSort={handleSort} tip="Median Overall-point gap in this profile's own favour, across its bouts." />
                    <StandingHeader id="overall" label="Overall" align="right" orderBy={orderBy} order={order} onSort={handleSort} tip="The POOLED all-history score — what this profile measured across every run, not just its bouts. Sort by it to see where the ring and the raw record disagree." />
                    <StandingHeader id="pooled_rank" label="Overall rank" align="right" orderBy={orderBy} order={order} onSort={handleSort} tip="Where this profile sits on the pooled all-history score, among the profiles that have duelled. Compare with Duel rank: a big gap means the ring and the raw record disagree about it." />
                    <StandingHeader id="opponents" label="Opponents" align="right" orderBy={orderBy} order={order} onSort={handleSort} tip="Distinct profiles faced." />
                    <StandingHeader id="championships" label="Titles" align="right" orderBy={orderBy} order={order} onSort={handleSort} tip="Sessions ended holding the belt." />
                    <StandingHeader id="last_dueled_at" label="Last bout" align="right" orderBy={orderBy} order={order} onSort={handleSort} />
                  </TableRow>
                </TableHead>
                <TableBody>
                  {pagedStandings.map((r: DuelStanding) => (
                    <TableRow
                      key={r.fingerprint}
                      hover
                      sx={r.is_champion ? { bgcolor: "action.selected" } : undefined}
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
              Blank = they've never met — the ladder only pairs the holder with the next
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
                              title={`${cell.pairs} pairs · median Δ ${fmtMargin(cell.median_margin)} in ${r.name || r.label}'s favour`}
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
                . Whoever wins a bout stays on for the next one.
              </Typography>
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
                            {CARD_REASON[c.reason] ?? c.reason}
                            {c.on_cooldown
                              ? ` · re-raced (settled within ${card.rematch_days ?? 7} days, so it goes last among its equals)`
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
          <Typography variant="h6">Bout tape</Typography>
          <Typography variant="caption" color="text.secondary">
            Every matchup, newest session first — the pair scoreline, the margin, and what
            ended it (a boundary crossing, mutual futility, the pair cap, or the window
            closing). In each bar,{" "}
            <Box component="span" sx={{ color: "success.main" }}>
              green is the holder's pair wins
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
                  {(d.matchups ?? []).map((m, i) => (
                    <BoutRow key={i} m={m} />
                  ))}
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

      <Snackbar
        open={!!toast}
        autoHideDuration={4000}
        onClose={() => setToast(null)}
        message={toast ?? ""}
      />
    </Box>
  );
}
