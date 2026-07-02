import { useCallback, useEffect, useRef, useState } from "react";
import Button from "@mui/material/Button";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogContentText from "@mui/material/DialogContentText";
import DialogTitle from "@mui/material/DialogTitle";
import Stack from "@mui/material/Stack";
import Tooltip from "@mui/material/Tooltip";
import SystemUpdateAltIcon from "@mui/icons-material/SystemUpdateAlt";

import { api } from "../api/client";
import type { VersionInfo } from "../api/types";

// Top-bar affordance shown only when a newer build is available to pull. Polls the backend's
// cached /api/version (hourly) so it never hammers GitHub. When a Watchtower sidecar is wired
// up (info.self_update.available), it also offers a one-click "Update container" that asks
// Watchtower to pull :latest and recreate this container — then polls until the app restarts
// on the new build and reloads the page.
export default function UpdateChip() {
  const [info, setInfo] = useState<VersionInfo | null>(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [updating, setUpdating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The build SHA we were on when the update was triggered, so we can detect the restart.
  const preUpdateSha = useRef<string | null>(null);

  // Hourly availability check — paused while an update is in flight (the fast poll takes over).
  useEffect(() => {
    if (updating) return;
    let alive = true;
    const check = () =>
      api
        .version()
        .then((v) => alive && setInfo(v))
        .catch(() => {});
    check();
    const t = setInterval(check, 60 * 60 * 1000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [updating]);

  // While updating: poll fast; when the container is back on a new build, reload the page.
  useEffect(() => {
    if (!updating) return;
    let alive = true;
    const t = setInterval(async () => {
      try {
        const v = await api.version();
        if (!alive) return;
        const shaChanged =
          !!v.git_sha && !!preUpdateSha.current && v.git_sha !== preUpdateSha.current;
        // Fallback when the build SHA is knowable but unchanged-detection missed: we were
        // behind and now aren't (the new image == latest), so the update landed.
        const noLongerBehind = preUpdateSha.current != null && v.update_available === false;
        if (shaChanged || noLongerBehind) window.location.reload();
      } catch {
        // Container is down mid-restart — keep polling until it answers again.
      }
    }, 4000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, [updating]);

  const startUpdate = useCallback(async () => {
    setConfirmOpen(false);
    setError(null);
    preUpdateSha.current = info?.git_sha ?? null;
    try {
      await api.applyUpdate();
      setUpdating(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not start the update");
    }
  }, [info]);

  if (!info?.update_available && !updating) {
    return error ? (
      <Chip
        color="error"
        size="small"
        variant="outlined"
        label="Update failed"
        sx={{ mr: 1 }}
        onDelete={() => setError(null)}
      />
    ) : null;
  }

  const canSelfUpdate = !!info?.self_update?.available;
  const tip = `A newer build is available to pull (ghcr.io/jmorganthall/pathbrain:latest).\nThis build: ${
    info?.git_sha_short ?? "unknown"
  } · latest: ${info?.latest_sha_short ?? "?"}`;

  return (
    <>
      <Stack direction="row" spacing={1} alignItems="center" sx={{ mr: 1 }}>
        <Tooltip title={tip}>
          <Chip
            icon={<SystemUpdateAltIcon />}
            label="Update available"
            color="warning"
            size="small"
            variant="outlined"
            component="a"
            clickable
            href={info?.compare_url ?? undefined}
            target="_blank"
            rel="noopener noreferrer"
          />
        </Tooltip>
        {canSelfUpdate && !updating && (
          <Tooltip title="Pull the latest image and recreate this container via the Watchtower sidecar">
            <Button size="small" variant="contained" color="warning" onClick={() => setConfirmOpen(true)}>
              Update container
            </Button>
          </Tooltip>
        )}
        {updating && (
          <Chip
            icon={<CircularProgress size={14} color="inherit" />}
            label="Updating…"
            color="warning"
            size="small"
          />
        )}
      </Stack>

      <Dialog open={confirmOpen} onClose={() => setConfirmOpen(false)}>
        <DialogTitle>Update PathBrain container?</DialogTitle>
        <DialogContent>
          <DialogContentText>
            This asks the Watchtower sidecar to pull the latest image
            (ghcr.io/jmorganthall/pathbrain:latest) and recreate this container. PathBrain will
            be briefly unavailable while it restarts on the new build, then this page reloads
            automatically. Any in-flight benchmark run will be interrupted.
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirmOpen(false)}>Cancel</Button>
          <Button variant="contained" color="warning" onClick={startUpdate}>
            Update &amp; restart
          </Button>
        </DialogActions>
      </Dialog>

      <Dialog open={updating}>
        <DialogTitle>Updating PathBrain…</DialogTitle>
        <DialogContent>
          <Stack direction="row" spacing={2} alignItems="center">
            <CircularProgress size={24} />
            <DialogContentText>
              Watchtower is pulling the new image and recreating the container. This page reloads
              once PathBrain is back on the new build. If it doesn’t, reload manually in a moment.
            </DialogContentText>
          </Stack>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => window.location.reload()}>Reload now</Button>
        </DialogActions>
      </Dialog>
    </>
  );
}
