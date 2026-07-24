// Top-bar "Follow best" control: a switch that arms the crown follower (keep the
// firewall's SQM settings on the crowned best profile as the crown changes), plus a
// popover with the follower's status, the crown-churn statistics ("how often does the
// best profile change?"), and the recent crown-change ledger. Tracking (the ledger)
// runs whether or not the switch is on — the switch only arms the firewall write.
import { useCallback, useEffect, useState } from "react";
import { Link as RouterLink } from "react-router-dom";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Divider from "@mui/material/Divider";
import IconButton from "@mui/material/IconButton";
import Link from "@mui/material/Link";
import Popover from "@mui/material/Popover";
import Snackbar from "@mui/material/Snackbar";
import Stack from "@mui/material/Stack";
import Switch from "@mui/material/Switch";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import EmojiEventsIcon from "@mui/icons-material/EmojiEvents";

import { api } from "../api/client";
import type { CrownFollowStatus } from "../api/types";
import { fmtTimeShort } from "../utils/format";

const POLL_MS = 60_000;

function fmtHours(h: number | null | undefined): string {
  if (h == null || Number.isNaN(h)) return "—";
  if (h < 1) return `${Math.round(h * 60)} min`;
  if (h < 48) return `${h.toFixed(h < 10 ? 1 : 0)} h`;
  return `${(h / 24).toFixed(1)} d`;
}

