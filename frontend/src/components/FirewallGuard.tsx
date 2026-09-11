// Top-bar firewall guard: the one control that says whether PathBrain may write the
// firewall at all, and the ledger of what it has written.
//
// The write path was the one part of PathBrain with no instrument on it. This chip is
// that instrument in the corner of every page: green when writes are armed, with the
// reconfigure rate over the last hour; red when hands-off, with the reason — an outage,
// the write budget, a new build waiting to be armed, or a person's own decision. The
// popover carries the last writes (who, what, how many reloads, how long the firewall
// took, and whether it went, was verified after a timeout, failed, or was refused), and the
// two actions: Arm (the only way writes resume) and Hands off (refuse everything now).
import { useCallback, useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import Divider from "@mui/material/Divider";
import Popover from "@mui/material/Popover";
import Snackbar from "@mui/material/Snackbar";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import ShieldIcon from "@mui/icons-material/Shield";
import GppBadIcon from "@mui/icons-material/GppBad";

import { api } from "../api/client";
import type { FirewallGuardStatus, FirewallWriteRow } from "../api/types";
import { fmtTimeShort } from "../utils/format";

const POLL_MS = 30_000;

const KIND_LABEL: Record<string, string> = {
  outage: "firewall outage",
  budget: "write budget exceeded",
  deploy: "new build — arm to enable writes",
  manual: "set by hand",
  hands_off: "hands-off",
};

function outcomeColor(o: FirewallWriteRow["outcome"]): string {
  return o === "ok" ? "success.main" : o === "verified" ? "warning.main" : o === "refused" ? "text.secondary" : "error.main";
}

export default function FirewallGuard() {
  const [info, setInfo] = useState<FirewallGuardStatus | null>(null);
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const [busy, setBusy] = useState(false);
  const [snack, setSnack] = useState<{ msg: string; sev: "success" | "error" | "warning" } | null>(null);

  const load = useCallback(() => {
    api.firewallGuard(25).then(setInfo).catch(() => {});
  }, []);
  useEffect(() => {
    load();
    const t = setInterval(load, POLL_MS);
    return () => clearInterval(t);
  }, [load]);

  const handsOff = info?.hands_off ?? false;
  const cap = info?.config.max_reconfigures_per_hour ?? 0;

  const arm = async () => {
    setBusy(true);
    try {
      setInfo(await api.firewallArm());
      setSnack({ msg: "Firewall writes armed on this build.", sev: "success" });
    } catch (e) {
      setSnack({ msg: e instanceof Error ? e.message : String(e), sev: "error" });
    } finally {
      setBusy(false);
      load();
    }
  };
  const off = async () => {
    setBusy(true);
    try {
      setInfo(await api.firewallHandsOff("set by hand from the top bar"));
      setSnack({ msg: "Hands off: every firewall write is refused until you arm again.", sev: "warning" });
    } catch (e) {
      setSnack({ msg: e instanceof Error ? e.message : String(e), sev: "error" });
    } finally {
      setBusy(false);
      load();
    }
  };

  const label = !info
    ? "guard"
    : handsOff
      ? "Hands off"
      : `${info.reconfigures_last_hour} reload${info.reconfigures_last_hour === 1 ? "" : "s"}/h`;

  return (
    <>
      <Tooltip
        title={
          !info
            ? "Firewall guard"
            : handsOff
              ? `Firewall writes are OFF: ${info.reason ?? ""}`
              : `Firewall writes armed · ${info.reconfigures_last_hour} shaper reloads in the last hour${cap ? ` (cap ${cap})` : ""}`
        }
      >
        <Chip
          size="small"
          icon={handsOff ? <GppBadIcon /> : <ShieldIcon />}
          label={label}
          color={handsOff ? "error" : "success"}
          variant={handsOff ? "filled" : "outlined"}
          onClick={(e) => setAnchor(e.currentTarget)}
          sx={{ mr: 1, cursor: "pointer" }}
        />
      </Tooltip>
      <Popover
        open={!!anchor}
        anchorEl={anchor}
        onClose={() => setAnchor(null)}
        anchorOrigin={{ vertical: "bottom", horizontal: "right" }}
        transformOrigin={{ vertical: "top", horizontal: "right" }}
        slotProps={{ paper: { sx: { width: "min(560px, calc(100vw - 16px))", p: 2 } } }}
      >
        <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>
          Firewall guard
        </Typography>
        <Typography variant="caption" color="text.secondary" component="div" sx={{ mb: 1 }}>
          Every write PathBrain makes to the shaper is counted, paced and budgeted here, and can be refused. A
          profile switch is one shaper reload. A write that times out is never reissued.
        </Typography>
        {info && (
          <>
            {handsOff ? (
              <Alert severity="error" sx={{ mb: 1 }}>
                <b>Hands off{info.kind ? ` · ${KIND_LABEL[info.kind] ?? info.kind}` : ""}</b>
                <br />
                {info.reason}
                {info.tripped_at ? ` (${fmtTimeShort(info.tripped_at)}, by ${info.tripped_by ?? "?"})` : ""}
                {info.refused_count > 0 ? ` · ${info.refused_count} write${info.refused_count === 1 ? "" : "s"} refused since` : ""}
              </Alert>
            ) : (
              <Alert severity="success" sx={{ mb: 1 }}>
                Writes armed{info.armed_sha ? ` on build ${info.armed_sha.slice(0, 7)}` : ""}
                {info.armed_at ? ` since ${fmtTimeShort(info.armed_at)}` : ""}.
                {info.build_sha && info.armed_sha && info.build_sha !== info.armed_sha
                  ? ` Running build ${info.build_sha.slice(0, 7)} differs — arm again to stamp it.`
                  : ""}
              </Alert>
            )}
            <Stack direction="row" spacing={2} flexWrap="wrap" useFlexGap sx={{ mb: 1 }}>
              <Box>
                <Typography variant="caption" color="text.secondary">Reloads, last hour</Typography>
                <Typography variant="body1">
                  {info.reconfigures_last_hour}
                  {cap ? <Typography component="span" variant="caption" color="text.secondary"> / {cap} cap</Typography> : null}
                </Typography>
              </Box>
              <Box>
                <Typography variant="caption" color="text.secondary">Last 24 h</Typography>
                <Typography variant="body1">{info.reconfigures_last_24h}</Typography>
              </Box>
              <Box>
                <Typography variant="caption" color="text.secondary">Refused, last hour</Typography>
                <Typography variant="body1">{info.refused_last_hour}</Typography>
              </Box>
              <Box>
                <Typography variant="caption" color="text.secondary">Min gap · cooldown</Typography>
                <Typography variant="body1">
                  {info.config.min_reconfigure_gap_s}s · {info.config.cooldown_after_outage_s}s
                </Typography>
              </Box>
            </Stack>
            <Stack direction="row" spacing={1} sx={{ mb: 1 }}>
              {handsOff ? (
                <Button size="small" variant="contained" color="success" onClick={arm} disabled={busy}>
                  Arm writes on this build
                </Button>
              ) : (
                <Button size="small" variant="outlined" color="error" onClick={off} disabled={busy}>
                  Hands off now
                </Button>
              )}
            </Stack>
            <Divider sx={{ my: 1 }} />
            <Typography variant="caption" color="text.secondary" component="div" sx={{ mb: 0.5 }}>
              Last writes — newest first
            </Typography>
            {info.writes.length === 0 ? (
              <Typography variant="caption" color="text.secondary">No writes on record yet.</Typography>
            ) : (
              <Box sx={{ overflowX: "auto", maxHeight: 280 }}>
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
                    {info.writes.map((w) => (
                      <TableRow key={w.id}>
                        <TableCell sx={{ whiteSpace: "nowrap" }}>{w.at ? fmtTimeShort(w.at) : "—"}</TableCell>
                        <TableCell sx={{ whiteSpace: "nowrap" }}>{w.owner ?? "—"}</TableCell>
                        <TableCell sx={{ whiteSpace: "nowrap" }}>{w.field ?? w.op}</TableCell>
                        <TableCell align="right">{w.reconfigures}</TableCell>
                        <TableCell align="right">{w.latency_ms == null ? "—" : `${Math.round(w.latency_ms)} ms`}</TableCell>
                        <TableCell sx={{ color: outcomeColor(w.outcome), whiteSpace: "nowrap" }}>
                          <Tooltip title={w.error ?? ""}>
                            <span>{w.outcome}</span>
                          </Tooltip>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </Box>
            )}
          </>
        )}
      </Popover>
      <Snackbar
        open={!!snack}
        autoHideDuration={5000}
        onClose={() => setSnack(null)}
        message={snack?.msg}
      />
    </>
  );
}
