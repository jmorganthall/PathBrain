/**
 * Write-and-ping: what one firewall write costs the network, measured.
 *
 * The reload-storm incident left "the internet blipped" as the only evidence anyone had,
 * and that can't tell a 200ms hiccup from a thirty-second outage or say which half of a
 * write caused it. This runs the two halves separately — writing the fields, then
 * reloading the shaper — with ping running to the firewall AND through it, and reports
 * the worst continuous gap per step.
 *
 * Applies a real change to a live firewall and restores it afterwards. Deliberately a
 * deliberate act: it needs writes armed, names what it will do before doing it, and the
 * whole point is that somebody is watching.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import LinearProgress from "@mui/material/LinearProgress";
import MenuItem from "@mui/material/MenuItem";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import { api } from "../api/client";
import type { FqCodelPipe, WriteProbe, WriteProbeStep } from "../api/types";

const POLL_MS = 2000;

/** Fields worth probing. `flows` leads because it is the one fq_codel parameter that makes
 *  dummynet allocate per-scheduler state at configure time — the prime suspect. */
const FIELDS = ["flows", "limit", "quantum", "target", "interval", "ecn"] as const;

function gapChip(ms: number | null | undefined) {
  if (ms === null || ms === undefined) return <Chip size="small" label="no data" />;
  if (ms <= 0) return <Chip size="small" color="success" label="clean" />;
  return (
    <Chip
      size="small"
      color={ms >= 2000 ? "error" : "warning"}
      label={ms >= 1000 ? `${(ms / 1000).toFixed(1)}s gap` : `${Math.round(ms)}ms gap`}
    />
  );
}

function StepRow({ step }: { step: WriteProbeStep }) {
  const fw = step.targets?.firewall;
  const th = step.targets?.through;
  return (
    <Box sx={{ py: 0.75, borderBottom: 1, borderColor: "divider" }}>
      <Stack direction={{ xs: "column", sm: "row" }} spacing={1} alignItems={{ sm: "center" }}>
        <Typography variant="body2" sx={{ fontWeight: 600, flex: 1, minWidth: 0 }}>
          {step.label}
          {step.action_ms > 50 && (
            <Typography component="span" variant="caption" sx={{ ml: 1, opacity: 0.7 }}>
              call took {(step.action_ms / 1000).toFixed(1)}s
            </Typography>
          )}
        </Typography>
        <Stack direction="row" spacing={0.5} alignItems="center">
          <Tooltip title="The firewall's own address — if this gaps, the box itself went away">
            <Typography variant="caption" sx={{ opacity: 0.7 }}>box</Typography>
          </Tooltip>
          {gapChip(fw?.worst_gap_ms)}
          <Tooltip title="Through the firewall — if this gaps but the box answers, forwarding broke">
            <Typography variant="caption" sx={{ opacity: 0.7, ml: 1 }}>through</Typography>
          </Tooltip>
          {gapChip(th?.worst_gap_ms)}
        </Stack>
      </Stack>
      {step.failed && (
        <Typography variant="caption" color="error" sx={{ display: "block", mt: 0.25 }}>
          {step.failed}
        </Typography>
      )}
    </Box>
  );
}