export default function FollowBest() {
  const [info, setInfo] = useState<CrownFollowStatus | null>(null);
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const [busy, setBusy] = useState(false); // toggle or sync in flight
  const [snack, setSnack] = useState<{ msg: string; sev: "success" | "error" | "info" } | null>(
    null,
  );

  const load = useCallback(() => {
    api
      .crownFollow()
      .then(setInfo)
      .catch(() => {});
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, POLL_MS);
    return () => clearInterval(t);
  }, [load]);

  const enabled = info?.config.enabled ?? false;

  const toggle = (next: boolean) => {
    setBusy(true);
    // Optimistic flip so the switch feels instant; reconcile with the server response.
    setInfo((v) => (v ? { ...v, config: { ...v.config, enabled: next } } : v));
    api
      .crownFollowUpdate({ enabled: next })
      .then(() => {
        setSnack(
          next
            ? {
                msg: "Follow best is on — the firewall will track the crowned profile.",
                sev: "success",
              }
            : { msg: "Follow best is off — crown changes are still recorded.", sev: "info" },
        );
        load();
      })
      .catch((e) => {
        setSnack({ msg: e instanceof Error ? e.message : "Could not update.", sev: "error" });
        load();
      })
      .finally(() => setBusy(false));
  };

  const syncNow = () => {
    setBusy(true);
    api
      .crownFollowSync()
      .then(({ result }) => {
        if (result.applied) setSnack({ msg: "Crown applied to the firewall.", sev: "success" });
        else if (result.error) setSnack({ msg: result.error, sev: "error" });
        else if (result.apply_skipped)
          setSnack({ msg: result.apply_skipped, sev: "info" });
        else if (result.on_crown)
          setSnack({ msg: "Firewall is already on the crowned profile.", sev: "success" });
        else if (!result.crown_fingerprint)
          setSnack({ msg: "No confident crown yet — nothing to follow.", sev: "info" });
        else setSnack({ msg: "Checked.", sev: "info" });
        load();
      })
      .catch((e) => {
        setSnack({ msg: e instanceof Error ? e.message : "Check failed.", sev: "error" });
      })
      .finally(() => setBusy(false));
  };

  const stats = info?.stats;
  const last = info?.status.last_result;
  const changes = (info?.events ?? []).filter((e) => e.kind === "change").slice(0, 5);

  return (
    <>
      <Box sx={{ display: "flex", alignItems: "center", mr: 1, flexShrink: 0 }}>
        <Tooltip
          title={
            enabled
              ? "Follow best is ON — the firewall tracks the crowned profile. Click for status."
              : "Follow best is OFF — click for crown-change history and stats."
          }
        >
          <IconButton size="small" onClick={(e) => setAnchor(e.currentTarget)}>
            <EmojiEventsIcon fontSize="small" color={enabled ? "warning" : "disabled"} />
          </IconButton>
        </Tooltip>
        <Typography
          variant="caption"
          color={enabled ? "text.primary" : "text.secondary"}
          sx={{
            display: { xs: "none", sm: "block" },
            cursor: "pointer",
            userSelect: "none",
            whiteSpace: "nowrap",
          }}
          onClick={(e) => setAnchor(e.currentTarget as HTMLElement)}
        >
          Follow best
        </Typography>
        <Tooltip title="Keep the firewall's SQM settings on the best confident profile as the crown changes">
          <span>
            {/* On phones the toolbar is tight (menu, title, update button, jobs) — collapse
                to the trophy icon alone and let the popover carry the switch, so the
                "Update now" button is never crowded off the right edge. */}
            <Switch
              size="small"
              checked={enabled}
              disabled={busy || info === null}
              onChange={(e) => toggle(e.target.checked)}
              inputProps={{ "aria-label": "Follow best" }}
              sx={{ display: { xs: "none", sm: "inline-flex" } }}
            />
          </span>
        </Tooltip>
      </Box>

      <Popover
        open={!!anchor}
        anchorEl={anchor}
        onClose={() => setAnchor(null)}
        anchorOrigin={{ vertical: "bottom", horizontal: "right" }}
        transformOrigin={{ vertical: "top", horizontal: "right" }}
        slotProps={{ paper: { sx: { p: 2, width: 360, maxWidth: "95vw" } } }}
      >
        <Stack spacing={1.25}>
          <Stack direction="row" alignItems="center" spacing={1}>
            <EmojiEventsIcon fontSize="small" color={enabled ? "warning" : "disabled"} />
            <Typography variant="subtitle2" sx={{ flexGrow: 1 }}>
              Follow best
            </Typography>
            <Chip
              size="small"
              label={enabled ? "following" : "tracking only"}
              color={enabled ? "warning" : "default"}
              variant="outlined"
            />
            {/* The popover always carries the switch — on phones it's the only one (the
                toolbar switch is hidden on xs to keep the update button visible). */}
            <Switch
              size="small"
              checked={enabled}
              disabled={busy || info === null}
              onChange={(e) => toggle(e.target.checked)}
              inputProps={{ "aria-label": "Follow best" }}
            />
          </Stack>
          <Typography variant="caption" color="text.secondary">
            When on, the firewall's SQM settings are re-applied to whichever confident profile
            holds the crown. The crown is re-checked as each benchmark run completes (a cheap
            single-profile test; the full standings recompute only when that run could have
            moved the crown), with a full audit every{" "}
            {Math.round(((info?.config.interval_minutes ?? 360) / 60) * 10) / 10} h as a
            backstop. Crown changes are recorded either way.
          </Typography>

          <Divider />

          {/* Current crown + firewall state from the last check */}
          <Box>
            <Typography variant="caption" color="text.secondary">
              Crown
            </Typography>
            {last?.crown_fingerprint ? (
              <Typography variant="body2">
                <Link
                  component={RouterLink}
                  to={`/profiles/${encodeURIComponent(last.crown_fingerprint)}`}
                  onClick={() => setAnchor(null)}
                  underline="hover"
                >
                  {last.crown_label || last.crown_fingerprint}
                </Link>{" "}
                {last.on_crown === true && (
                  <Chip size="small" label="firewall on crown" color="success" variant="outlined" />
                )}
                {last.on_crown === false && (
                  <Chip size="small" label="firewall elsewhere" color="warning" variant="outlined" />
                )}
              </Typography>
            ) : (
              <Typography variant="body2" color="text.secondary">
                {stats?.current_crown_label ||
                  stats?.current_crown_fingerprint ||
                  "No confident crown yet"}
              </Typography>
            )}
            <Typography variant="caption" color="text.disabled">
              Last check: {info?.status.last_check_at ? fmtTimeShort(info.status.last_check_at) : "—"}
              {last?.apply_skipped ? ` · ${last.apply_skipped}` : ""}
              {last?.error ? ` · ${last.error}` : ""}
            </Typography>
          </Box>

          {/* Churn stats: how often does the best profile change? */}
          <Box>
            <Typography variant="caption" color="text.secondary">
              Crown stability
            </Typography>
            {stats && stats.tracked_since ? (
              <>
                <Typography variant="body2">
                  Changed <b>{stats.changes_7d}×</b> in 7 days ({stats.changes_24h}× in 24 h,{" "}
                  {stats.changes_30d}× in 30 days)
                </Typography>
                <Typography variant="caption" color="text.secondary">
                  Median reign {fmtHours(stats.median_reign_hours)} · current reign{" "}
                  {fmtHours(stats.current_reign_hours)}
                  {stats.changes_per_day != null ? ` · ${stats.changes_per_day}/day` : ""} · tracked
                  since {fmtTimeShort(stats.tracked_since)}
                </Typography>
              </>
            ) : (
              <Typography variant="body2" color="text.secondary">
                No crown observations yet — stats appear after the first check.
              </Typography>
            )}
          </Box>

          {/* Recent crown changes */}
          {changes.length > 0 && (
            <Box>
              <Typography variant="caption" color="text.secondary">
                Recent changes
              </Typography>
              <Stack spacing={0.5} sx={{ mt: 0.5 }}>
                {changes.map((e) => (
                  <Typography key={e.id} variant="caption" sx={{ display: "block" }}>
                    {fmtTimeShort(e.created_at)} ·{" "}
                    {e.previous_fingerprint
                      ? `${e.previous_label || e.previous_fingerprint} → ${e.label || e.fingerprint}`
                      : `tracking started (${e.label || e.fingerprint})`}
                    {e.applied && (
                      <Chip
                        size="small"
                        label="applied"
                        color="success"
                        variant="outlined"
                        sx={{ ml: 0.5, height: 16, fontSize: "0.65rem" }}
                      />
                    )}
                    {e.error && (
                      <Chip
                        size="small"
                        label="apply failed"
                        color="error"
                        variant="outlined"
                        sx={{ ml: 0.5, height: 16, fontSize: "0.65rem" }}
                      />
                    )}
                  </Typography>
                ))}
              </Stack>
            </Box>
          )}

          <Stack direction="row" spacing={1} justifyContent="flex-end">
            <Button
              size="small"
              onClick={syncNow}
              disabled={busy}
              startIcon={busy ? <CircularProgress size={12} /> : undefined}
            >
              Check now
            </Button>
          </Stack>
        </Stack>
      </Popover>

      <Snackbar
        open={!!snack}
        autoHideDuration={6000}
        onClose={() => setSnack(null)}
        anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
      >
        {snack ? (
          <Alert severity={snack.sev} variant="filled" onClose={() => setSnack(null)}>
            {snack.msg}
          </Alert>
        ) : undefined}
      </Snackbar>
    </>
  );
}
