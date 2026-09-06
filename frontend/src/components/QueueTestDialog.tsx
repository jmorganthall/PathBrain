import { useMemo } from "react";

import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogContentText from "@mui/material/DialogContentText";
import DialogTitle from "@mui/material/DialogTitle";
import List from "@mui/material/List";
import ListItem from "@mui/material/ListItem";
import ListItemText from "@mui/material/ListItemText";
import Typography from "@mui/material/Typography";
import QueueIcon from "@mui/icons-material/QueuePlayNext";

import type { ProfileTestQueue } from "../api/types";

// What the user asked to measure, held while they decide whether to queue it.
export interface PendingTest {
  // Shown in the dialog so "queue this" names the thing being queued.
  label: string;
  // How many iterations it would run (null = top up to the confidence minimum).
  iterations?: number;
  // Run it when the user confirms.
  run: () => void | Promise<void>;
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

/**
 * "Busy now. Queue this?" — the confirm shown when a test would not start immediately.
 *
 * The pipeline runs one firewall session at a time, and a duel window is a night, so a test
 * pressed at the wrong moment can sit for hours before it measures anything. It *does* queue
 * (that is not the question), but silently: the button reported success, the test stood still,
 * and the difference between "running" and "eighth in line behind a duel" was invisible. So
 * when something is in the way, say what it is and let the user decide — queue it, or drop it
 * and come back later.
 *
 * Deliberately no "it will start in about X": the holder might be a monitoring run finishing in
 * two minutes or a ladder running until 05:00, and a made-up wait is the one number someone
 * would plan around. What is in the way and how many are ahead is knowable, so that is what it
 * says.
 */
export default function QueueTestDialog({
  pending,
  queue,
  onConfirm,
  onCancel,
}: {
  // Non-null while the confirm is open.
  pending: PendingTest | null;
  queue: ProfileTestQueue | null;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const blocker = queue?.owner_label || queue?.owner || null;
  const held = humanDuration(queue?.held_for_s);
  const ahead = useMemo(() => {
    if (!queue) return 0;
    return (queue.running ? 1 : 0) + queue.pending.length;
  }, [queue]);

  if (!pending) return null;

  return (
    <Dialog open onClose={onCancel} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ display: "flex", alignItems: "center", gap: 1 }}>
        <QueueIcon color="primary" /> Busy now — queue this test?
      </DialogTitle>
      <DialogContent>
        <DialogContentText component="div">
          <Typography variant="body2" gutterBottom>
            {blocker ? (
              <>
                The pipeline is being used by <strong>{blocker}</strong>
                {held ? ` (running for ${held})` : ""}. PathBrain measures one thing at a time,
                so this test would wait its turn.
              </>
            ) : (
              <>
                {ahead === 1 ? "A test is" : `${ahead} tests are`} already waiting to run, so this
                one would go behind {ahead === 1 ? "it" : "them"}.
              </>
            )}
          </Typography>

          <Typography variant="body2" sx={{ mt: 1.5 }}>
            Queue <strong>{pending.label}</strong>
            {pending.iterations ? ` (${pending.iterations} iteration${pending.iterations === 1 ? "" : "s"})` : ""}?
            It applies the profile, benchmarks, and restores your settings when its turn comes —
            and you can cancel it from the jobs menu at any point before it starts.
          </Typography>

          {ahead > 0 && (
            <List dense sx={{ mt: 1, mb: 0 }}>
              {queue?.running && (
                <ListItem disableGutters sx={{ py: 0.25 }}>
                  <ListItemText
                    primary={queue.running.label || queue.running.fingerprint}
                    secondary={queue.running.stage || "running"}
                    primaryTypographyProps={{ variant: "body2" }}
                    secondaryTypographyProps={{ variant: "caption" }}
                  />
                </ListItem>
              )}
              {queue?.pending.map((t) => (
                <ListItem key={t.id} disableGutters sx={{ py: 0.25 }}>
                  <ListItemText
                    primary={t.label || t.fingerprint}
                    secondary={`queued · ${t.iterations} iteration${t.iterations === 1 ? "" : "s"}`}
                    primaryTypographyProps={{ variant: "body2" }}
                    secondaryTypographyProps={{ variant: "caption" }}
                  />
                </ListItem>
              ))}
            </List>
          )}

          <Alert severity="info" sx={{ mt: 1.5 }}>
            No estimate of when it starts: the job ahead might finish in minutes or run until
            morning. Nothing is applied to the firewall while it waits.
          </Alert>
        </DialogContentText>
      </DialogContent>
      <DialogActions>
        <Button onClick={onCancel}>Not now</Button>
        <Button variant="contained" startIcon={<QueueIcon />} onClick={onConfirm}>
          Queue it
        </Button>
      </DialogActions>
    </Dialog>
  );
}