export default function WriteAndPing({
  pipes,
  onLoadPipes,
  loadingPipes,
}: {
  pipes: FqCodelPipe[];
  onLoadPipes: () => void | Promise<void>;
  loadingPipes?: boolean;
}) {
  const [probe, setProbe] = useState<WriteProbe | null>(null);
  const [recent, setRecent] = useState<WriteProbe[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const [pipeUuid, setPipeUuid] = useState("");
  const [field, setField] = useState<string>("flows");
  const [value, setValue] = useState("4096");
  const [firewallTarget, setFirewallTarget] = useState("");
  const [throughTarget, setThroughTarget] = useState("1.1.1.1");
  const timer = useRef<number | null>(null);

  const poll = useCallback(async () => {
    try {
      const s = await api.writeProbeStatus();
      setProbe(s.current);
      setRecent(s.recent ?? []);
    } catch {
      /* a diagnostic must never be why the page fails */
    }
  }, []);

  useEffect(() => {
    void poll();
    timer.current = window.setInterval(() => void poll(), POLL_MS);
    return () => {
      if (timer.current) window.clearInterval(timer.current);
    };
  }, [poll]);

  // A pipe's uuid and description live in `extra` — the read model keeps the shaper
  // fields first-class and the firewall's own bookkeeping beside them.
  const pipeId = (p: FqCodelPipe) => String(p.extra?.uuid ?? "");
  const pipeName = (p: FqCodelPipe) =>
    String(p.extra?.description ?? p.extra?.pipe ?? p.extra?.uuid ?? "pipe");

  useEffect(() => {
    if (!pipeUuid && pipes.length) setPipeUuid(pipeId(pipes[0]));
  }, [pipes, pipeUuid]);

  const running = probe?.status === "running";

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.startWriteProbe({
        changes: [{ pipe_uuid: pipeUuid || null, param: field, value: Number(value) || value }],
        firewall_target: firewallTarget.trim(),
        through_target: throughTarget.trim() || "1.1.1.1",
      });
      await poll();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const shown = probe ?? recent[0] ?? null;

  return (
    <Card sx={{ mb: 2 }}>
      <CardContent>
        <Typography variant="h6" gutterBottom>
          Write and ping
        </Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
          Writes one field to the firewall with ping running throughout, then puts it back.
          The two halves of a write are issued <b>separately</b> — setting the fields, then
          reloading the shaper — so the cost lands on whichever one causes it. Two targets,
          because <i>the box stopped answering</i> and <i>traffic stopped flowing</i> are
          different failures: only the second is what a queue rebuild looks like.
        </Typography>

        {pipes.length === 0 && (
          <Alert severity="info" sx={{ mb: 2 }}>
            The shaper pipes aren't loaded yet — this needs to read them from the firewall
            before it can write one.
            <Button
              size="small"
              sx={{ ml: 1 }}
              disabled={loadingPipes}
              onClick={() => void onLoadPipes()}
            >
              {loadingPipes ? "Reading…" : "Read the pipes"}
            </Button>
          </Alert>
        )}

        <Stack direction={{ xs: "column", sm: "row" }} spacing={1.5} sx={{ mb: 2 }} flexWrap="wrap">
          <TextField
            select size="small" label="Pipe" value={pipeUuid} disabled={pipes.length === 0}
            sx={{ minWidth: 180 }}
            onChange={(e) => setPipeUuid(e.target.value)}
          >
            {pipes.map((p) => (
              <MenuItem key={pipeId(p)} value={pipeId(p)}>
                {pipeName(p)}
              </MenuItem>
            ))}
          </TextField>
          <TextField
            select size="small" label="Field" value={field} sx={{ minWidth: 130 }}
            onChange={(e) => setField(e.target.value)}
          >
            {FIELDS.map((f) => (
              <MenuItem key={f} value={f}>{f}</MenuItem>
            ))}
          </TextField>
          <TextField
            size="small" label="Value" value={value} sx={{ width: 120 }}
            onChange={(e) => setValue(e.target.value)}
          />
          <TextField
            size="small" label="Firewall IP" value={firewallTarget} sx={{ width: 160 }}
            placeholder="192.168.1.1"
            onChange={(e) => setFirewallTarget(e.target.value)}
          />
          <TextField
            size="small" label="Ping through" value={throughTarget} sx={{ width: 150 }}
            onChange={(e) => setThroughTarget(e.target.value)}
          />
        </Stack>

        <Stack direction="row" spacing={1} sx={{ mb: 2 }} flexWrap="wrap" useFlexGap>
          <Button
            variant="contained" onClick={() => void run()}
            disabled={busy || running || !firewallTarget.trim() || !pipeUuid}
          >
            Run write and ping
          </Button>
          {running && (
            <Button color="inherit" onClick={() => void api.cancelWriteProbe().then(poll)}>
              Stop
            </Button>
          )}
          <Typography variant="caption" color="text.secondary" sx={{ alignSelf: "center" }}>
            ~1 minute. Applies a real change and restores it. Writes must be armed.
          </Typography>
        </Stack>

        {error && (
          <Alert severity="warning" sx={{ mb: 2 }} onClose={() => setError(null)}>
            {error}
          </Alert>
        )}

        {running && (
          <>
            <LinearProgress sx={{ mb: 1 }} />
            <Typography variant="caption" sx={{ display: "block", mb: 1 }}>
              {probe?.stage}
            </Typography>
          </>
        )}

        {shown && (shown.steps?.length ?? 0) > 0 && (
          <Box>
            {shown.steps!.map((s) => (
              <StepRow key={s.step} step={s} />
            ))}
            {shown.verdict && (
              <Alert
                severity={
                  (shown.steps ?? []).some(
                    (s) => (s.targets?.firewall?.worst_gap_ms ?? 0) > 0,
                  )
                    ? "error"
                    : (shown.steps ?? []).some((s) => (s.targets?.through?.worst_gap_ms ?? 0) > 0)
                      ? "warning"
                      : "success"
                }
                sx={{ mt: 1.5 }}
              >
                {shown.verdict}
              </Alert>
            )}
            {shown.error && (
              <Typography variant="caption" color="error" sx={{ display: "block", mt: 1 }}>
                {shown.error}
              </Typography>
            )}
          </Box>
        )}
      </CardContent>
    </Card>
  );
}
