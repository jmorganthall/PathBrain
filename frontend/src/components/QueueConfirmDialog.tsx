import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogTitle from "@mui/material/DialogTitle";
import List from "@mui/material/List";
import ListItem from "@mui/material/ListItem";
import ListItemText from "@mui/material/ListItemText";
import Typography from "@mui/material/Typography";
import QueueIcon from "@mui/icons-material/QueuePlayNext";

import type { QueueStatus } from "../api/types";

export interface PendingJob {
  label: string;
}

function humanDuration(seconds: number | null | undefined): string | null {
  if (seconds == null || seconds < 0) return null;
  if (seconds < 90) return `${Math.round(seconds)}s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 90) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours}h ${rest}m` : `${hours}h`;
}

const KIND_NAMES: Record<string, string> = {
  run: "benchmark run",
  sweep: "shotgun sweep",
  race: "challenger race",
  refresh: "profile re-run",
  duel: "duel session",
  baseline_test: "baseline (SQM off) test",
  current_test: "test of the current profile",
  profile_test: "profile test",
};

/**
 * "Busy now — queue this?" — the one confirm every "Run this" button shows.
 *
 * PathBrain measures one thing at a time, so a job pressed at the wrong moment can sit for
 * hours before it does anything. It *does* queue — that is not the question — but queueing
 * silently is indistinguishable from a button that did nothing, which is exactly how this
 * read to people. So when something is in the way, name it, show what is already waiting,
 * and let the user decide: queue it, or come back later.
 *
 * Deliberately no "it will start in about X". The job ahead might be a monitoring run
 * finishing in two minutes or a duel ladder running until 05:00, and a fabricated wait is
 * the one number someone would plan around.
 */
export default function QueueConfirmDialog({
  pending,
  queue,
  busy,
  onConfirm,
  onCancel,
}: {
  pending: PendingJob | null;
  queue: QueueStatus | null;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  if (!pending) return null;

  const blocker = queue?.blocked_by ?? null;
  const held = humanDuration(queue?.held_for_s);
  const waiting = queue?.pending ?? [];

  return (
    <Dialog open onClose={busy ? undefined : onCancel} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ display: "flex", alignItems: "center", gap: 1 }}>
        <QueueIcon color="primary" /> Busy now — queue this?
      </DialogTitle>
      <DialogContent>
        <Typography variant="body2" gutterBottom>
          {blocker ? (
            <>
              The pipeline is being used by <strong>{blocker}</strong>
              {held ? ` (running for ${held})` : ""}. PathBrain measures one thing at a time,
              so this would wait its turn.
            </>
          ) : (
            <>
              {waiting.length === 1 ? "A job is" : `${waiting.length} jobs are`} already
              waiting to run, so this would go behind {waiting.length === 1 ? "it" : "them"}.
            </>
          )}
        </Typography>

        <Typography variant="body2" sx={{ mt: 1.5 }}>
          Queue <strong>{pending.label}</strong>? Nothing is applied to the firewall while it
          waits, and you can cancel it from the jobs menu at any point before it starts.
        </Typography>

        {waiting.length > 0 && (
          <List dense sx={{ mt: 1, mb: 0 }}>
            {waiting.map((job) => (
              <ListItem key={`${job.kind}-${job.ticket_id ?? job.id}`} disableGutters sx={{ py: 0.25 }}>
                <ListItemText
                  primary={job.label}
                  secondary={`queued · ${KIND_NAMES[job.kind] ?? job.kind}`}
                  primaryTypographyProps={{ variant: "body2" }}
                  secondaryTypographyProps={{ variant: "caption" }}
                />
              </ListItem>
            ))}
          </List>
        )}

        <Alert severity="info" sx={{ mt: 1.5 }}>
          No estimate of when it starts: the job ahead might finish in minutes or run until
          morning.
        </Alert>
      </DialogContent>
      <DialogActions>
        <Button onClick={onCancel} disabled={busy}>
          Not now
        </Button>
        <Button variant="contained" startIcon={<QueueIcon />} onClick={onConfirm} disabled={busy}>
          {busy ? "Queueing…" : "Queue it"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
