import { useCallback, useEffect, useMemo, useState } from "react";
import { Link as RouterLink } from "react-router-dom";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Snackbar from "@mui/material/Snackbar";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import EqualizerIcon from "@mui/icons-material/Equalizer";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import StopIcon from "@mui/icons-material/Stop";
import RefreshIcon from "@mui/icons-material/Refresh";

import { api } from "../api/client";
import type { DuelCard, DuelCardEntry, DuelLever, DuelLive, DuelMatchup, DuelSession, LeverLedger } from "../api/types";
import { FoldCard, HelpTip } from "../components/Explain";
import LeverLedgerCard from "../components/LeverLedgerCard";
import { useQueuedAction } from "../hooks/useQueuedAction";
import { fmtDateTime, fmtNum } from "../utils/format";

/**
 * Lever duels — measuring one setting at a time.
 *
 * A profile is a bundle of levers, and a bout between two bundles that differ in four of
 * them is four questions asked at once with one answer. A lever session seats the champion
 * against single-setting variants of ITSELF, so every round is a paired, interleaved,
 * same-weather reading of one lever. It is a session KIND chosen here for one session at a
 * time, never a standing mode on the ladder: a lever session measures settings, it does not
 * hunt the best profile, and the ladder's own config never learns about it.
 */

const isRunning = (s: DuelSession | null) => !!s && (s.status === "running" || s.status === "pending");

function leverText(l: DuelLever | null | undefined): string {
  if (!l) return "";
  return `${l.pipe} ${l.field_label} ${String(l.from)} → ${String(l.to)}`;
}

function LeverChip({ lever, source }: { lever: DuelLever | null | undefined; source?: string | null }) {
  if (!lever) return null;
  return (
    <Tooltip title={source ?? "The one setting this match measures. Everything else is identical on both sides."}>
      <Chip size="small" variant="outlined" color="info" label={leverText(lever)} sx={{ height: 22 }} />
    </Tooltip>
  );
}

/** "lever: wan Quantum 1514 → 757 (a measured profile, 20 iterations)" → its two halves. */
function splitReason(reason: string | null | undefined): { lever: string; note: string } {
  const text = (reason ?? "").replace(/^lever:\s*/, "");
  const i = text.indexOf(" (");
  return i < 0 ? { lever: text, note: "" } : { lever: text.slice(0, i), note: text.slice(i + 2).replace(/\)$/, "") };
}

function crownSplit(m: Record<string, number> | null | undefined): string {
  if (!m) return "";
  return Object.entries(m)
    .map(([k, v]) => `${k} ${v > 0 ? "+" : ""}${fmtNum(v, 1)}`)
    .join(" · ");
}

