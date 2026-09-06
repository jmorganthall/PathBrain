import { useCallback, useState } from "react";

import { api } from "../api/client";
import type { QueuePlacement, QueueStatus } from "../api/types";
import QueueConfirmDialog from "../components/QueueConfirmDialog";

/** One submittable job: what to call it, and what to do when the user says go. */
export interface QueuedAction<T = unknown> {
  /** Shown in the confirm and in the resulting message — name the thing, not the button. */
  label: string;
  /** Fires the request. Its response should carry the universal placement block. */
  run: () => Promise<T>;
  /**
   * Skip the confirm and go straight to the queue. For actions whose whole point is
   * queueing (a batch of bets), asking "queue this?" is asking a question the user has
   * already answered.
   */
  alwaysQueue?: boolean;
}

/** Phrase one response's placement block the same way for every engine. */
export function describePlacement(placement: QueuePlacement, what: string): string {
  if (!placement.queued) return `${what} started.`;
  const where =
    placement.queue_position && placement.queue_position > 1
      ? ` (#${placement.queue_position} in the queue)`
      : "";
  const behind = placement.blocked_by ? ` behind ${placement.blocked_by}` : "";
  return `${what} queued${behind}${where}. It runs when the pipeline is free — cancel it any time from the jobs menu.`;
}

/**
 * The universal "add a job" behaviour, in one place.
 *
 * PathBrain runs one firewall/benchmark session at a time, so any "Run this" button pressed
 * while something else is going can only do one of two things: start, or wait. Every page
 * used to answer that differently — some engines refused outright with a 409, others queued
 * silently while the toast claimed the session had begun — and three behaviours for one
 * intent is not a policy, it is an accident of which module a button landed in.
 *
 * This hook is the policy. Before submitting it asks what holds the pipeline; if anything
 * does, it puts up a confirm naming the holder and what is already waiting, and only submits
 * on "Queue it". Either way the resulting message says which of the two things happened,
 * phrased identically everywhere, because "did anything happen?" should not depend on which
 * button was pressed.
 *
 * Usage:
 *
 *     const queue = useQueuedAction(setSnack);
 *     <Button onClick={() => queue.submit({ label: "Shotgun sweep", run: () => api.startSweep(...) })} />
 *     {queue.dialog}
 */
export function useQueuedAction(notify: (message: string) => void) {
  const [pending, setPending] = useState<QueuedAction | null>(null);
  const [status, setStatus] = useState<QueueStatus | null>(null);
  const [busy, setBusy] = useState(false);

  const fire = useCallback(
    async (action: QueuedAction) => {
      setBusy(true);
      try {
        const result = (await action.run()) as QueuePlacement | undefined;
        notify(describePlacement(result ?? {}, action.label));
        return result;
      } catch (e) {
        notify(e instanceof Error ? e.message : `Could not start ${action.label}.`);
        return undefined;
      } finally {
        setBusy(false);
        setPending(null);
      }
    },
    [notify],
  );

  const submit = useCallback(
    async (action: QueuedAction) => {
      if (action.alwaysQueue) return fire(action);
      let queue: QueueStatus | null = null;
      try {
        queue = await api.jobQueue();
      } catch {
        // Best-effort: if we cannot tell whether the pipeline is busy, do not block the
        // press. It queues correctly either way — the confirm exists to say so, not to
        // gate it.
      }
      if (queue && (queue.busy || queue.queue_depth > 0)) {
        setStatus(queue);
        setPending(action);
        return undefined;
      }
      return fire(action);
    },
    [fire],
  );

  const dialog = (
    <QueueConfirmDialog
      pending={pending ? { label: pending.label } : null}
      queue={status}
      busy={busy}
      onConfirm={() => {
        if (pending) void fire(pending);
      }}
      onCancel={() => setPending(null)}
    />
  );

  return { submit, dialog, busy };
}
