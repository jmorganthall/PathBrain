/**
 * Link watch — the continuous ping beside every firewall write.
 *
 * The card exists to answer one question that a ledger of writes cannot answer on its
 * own: **which write broke the firewall?** So the headline is not "how many gaps" — a
 * count of gaps says only that the link is unstable, which anyone watching Netflix
 * already knew. It is the **split**: of the gaps seen, how many had a PathBrain write in
 * flight and how many had nothing at all going on. Those are opposite findings and they
 * are the same number of gaps.
 *
 * The unattributed rows are deliberately given equal billing. An instrument that only
 * sampled around writes could only ever conclude that writes cause gaps, because it never
 * looked anywhere else.
 */
import { useCallback, useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import FormControlLabel from "@mui/material/FormControlLabel";
import Stack from "@mui/material/Stack";
import Switch from "@mui/material/Switch";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";
import { api } from "../api/client";
import type { LinkWatchResponse } from "../api/types";
import { fmtDateTime } from "../utils/format";

export function fmtGap(ms: number | null | undefined): string {
  // null is "nothing was watching", which is not "nothing went wrong" — a card that
  // renders them identically is lying about its own coverage.
  if (ms == null) return "—";
  if (ms <= 0) return "clean";
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${Math.round(ms)} ms`;
}

export default function LinkWatchCard() {
  const [data, setData] = useState<LinkWatchResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setData(await api.linkWatch(100, 24));
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void load();
    const t = window.setInterval(() => void load(), 10000);
    return () => window.clearInterval(t);
  }, [load]);

  const toggle = async (enabled: boolean) => {
    setBusy(true);
    try {
      await api.setLinkWatch(enabled);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const status = data?.status;
  const summary = data?.summary;
  const targets = Object.entries(status?.targets ?? {});

  return (
    <Card sx={{ mb: 2 }}>
      <CardContent>
        <Stack
          direction={{ xs: "column", sm: "row" }}
          spacing={1}
          alignItems={{ xs: "flex-start", sm: "center" }}
          sx={{ mb: 1 }}
        >
          <Typography variant="h6" sx={{ flexGrow: 1 }}>
            Link watch — which write broke it
          </Typography>
          <FormControlLabel
            control={
              <Switch
                size="small"
                checked={!!status?.running}
                disabled={busy}
                onChange={(e) => void toggle(e.target.checked)}
              />
            }
            label={status?.running ? "Watching" : "Off"}
          />
        </Stack>

        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          A continuous ping runs beside every write, so each row in the ledger below says
          what it cost. Two targets, because they separate the diagnosis: the firewall's
          own address going quiet means the <b>box</b> was wedged, while traffic stopping
          <i> through</i> it while the box still answers is a queue rebuild dropping flows.
        </Typography>

        {error && (
          <Alert severity="warning" sx={{ mb: 2 }} onClose={() => setError(null)}>
            {error}
          </Alert>
        )}

        {status && !status.running && (
          <Alert severity="info" sx={{ mb: 2 }}>
            {status.error
              ? `Not watching — ${status.error}`
              : "Not watching. Writes are still ledgered, but nothing measures what they cost."}
          </Alert>
        )}

        {targets.map(([label, t]) => (
          <Stack
            key={label}
            direction="row"
            spacing={1}
            alignItems="center"
            flexWrap="wrap"
            useFlexGap
            sx={{ mb: 1 }}
          >
            <Chip size="small" label={label === "firewall" ? "the box" : "through it"} />
            <Typography variant="body2" sx={{ fontFamily: "monospace" }}>
              {t.address}
            </Typography>
            {t.error ? (
              <Chip size="small" color="error" label={t.error} />
            ) : (
              <Typography variant="caption" color="text.secondary">
                last minute: {t.last_minute.sent} sent, {t.last_minute.loss_pct ?? 0}% lost,
                worst gap {fmtGap(t.last_minute.worst_gap_ms)}
                {t.last_minute.rtt_median_ms != null &&
                  ` · ${t.last_minute.rtt_median_ms.toFixed(1)} ms typical`}
              </Typography>
            )}
          </Stack>
        ))}

        {summary && (
          <Alert
            severity={
              summary.gaps === 0 ? "success" : summary.during_a_write > 0 ? "warning" : "info"
            }
            sx={{ mt: 2 }}
          >
            {summary.gaps === 0 ? (
              <>
                No gap over {status?.min_gap_ms ?? 250} ms on either target in the last{" "}
                {summary.hours} hours.
              </>
            ) : (
              <>
                <b>
                  {summary.gaps} gap{summary.gaps === 1 ? "" : "s"} in the last {summary.hours}{" "}
                  hours: {summary.during_a_write} with a PathBrain write in flight,{" "}
                  {summary.unattributed} with nothing running.
                </b>{" "}
                Worst overall {fmtGap(summary.worst_ms)}
                {summary.during_a_write > 0 && (
                  <> · worst during a write {fmtGap(summary.worst_during_a_write_ms)}</>
                )}
                .{" "}
                {summary.during_a_write === 0
                  ? "No gap has coincided with a write — on this evidence the drops are not PathBrain's."
                  : summary.unattributed === 0
                    ? "Every gap coincided with a write."
                    : "Both kinds are happening; the writes are not the whole story."}
              </>
            )}
          </Alert>
        )}

        {!!data?.gaps.length && (
          <Box sx={{ overflowX: "auto", mt: 2 }}>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>When</TableCell>
                  <TableCell>What went quiet</TableCell>
                  <TableCell align="right">For</TableCell>
                  <TableCell>PathBrain was…</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {data.gaps.map((g) => (
                  <TableRow key={g.id} hover>
                    <TableCell sx={{ whiteSpace: "nowrap" }}>{fmtDateTime(g.at)}</TableCell>
                    <TableCell>{g.target === "firewall" ? "the box itself" : "through it"}</TableCell>
                    <TableCell align="right">{fmtGap(g.duration_ms)}</TableCell>
                    <TableCell>
                      {g.write_id ? (
                        <Chip
                          size="small"
                          color="warning"
                          label={`writing (${g.op}${g.owner ? ` · ${g.owner}` : ""})`}
                        />
                      ) : (
                        <Typography variant="caption" color="text.secondary">
                          not writing
                        </Typography>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </Box>
        )}
      </CardContent>
    </Card>
  );
}
