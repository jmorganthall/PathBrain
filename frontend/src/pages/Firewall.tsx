/**
 * Firewall — what PathBrain does to the box it tunes, and what that costs.
 *
 * This used to live at the bottom of Config, which is where settings go, not where a
 * diagnostic you actively run belongs: it was below the fold of a long page, behind a
 * button you had to press first, and nobody could find it. Writing to the firewall is the
 * one thing PathBrain does that a household actually feels, so it gets its own section.
 */
import { useCallback, useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";
import { api } from "../api/client";
import WriteAndPing from "../components/WriteAndPing";
import type { FirewallGuardStatus, FirewallWriteRow, FqCodelPipe } from "../api/types";
import { fmtDateTime } from "../utils/format";

const OUTCOME_COLOR: Record<string, "success" | "warning" | "error" | "default"> = {
  ok: "success",
  verified: "warning",
  refused: "default",
  failed: "error",
};

export default function FirewallPage() {
  const [pipes, setPipes] = useState<FqCodelPipe[]>([]);
  const [guard, setGuard] = useState<FirewallGuardStatus | null>(null);
  const [writes, setWrites] = useState<FirewallWriteRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [loadingPipes, setLoadingPipes] = useState(false);

  const loadGuard = useCallback(async () => {
    try {
      const g = await api.firewallGuard();
      setGuard(g);
      setWrites(g.writes ?? []);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void loadGuard();
    const t = window.setInterval(() => void loadGuard(), 15000);
    return () => window.clearInterval(t);
  }, [loadGuard]);

  // Reading the pipes is a real firewall read that also stores a snapshot, so it stays a
  // deliberate press rather than firing on every page load.
  const loadPipes = useCallback(async () => {
    setLoadingPipes(true);
    setError(null);
    try {
      setPipes((await api.discover()).pipes);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoadingPipes(false);
    }
  }, []);

  return (
    <Box>
      <Typography variant="h5" gutterBottom>
        Firewall
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
        Writing to the firewall is the one thing PathBrain does that your household feels.
        Every write is on the ledger below, and the probe measures what one costs.
      </Typography>

      {error && (
        <Alert severity="warning" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {guard?.hands_off && (
        <Alert severity="error" sx={{ mb: 2 }}>
          <b>Writes are hands-off</b>
          {guard.reason ? ` — ${guard.reason}` : ""}. Nothing will be applied, including a
          restore, until you arm writes from the chip in the top bar.
        </Alert>
      )}

      <WriteAndPing pipes={pipes} onLoadPipes={loadPipes} loadingPipes={loadingPipes} />

      <Card>
        <CardContent>
          <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
            <Typography variant="h6">Every write, and what it cost</Typography>
            {guard && (
              <Chip
                size="small"
                label={`${guard.reconfigures_last_hour ?? 0} reloads in the last hour`}
              />
            )}
          </Stack>
          <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
            "What did PathBrain do in the five minutes before the drop?" — one query.
            <b> Took</b> is how long the firewall held the call; a write that timed out is
            never reissued, only re-read.
          </Typography>
          {writes.length === 0 ? (
            <Typography variant="body2" color="text.secondary">
              No writes recorded yet.
            </Typography>
          ) : (
            <Box sx={{ overflowX: "auto" }}>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>When</TableCell>
                    <TableCell>By</TableCell>
                    <TableCell>Fields</TableCell>
                    <TableCell align="right">Reloads</TableCell>
                    <TableCell align="right">Took</TableCell>
                    <TableCell>Outcome</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {writes.map((w) => (
                    <TableRow key={w.id} hover>
                      <TableCell sx={{ whiteSpace: "nowrap" }}>{fmtDateTime(w.at)}</TableCell>
                      <TableCell>{w.owner ?? "—"}</TableCell>
                      <TableCell>{w.field ?? "—"}</TableCell>
                      <TableCell align="right">{w.reconfigures}</TableCell>
                      <TableCell align="right">
                        {w.latency_ms == null ? "—" : `${Math.round(w.latency_ms)} ms`}
                      </TableCell>
                      <TableCell>
                        <Chip
                          size="small"
                          label={w.outcome}
                          color={OUTCOME_COLOR[w.outcome] ?? "default"}
                        />
                        {w.error && (
                          <Typography variant="caption" display="block" color="text.secondary">
                            {w.error}
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
    </Box>
  );
}