function PreviewCard({ card, busy, onRefresh }: { card: DuelCard | null; busy: boolean; onRefresh: () => void }) {
  const rows = card?.queue ?? [];
  return (
    <FoldCard
      title="What a lever session would seat"
      defaultOpen
      summary={
        card?.incumbent ? (
          <>
            <b>{card.incumbent.name ?? card.incumbent.label}</b> defends against {card.total ?? rows.length}{" "}
            single-setting variant{(card.total ?? rows.length) === 1 ? "" : "s"} of itself.
            <HelpTip title="Measured siblings first (a profile already on record that differs from the champion in exactly one setting — the session matures it), then generated steps the firewall can hold: the next option on a select, halve and double on quantum, limit or flows, the flip on ECN. Levers with the least paired evidence on the ledger are asked first. A projection: the engine re-reads the ledger each cycle." />
          </>
        ) : busy ? (
          "Ranking the field…"
        ) : (
          card?.reason ?? "No preview yet."
        )
      }
      actions={
        <Button size="small" startIcon={busy ? <CircularProgress size={14} /> : <RefreshIcon />} disabled={busy} onClick={onRefresh}>
          Refresh
        </Button>
      }
    >
      {card?.incumbent && (
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
          Defender: <b>{card.incumbent.name ?? card.incumbent.label}</b>
          {card.incumbent.overall != null ? ` · Overall ${fmtNum(card.incumbent.overall, 1)}` : ""} —{" "}
          {card.incumbent.why ?? "the ring's #1"}.
        </Typography>
      )}
      {rows.length === 0 ? (
        <Alert severity="info">
          {card?.reason ??
            "Nothing to seat: the champion is either unreachable from the live firewall or has no settings on record."}
        </Alert>
      ) : (
        <TableContainer sx={{ overflowX: "auto" }}>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>#</TableCell>
                <TableCell>Lever</TableCell>
                <TableCell>Variant</TableCell>
                <TableCell align="right">Overall on record</TableCell>
                <TableCell />
              </TableRow>
            </TableHead>
            <TableBody>
              {rows.map((e: DuelCardEntry) => {
                const { lever, note } = splitReason(e.reason);
                const generated = note.startsWith("a new step");
                return (
                  <TableRow key={e.fingerprint} hover>
                    <TableCell>{e.position}</TableCell>
                    <TableCell sx={{ whiteSpace: "nowrap", fontWeight: 600 }}>{lever}</TableCell>
                    <TableCell>
                      {e.name ?? e.label ?? e.fingerprint.slice(0, 8)}
                      {e.name && e.label && (
                        <Typography variant="caption" color="text.secondary" sx={{ display: "block" }}>
                          {e.label}
                        </Typography>
                      )}
                    </TableCell>
                    <TableCell align="right">{e.overall != null ? fmtNum(e.overall, 1) : "—"}</TableCell>
                    <TableCell>
                      <Tooltip title={note}>
                        <Chip
                          size="small"
                          variant="outlined"
                          color={generated ? "default" : "success"}
                          label={generated ? "new step" : "measured sibling"}
                          sx={{ height: 20 }}
                        />
                      </Tooltip>
                      {e.on_cooldown && (
                        <Chip size="small" variant="outlined" label="re-race" sx={{ height: 20, ml: 0.5 }} />
                      )}
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </TableContainer>
      )}
    </FoldCard>
  );
}

function SeatLine({ seat }: { seat: DuelLive }) {
  return (
    <Box sx={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 1, py: 0.5 }}>
      <Typography variant="body2" sx={{ fontWeight: 600 }}>
        {seat.challenger?.name ?? seat.challenger?.label}
      </Typography>
      <LeverChip lever={seat.lever} />
      <Typography variant="caption" color="text.secondary">
        round {seat.pairs ?? 0}
        {seat.median_margin != null ? ` · median Δ ${seat.median_margin > 0 ? "+" : ""}${fmtNum(seat.median_margin, 2)}` : ""}
        {seat.crown_margins && Object.keys(seat.crown_margins).length > 0 ? ` · ${crownSplit(seat.crown_margins)}` : ""}
      </Typography>
      {seat.measuring && <Chip size="small" color="primary" label="measuring" sx={{ height: 20 }} />}
    </Box>
  );
}

function MatchLine({ m }: { m: DuelMatchup }) {
  const won = m.verdict === "challenger" ? "variant won" : m.verdict === "incumbent" ? "champion held" : m.verdict === "draw" ? "draw" : m.verdict;
  return (
    <Box sx={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 1, py: 0.5 }}>
      <LeverChip lever={m.lever} />
      <Typography variant="body2">
        {m.challenger_name ?? m.challenger_label} vs {m.incumbent_name ?? m.incumbent_label}
      </Typography>
      <Chip
        size="small"
        variant="outlined"
        color={m.verdict === "challenger" ? "success" : m.verdict === "incumbent" ? "default" : "warning"}
        label={won}
        sx={{ height: 20 }}
      />
      <Typography variant="caption" color="text.secondary">
        {m.wins_incumbent}–{m.wins_challenger} in {m.pairs} round{m.pairs === 1 ? "" : "s"}
        {m.median_delta != null ? ` · Δ ${m.median_delta > 0 ? "+" : ""}${fmtNum(m.median_delta, 2)}` : ""}
        {m.median_crown_delta ? ` · ${crownSplit(m.median_crown_delta)}` : ""}
      </Typography>
    </Box>
  );
}

export default function Levers() {
  const [minutes, setMinutes] = useState<number>(120);
  const [status, setStatus] = useState<DuelSession | null>(null);
  const [card, setCard] = useState<DuelCard | null>(null);
  const [cardBusy, setCardBusy] = useState(false);
  const [book, setBook] = useState<LeverLedger | null>(null);
  const [history, setHistory] = useState<DuelSession[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const queue = useQueuedAction(setToast);

  const loadStatus = useCallback(async () => {
    try {
      setStatus(await api.duelStatus());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);
  const loadCard = useCallback(async () => {
    setCardBusy(true);
    try {
      setCard(await api.duelCard(16, "levers"));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setCardBusy(false);
    }
  }, []);
  const loadBook = useCallback(async () => {
    try {
      setBook(await api.leverLedger());
    } catch {
      setBook(null);
    }
  }, []);
  const loadHistory = useCallback(async () => {
    try {
      const h = await api.duelHistory(20);
      setHistory(h.duels.filter((d) => d.mode === "levers"));
    } catch {
      setHistory([]);
    }
  }, []);

  useEffect(() => {
    api
      .duelConfig()
      .then((c) => setMinutes(c.duration_minutes || 120))
      .catch(() => {});
    void loadStatus();
    void loadCard();
    void loadBook();
    void loadHistory();
  }, [loadStatus, loadCard, loadBook, loadHistory]);

  // Poll while any session holds the ring; when it ends, the book and the history move.
  const running = isRunning(status);
  useEffect(() => {
    if (!running) return;
    const t = setInterval(() => void loadStatus(), 5000);
    return () => clearInterval(t);
  }, [running, loadStatus]);
  const [wasRunning, setWasRunning] = useState(false);
  useEffect(() => {
    if (running) setWasRunning(true);
    else if (wasRunning) {
      setWasRunning(false);
      void loadBook();
      void loadHistory();
    }
  }, [running, wasRunning, loadBook, loadHistory]);

  const leverSession = status?.mode === "levers";
  const otherSession = running && !leverSession;

  const start = () =>
    queue.submit({
      label: `Lever session · ${minutes} min`,
      run: async () => {
        const started = await api.duelStart(minutes, "levers");
        if (!started.queued) setStatus(started);
        return started;
      },
    });
  const cancel = async () => {
    try {
      await api.duelCancel();
      setToast("Cancelling after the current round — your settings are restored either way");
      await loadStatus();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  };

  const liveSeats = useMemo(() => status?.live?.seats ?? (status?.live ? [status.live] : []), [status]);
  const decided = status?.matchups ?? [];

  return (
    <Box>
      <Stack direction="row" alignItems="center" spacing={1} sx={{ mb: 1 }}>
        <EqualizerIcon color="primary" />
        <Typography variant="h5">Lever duels</Typography>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2, maxWidth: 900 }}>
        A profile is a bundle of settings, and a bout between two profiles that differ in four of them is
        four questions asked at once with one answer. A lever session seats the champion against
        single-setting variants of <b>itself</b>, so every round is a paired, same-weather reading of one
        setting — the strongest evidence this platform can produce about what a setting does. It runs for
        one session and then the ladder goes back to its own matchmaking.
      </Typography>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {/* ── Start / live ─────────────────────────────────────────────────────── */}
      <Card sx={{ mb: 2 }}>
        <CardContent>
          {leverSession && running ? (
            <Stack spacing={1}>
              <Stack direction="row" alignItems="center" spacing={1} flexWrap="wrap">
                <Chip size="small" color="primary" label="lever session in the ring" />
                <Typography variant="body2">{status?.stage ?? "starting…"}</Typography>
                <Box sx={{ flex: 1 }} />
                <Button size="small" color="warning" variant="outlined" startIcon={<StopIcon />} onClick={cancel}>
                  Stop after this round
                </Button>
                <Button size="small" component={RouterLink} to="/duels">
                  Full board
                </Button>
              </Stack>
              {status?.live?.reference && (
                <Typography variant="caption" color="text.secondary">
                  Reference: {status.live.reference.name ?? status.live.reference.label}
                </Typography>
              )}
              {liveSeats.map((s, i) => (
                <SeatLine key={s.challenger?.fingerprint ?? i} seat={s} />
              ))}
              {decided.length > 0 && (
                <Box>
                  <Typography variant="subtitle2" sx={{ mt: 1 }}>
                    Decided this session
                  </Typography>
                  {decided.map((m, i) => (
                    <MatchLine key={i} m={m} />
                  ))}
                </Box>
              )}
            </Stack>
          ) : (
            <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} alignItems={{ sm: "center" }}>
              <TextField
                size="small"
                type="number"
                label="Run for (minutes)"
                value={minutes}
                onChange={(e) => setMinutes(Math.max(5, Math.round(Number(e.target.value) || 0)))}
                inputProps={{ min: 5, step: 5 }}
                sx={{ width: 170 }}
              />
              <Button
                variant="contained"
                startIcon={<PlayArrowIcon />}
                disabled={queue.busy || !card?.incumbent || (card?.queue?.length ?? 0) === 0}
                onClick={start}
              >
                Start a lever session
              </Button>
              <Typography variant="caption" color="text.secondary">
                Runs through the same queue as every other benchmark and restores your settings when it ends.
                {otherSession ? " Another session holds the ring right now; this one will queue behind it." : ""}
              </Typography>
            </Stack>
          )}
        </CardContent>
      </Card>

      {otherSession && (
        <Alert severity="info" sx={{ mb: 2 }} action={<Button size="small" component={RouterLink} to="/duels">Open</Button>}>
          The ladder is running an ordinary session ({status?.stage ?? "in progress"}).
        </Alert>
      )}

      <PreviewCard card={card} busy={cardBusy} onRefresh={() => void loadCard()} />

      <LeverLedgerCard book={book} />

      <FoldCard
        title="Past lever sessions"
        summary={`${history.length} lever session${history.length === 1 ? "" : "s"} on record.`}
      >
        {history.length === 0 ? (
          <Typography variant="body2" color="text.secondary">
            None yet. Start one above; each match it decides lands in the ledger.
          </Typography>
        ) : (
          <Stack spacing={1.5}>
            {history.map((d) => (
              <Box key={d.id}>
                <Typography variant="subtitle2">
                  {fmtDateTime(d.started_at ?? d.created_at)} · {d.status}
                  {d.matchups_total != null ? ` · ${d.matchups_total} match${d.matchups_total === 1 ? "" : "es"}` : ""}
                  {d.iterations_run ? ` · ${d.iterations_run} iterations` : ""}
                </Typography>
                {(d.matchups ?? []).map((m, i) => (
                  <MatchLine key={i} m={m} />
                ))}
                {d.error && (
                  <Typography variant="caption" color="error.main">
                    {d.error}
                  </Typography>
                )}
              </Box>
            ))}
          </Stack>
        )}
      </FoldCard>

      <Snackbar open={!!toast} autoHideDuration={6000} onClose={() => setToast(null)} message={toast ?? ""} />
      {queue.dialog}
    </Box>
  );
}
