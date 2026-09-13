/**
 * "Can this profile exist?" — asked of the whole measured field.
 *
 * Every other audit on this page asks whether a *measurement* is sound. This one asks
 * whether a profile is still **raceable**, which became a live question the day `flows`
 * stopped being writable: a profile differing from the live firewall in a field PathBrain
 * never writes can't be applied, so the duel, the challenger race and the heirs card skip
 * it — correctly, and silently. A ledger of hundreds quietly becomes a ladder of a dozen
 * with nothing on screen saying why. This is that silence, counted.
 *
 * The headline is the **move**, not the list: "87 profiles are unreachable" is a fact
 * nobody can act on, while "they are all waiting on one value, and setting the Download
 * pipe's flow table to 512 brings back 87 profiles and 4,300 iterations of measurement" is
 * a decision. So the groups lead and the per-profile table follows.
 *
 * Deliberately **no button**. The change it names is a write to a field the registry
 * forbids, and the reason it forbids it is that writing it took the link down for about
 * half a minute every time. A one-click fix here would be the platform doing the exact
 * thing it just banned, from a page with nobody watching.
 *
 * Fetched on demand like the other audits on this page — it reads the live firewall.
 */
import { useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Typography from "@mui/material/Typography";
import { Link as RouterLink } from "react-router-dom";
import { api } from "../api/client";
import { HelpTip } from "./Explain";
import type { ReachabilityAudit } from "../api/types";

export default function ReachabilityCard() {
  const [audit, setAudit] = useState<ReachabilityAudit | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      setAudit(await api.reachabilityAudit());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card sx={{ mb: 2 }}>
      <CardContent>
        <Stack
          direction={{ xs: "column", sm: "row" }}
          sx={{ alignItems: { sm: "center" }, gap: 1, mb: 1 }}
        >
          <Typography variant="h6" sx={{ flexGrow: 1 }}>
            Can this profile exist?
            <HelpTip title="Which measured profiles the firewall can actually be driven to. A profile differing in a field PathBrain never writes — the flow table, the scheduler, the queue count, the upload bandwidth — can't be applied, so the duel, the challenger race and the heirs card skip it. Read-only: it names the change that would bring the most back, and leaves the change to you." />
          </Typography>
          <Button size="small" variant="outlined" onClick={run} disabled={busy}>
            {busy ? <CircularProgress size={16} /> : audit ? "Re-check" : "Check the field"}
          </Button>
        </Stack>

        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.5 }}>
          Reads the firewall as it stands now — so a profile is "reachable" relative to the
          settings the firewall is currently on, not in the abstract.
        </Typography>

        {error && <Alert severity="warning" sx={{ mb: 1 }}>{error}</Alert>}
        {!audit && !error && (
          <Typography variant="body2" color="text.secondary">
            Not checked yet.
          </Typography>
        )}

        {audit && (
          <>
            <Alert
              severity={audit.unreachable ? "warning" : "success"}
              variant="outlined"
              sx={{ mb: 2 }}
            >
              {audit.verdict}
            </Alert>

            <Stack direction="row" sx={{ gap: 1, flexWrap: "wrap", mb: 2 }}>
              <Chip size="small" label={`${audit.reachable} reachable`} color="success" variant="outlined" />
              <Chip size="small" label={`${audit.unreachable} out of reach`}
                    color={audit.unreachable ? "warning" : "default"} variant="outlined" />
              {audit.unreachable > 0 && (
                <Chip size="small" variant="outlined"
                      label={`${audit.iterations_unreachable} iterations stranded`} />
              )}
              {audit.by_field.map((f) => (
                <Chip key={f.field} size="small" variant="outlined"
                      label={`${f.field_label}: ${f.profiles}`} />
              ))}
            </Stack>

            {audit.moves.length > 0 && (
              <Box sx={{ mb: 2 }}>
                <Typography variant="subtitle2" gutterBottom>
                  What would bring them back
                </Typography>
                <Box sx={{ overflowX: "auto" }}>
                  <Table size="small">
                    <TableHead>
                      <TableRow>
                        <TableCell>Change the firewall to</TableCell>
                        <TableCell align="right">Profiles</TableCell>
                        <TableCell align="right">Iterations</TableCell>
                        <TableCell align="right">Best Overall</TableCell>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {audit.moves.map((m) => (
                        <TableRow key={m.describe} hover>
                          <TableCell>{m.describe}</TableCell>
                          <TableCell align="right">{m.profiles}</TableCell>
                          <TableCell align="right">{m.iterations}</TableCell>
                          <TableCell align="right">
                            {m.best_overall == null ? "—" : m.best_overall.toFixed(1)}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </Box>
              </Box>
            )}

            {audit.profiles.length > 0 && (
              <Box>
                <Typography variant="subtitle2" gutterBottom>
                  Out of reach
                </Typography>
                <Box sx={{ overflowX: "auto" }}>
                  <Table size="small">
                    <TableHead>
                      <TableRow>
                        <TableCell>Profile</TableCell>
                        <TableCell>Why</TableCell>
                        <TableCell align="right">Iterations</TableCell>
                        <TableCell align="right">Rounds</TableCell>
                        <TableCell align="right">Overall</TableCell>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {audit.profiles.map((p) => (
                        <TableRow key={p.fingerprint} hover>
                          <TableCell sx={{ whiteSpace: "nowrap" }}>
                            <RouterLink to={`/profiles/${encodeURIComponent(p.fingerprint)}`}>
                              {p.name || p.label}
                            </RouterLink>
                          </TableCell>
                          <TableCell>
                            {p.diffs
                              .map((d) => `${d.label}·${d.field_label} ${String(d.from)} → ${String(d.to)}`)
                              .join("; ")}
                          </TableCell>
                          <TableCell align="right">{p.iterations}</TableCell>
                          <TableCell align="right">{p.ring_pairs ?? "—"}</TableCell>
                          <TableCell align="right">
                            {p.overall == null ? "—" : p.overall.toFixed(1)}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </Box>
                {audit.truncated > 0 && (
                  <Typography variant="caption" color="text.secondary">
                    …and {audit.truncated} more. The groups above carry the whole count.
                  </Typography>
                )}
                <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
                  "Rounds" is head-to-head evidence already fought on that profile — which can
                  no longer be extended while it is out of reach.
                </Typography>
              </Box>
            )}
          </>
        )}
      </CardContent>
    </Card>
  );
}
