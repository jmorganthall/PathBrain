/**
 * What this profile actually IS — every shaper field, per pipe, against the live firewall.
 *
 * Profile Detail led with a call sign, a grade, a standings box and a bout tape, and never
 * showed the settings: the one thing a profile *is*. The closest it came was the technical
 * summary in the subtitle ("wan: 900Mbit q1514 t5ms"), which is a lossy one-liner — a few
 * fields, no units, no per-pipe split, and nothing to compare against.
 *
 * Two questions get answered together, because on this page they are the same question:
 * *what is this profile?* and *can the firewall be put on it?* The second became live when
 * `flows` stopped being writable — a profile differing there is skipped by the duel, the
 * race and the heirs card, silently — so the verdict sits at the top of the card rather
 * than being left for the reader to infer from a table.
 *
 * Every value, and the writable/captured split, comes from the server (which reads the
 * shaper registry and formats with the registry's own formatter). Nothing here hardcodes a
 * field name or a unit: a hardcoded list in a component is what once offered the ECN field
 * a value of 4096.
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
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import { api } from "../api/client";
import type { ProfileSettingsView } from "../api/types";

export default function ProfileSettingsCard({ fingerprint }: { fingerprint: string }) {
  const [view, setView] = useState<ProfileSettingsView | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setError(null);
    try {
      setView(await api.profileSettingsView(fingerprint));
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [fingerprint]);

  useEffect(() => {
    void load();
  }, [load]);

  if (error) {
    return (
      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Typography variant="h6" gutterBottom>Settings</Typography>
          <Alert severity="warning">{error}</Alert>
        </CardContent>
      </Card>
    );
  }
  if (!view) return null;

  return (
    <Card sx={{ mb: 2 }}>
      <CardContent>
        <Stack
          direction={{ xs: "column", sm: "row" }}
          sx={{ alignItems: { sm: "center" }, gap: 1, mb: 1 }}
        >
          <Typography variant="h6" sx={{ flexGrow: 1 }}>Settings</Typography>
          {view.is_live ? (
            <Chip size="small" color="info" label="on the firewall now" />
          ) : view.can_exist ? (
            <Chip size="small" color="success" variant="outlined" label="can be applied" />
          ) : (
            <Chip size="small" color="warning" label="cannot be applied" />
          )}
        </Stack>

        {/* The verdict leads, because "can I be put on this?" is what a reader does next. */}
        <Alert
          severity={view.can_exist ? "success" : "warning"}
          variant="outlined"
          sx={{ mb: 2 }}
        >
          {view.verdict}
        </Alert>

        {view.pipes.map((pipe) => (
          <Box key={pipe.label} sx={{ mb: 2 }}>
            <Stack direction="row" sx={{ alignItems: "center", gap: 1, mb: 0.5 }}>
              <Typography variant="subtitle2">{pipe.label}</Typography>
              {!pipe.on_firewall && (
                <Chip size="small" color="warning" variant="outlined"
                      label="no matching pipe on the firewall" />
              )}
            </Stack>
            <Box sx={{ overflowX: "auto" }}>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>Field</TableCell>
                    <TableCell align="right">This profile</TableCell>
                    <TableCell align="right">Firewall now</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {pipe.fields.map((f) => (
                    <TableRow key={f.field} hover>
                      <TableCell sx={{ whiteSpace: "nowrap" }}>
                        {f.label}
                        {!f.writable && (
                          <Tooltip title="Captured on every run and part of the profile's identity, but PathBrain never writes it — so a profile differing here can't be applied.">
                            <Chip size="small" variant="outlined" label="read only"
                                  sx={{ ml: 1, height: 18, fontSize: 11 }} />
                          </Tooltip>
                        )}
                      </TableCell>
                      <TableCell
                        align="right"
                        sx={{
                          whiteSpace: "nowrap",
                          // The difference is the information, so it is what is marked —
                          // and marked by weight and colour together, never colour alone.
                          fontWeight: f.differs ? 700 : undefined,
                          color: f.differs
                            ? (f.writable ? "info.main" : "warning.main")
                            : undefined,
                        }}
                      >
                        {f.display}
                      </TableCell>
                      <TableCell align="right"
                                 sx={{ whiteSpace: "nowrap", color: "text.secondary" }}>
                        {f.differs ? f.live_display : "—"}
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Box>
          </Box>
        ))}

        <Typography variant="caption" color="text.secondary">
          "Firewall now" is shown only where it differs. A difference on a writable field is
          what applying this profile would change; one on a read-only field is why it can't
          be applied at all.
        </Typography>
      </CardContent>
    </Card>
  );
}
