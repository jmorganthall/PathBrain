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
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
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
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import StopIcon from "@mui/icons-material/Stop";
import MilitaryTechIcon from "@mui/icons-material/MilitaryTech";
import SportsMmaIcon from "@mui/icons-material/SportsMma";

import { api } from "../api/client";
import type {
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

function NumField({
  label,
  value,
  onCommit,
  disabled,
  min = 0,
  step = 1,
  width = 120,
  help,
}: {
  label: string;
  value: number;
  onCommit: (v: number) => void;
  disabled?: boolean;
  min?: number;
  step?: number;
  width?: number;
  help?: string;
}) {
  // Local draft so typing doesn't fire a PUT per keystroke — committed on blur/Enter.
  const [draft, setDraft] = useState(String(value));
  useEffect(() => setDraft(String(value)), [value]);
  const commit = () => {
    const n = Number(draft);
    if (Number.isFinite(n) && n !== value) onCommit(Math.max(min, n));
    else setDraft(String(value));
  };
  const field = (
    <TextField
      size="small"
      type="number"
      label={label}
      value={draft}
      disabled={disabled}
      onChange={(e) => setDraft(e.target.value)}
      onBlur={commit}
      onKeyDown={(e) => {
        if (e.key === "Enter") (e.target as HTMLInputElement).blur();
      }}
      sx={{ width }}
      inputProps={{ min, step }}
    />
  );
  return help ? <Tooltip title={help}>{field}</Tooltip> : field;
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
  const winner = m.verdict === "challenger" ? m.challenger_label : m.incumbent_label;
  return (
    <Chip
      size="small"
      color={m.verdict === "challenger" ? "warning" : "success"}
      label={`${winner} wins`}
      icon={<SportsMmaIcon />}
    />
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
        <Typography variant="body2" sx={{ fontWeight: 500 }}>
          {m.incumbent_label}{" "}
          <Typography component="span" variant="caption" color="text.secondary">
            (holder)
          </Typography>{" "}
          vs {m.challenger_label}
        </Typography>
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
  const [duration, setDuration] = useState<number | null>(null);
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

  const loadAll = useCallback(async () => {
    try {
      const [c, s, st, h] = await Promise.all([
        api.duelConfig(),
        api.duelStatus(),
        api.duelStandings(),
        api.duelHistory(20),
      ]);
      setCfg(c);
      setDuration((d) => d ?? c.duration_minutes);
      setStatus(s.status ? s : null);
      setTable(st);
      setLedger(h.duels.filter((d) => (d.matchups?.length ?? 0) > 0 || d.status === "failed"));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
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

  const startNow = async () => {
    setBusy(true);
    setError(null);
    try {
      setStatus(await api.duelStart(duration ?? undefined));
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

  const standings = table?.standings ?? [];
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
            <Button
              variant="contained"
              startIcon={<PlayArrowIcon />}
              onClick={() => void startNow()}
              disabled={busy}
            >
              Duel now
            </Button>
          )}
        </Stack>
      </Stack>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

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
                        {champion.label || champion.fingerprint}
                      </Link>
                    </Typography>
                    <Typography variant="caption" color="text.secondary">
                      Held for {champion.consecutive_sessions} consecutive session
                      {champion.consecutive_sessions === 1 ? "" : "s"} · crowned in duel #
                      {champion.duel_id} · {fmtDateTime(champion.finished_at)}
                      {champion.decisive ? "" : " · inherited by draws only"}
                    </Typography>
                  </>
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
            Ranked by match points (win 3 · draw 1), then decisive-win rate, then pair-win
            rate. Margin is the median Overall-point gap in that profile's own favour.
          </Typography>
          {standings.length === 0 ? (
            <Alert severity="info" sx={{ mt: 1.5 }}>
              No bouts on the ledger yet. A duel needs a confident pooled crown to defend and
              at least one reachable heir to challenge it.
            </Alert>
          ) : (
            <TableContainer sx={{ mt: 1.5 }}>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>#</TableCell>
                    <TableCell>Profile</TableCell>
                    <TableCell align="right">
                      <Tooltip title="Match record across every duel session: wins–losses–draws">
                        <span>W–L–D</span>
                      </Tooltip>
                    </TableCell>
                    <TableCell align="right">Pts</TableCell>
                    <TableCell align="right">Win rate</TableCell>
                    <TableCell align="right">
                      <Tooltip title="Individual interleaved A/B pairs won — the raw evidence under the verdicts">
                        <span>Pairs</span>
                      </Tooltip>
                    </TableCell>
                    <TableCell align="right">Margin</TableCell>
                    <TableCell align="right">Opponents</TableCell>
                    <TableCell align="right">Titles</TableCell>
                    <TableCell align="right">Last bout</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {standings.map((r: DuelStanding) => (
                    <TableRow
                      key={r.fingerprint}
                      hover
                      sx={r.is_champion ? { bgcolor: "action.selected" } : undefined}
                    >
                      <TableCell>{r.rank}</TableCell>
                      <TableCell>
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
                            sx={{ font: "inherit", textAlign: "left" }}
                          >
                            {r.label}
                          </Link>
                        </Stack>
                        {(r.beaten.length > 0 || r.lost_to.length > 0) && (
                          <Typography variant="caption" color="text.secondary">
                            {r.beaten.length > 0 && `beat ${r.beaten.join(", ")}`}
                            {r.beaten.length > 0 && r.lost_to.length > 0 && " · "}
                            {r.lost_to.length > 0 && `lost to ${r.lost_to.join(", ")}`}
                          </Typography>
                        )}
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
                          <Typography variant="caption" noWrap sx={{ maxWidth: 90, display: "block" }}>
                            {c.label}
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
                        <Typography variant="caption">{r.label}</Typography>
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
                              title={`${cell.pairs} pairs · median Δ ${fmtMargin(cell.median_margin)} in ${r.label}'s favour`}
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
          {ledger.length === 0 ? (
            <Alert severity="info" sx={{ mt: 1.5 }}>
              No duel sessions recorded yet.
            </Alert>
          ) : (
            <Stack spacing={1.5} sx={{ mt: 1.5 }}>
              {ledger.map((d) => (
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
        </CardContent>
      </Card>

      {/* ── Rules of the ring ────────────────────────────────────────────────────── */}
      <Card>
        <CardContent>
          <Typography variant="h6">Rules of the ring</Typography>
          <Typography variant="caption" color="text.secondary">
            The nightly window and the sequential stopping rule. A bout can't be called
            before the minimum pairs, gives up at the cap, and a statistically real winner
            under the practical margin is recorded as a draw.
          </Typography>
          <Stack
            direction="row"
            spacing={1.5}
            alignItems="center"
            flexWrap="wrap"
            useFlexGap
            sx={{ mt: 1.5 }}
          >
            <FormControlLabel
              control={
                <Switch
                  size="small"
                  checked={cfg?.enabled ?? false}
                  disabled={!cfg || busy}
                  onChange={(e) => void patch({ enabled: e.target.checked })}
                />
              }
              label={
                <Typography variant="body2">
                  Nightly duel{cfg?.timezone ? ` (${cfg.timezone})` : ""}
                </Typography>
              }
            />
            <TextField
              size="small"
              type="time"
              label="Run at"
              value={
                cfg
                  ? `${String(cfg.hour).padStart(2, "0")}:${String(cfg.minute).padStart(2, "0")}`
                  : "03:00"
              }
              disabled={!cfg || busy}
              onChange={(e) => {
                const [h, m] = e.target.value.split(":").map((x) => parseInt(x, 10));
                void patch({ hour: h || 0, minute: m || 0 });
              }}
              sx={{ width: 130 }}
              InputLabelProps={{ shrink: true }}
            />
            <NumField
              label="Window (min)"
              value={cfg?.duration_minutes ?? 120}
              disabled={!cfg || busy}
              min={1}
              onCommit={(v) => void patch({ duration_minutes: Math.round(v) })}
              help="How long a duel session runs before the ladder stops."
            />
            <NumField
              label="Min pairs"
              value={cfg?.min_pairs ?? 10}
              disabled={!cfg || busy}
              min={2}
              onCommit={(v) => void patch({ min_pairs: Math.round(v) })}
              help="No verdict before this many interleaved pairs, however lopsided."
            />
            <NumField
              label="Max pairs"
              value={cfg?.max_pairs ?? 40}
              disabled={!cfg || busy}
              min={2}
              onCommit={(v) => void patch({ max_pairs: Math.round(v) })}
              help="Futility cap — an undecided bout at this many pairs is a draw."
            />
            <NumField
              label="Min margin"
              value={cfg?.min_margin ?? 1}
              disabled={!cfg || busy}
              step={0.5}
              onCommit={(v) => void patch({ min_margin: v })}
              help="Overall points. A statistical winner under this margin is recorded as a draw — real but not worth chasing."
            />
            <NumField
              label="Rematch (days)"
              value={cfg?.rematch_days ?? 7}
              disabled={!cfg || busy}
              onCommit={(v) => void patch({ rematch_days: Math.round(v) })}
              help="Cooldown before a decided pairing can be fought again."
            />
            <NumField
              label="Duel now (min)"
              value={duration ?? cfg?.duration_minutes ?? 120}
              disabled={busy || active}
              min={1}
              onCommit={(v) => setDuration(Math.round(v))}
              help="Window length for an on-demand duel started with the button above."
            />
          </Stack>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1.5 }}>
            Duel runs join the pooled record like any other runs; duel <b>verdicts</b> live
            only here. The engine never writes a winner to the firewall — it always restores
            your pre-duel settings.
          </Typography>
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
