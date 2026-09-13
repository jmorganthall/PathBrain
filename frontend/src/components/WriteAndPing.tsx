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
import Divider from "@mui/material/Divider";
import FormControlLabel from "@mui/material/FormControlLabel";
import LinearProgress from "@mui/material/LinearProgress";
import MenuItem from "@mui/material/MenuItem";
import Stack from "@mui/material/Stack";
import Switch from "@mui/material/Switch";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import { api } from "../api/client";
import type {
  FqCodelPipe,
  WriteProbe,
  WriteProbeStep,
  WriteSweepPlan,
} from "../api/types";

const POLL_MS = 2000;

// No hardcoded field list. It had drifted from the registry (six entries, no bandwidth) and
// nothing tied it to the value box, so switching the field left the previous field's number
// behind — which is how `ecn` came to be offered a value of 4096. The server proposes a
// minimal step per field from the live pipe, and the box follows the dropdown.
//
// The list this replaces led with `flows`, on the reasoning that it is the one fq_codel
// parameter making dummynet allocate per-scheduler state at configure time. That reasoning
// was right and the conclusion was backwards: it makes `flows` the field nothing may touch.
// Measured — setting it was free and putting it back took 35.3s, timing out the call and
// taking the box off the network for 33s — and the
// write ledger then showed the same cost on every ordinary profile switch whose diff
// happened to contain it. So the field is no longer writable at all (`shaper_fields`): it is
// captured on every run and never changed, which takes it out of the server's proposals and
// therefore out of this dropdown, with nothing here to remember.

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
  const [field, setField] = useState<string>("quantum");
  const [value, setValue] = useState("");
  const [plan, setPlan] = useState<WriteSweepPlan | null>(null);
  const [sweepFields, setSweepFields] = useState<string[] | null>(null);
  const [sweepReload, setSweepReload] = useState(false);
  const [touched, setTouched] = useState(false);
  const [firewallTarget, setFirewallTarget] = useState("");
  const [throughTarget, setThroughTarget] = useState("1.1.1.1");
  const timer = useRef<number | null>(null);

  const prefilled = useRef(false);

  const poll = useCallback(async () => {
    try {
      const s = await api.writeProbeStatus();
      setProbe(s.current);
      setRecent(s.recent ?? []);
      // PathBrain already talks to this firewall, so fill its address in rather than
      // asking for it. Once only, so it can never overwrite what someone is typing.
      if (!prefilled.current && s.defaults?.firewall_target) {
        prefilled.current = true;
        setFirewallTarget((cur) => cur || s.defaults!.firewall_target!);
      }
    } catch (e) {
      // Never silent. This swallow is exactly why "I hit the test and nothing came
      // back" had no explanation anywhere on the page.
      setError(e instanceof Error ? e.message : String(e));
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

  // The plan is read-only — it discovers the firewall and prices a sweep, it writes nothing —
  // and it carries the per-field proposals the single probe's value box reads.
  useEffect(() => {
    let live = true;
    void api
      .writeSweepPreview({ pipe_uuid: pipeUuid || undefined, reload: !sweepReload ? true : false })
      .then((p) => live && setPlan(p))
      .catch(() => live && setPlan(null));
    return () => {
      live = false;
    };
  }, [pipeUuid, sweepReload]);

  // Switching the field proposes that field's own step. A value the user typed is never
  // overwritten — the fix is for the box to stop lying, not to fight whoever is using it.
  const proposal = plan?.proposals?.[field];
  useEffect(() => {
    if (!touched && proposal) setValue(String(proposal.to));
  }, [proposal, touched]);

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

  const runSweep = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.startWriteSweep({
        pipe_uuid: pipeUuid || null,
        fields: sweepFields,
        reload: !sweepReload,
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
            onChange={(e) => {
              setField(e.target.value);
              setTouched(false);
            }}
          >
            {Object.values(plan?.proposals ?? {}).map((f) => (
              <MenuItem key={f.param} value={f.param}>{f.param}</MenuItem>
            ))}
          </TextField>
          <TextField
            size="small" label="Value" value={value} sx={{ width: 150 }}
            helperText={proposal ? `now ${String(proposal.from)} · ${proposal.how}` : " "}
            onChange={(e) => {
              setValue(e.target.value);
              setTouched(true);
            }}
          />
          <TextField
            size="small" label="Firewall" value={firewallTarget} sx={{ width: 160 }}
            placeholder="from your provider config"
            helperText={firewallTarget ? "from your provider config" : "none configured"}
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

        <Divider sx={{ my: 2 }} />

        <Typography variant="subtitle2" gutterBottom>
          Sweep every field
        </Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 1.5 }}>
          Steps each field by the smallest write that is still a write — <b>+1</b>, a toggle,
          or the next value the firewall's own option list allows — puts it straight back, and
          measures the gap after every step. Exactly one field is ever away from its original
          value. The flow table is deliberately left out: changing it rebuilds every queue
          rather than re-reading a parameter, which on this link took the box off the network
          for 33 seconds.
        </Typography>

        <Stack direction="row" spacing={1} sx={{ mb: 1 }} flexWrap="wrap" useFlexGap>
          {(plan?.all_fields ?? []).map((f) => {
            const on = sweepFields === null || sweepFields.includes(f);
            return (
              <Chip
                key={f}
                size="small"
                label={f}
                color={on ? "primary" : "default"}
                variant={on ? "filled" : "outlined"}
                onClick={() => {
                  const base = sweepFields ?? (plan?.all_fields ?? []);
                  setSweepFields(on ? base.filter((x) => x !== f) : [...base, f]);
                }}
              />
            );
          })}
        </Stack>

        <FormControlLabel
          control={
            <Switch
              size="small"
              checked={sweepReload}
              onChange={(e: React.ChangeEvent<HTMLInputElement>) => setSweepReload(e.target.checked)}
            />
          }
          label={
            <Typography variant="caption">
              Cheap pass — write and revert each field with <b>no shaper reload</b>. Costs zero
              reconfigures, so it is exempt from the hourly budget and the pacing gap, and it
              answers whether a bare write costs anything at all before you spend the reloads.
            </Typography>
          }
          sx={{ alignItems: "flex-start", mb: 1 }}
        />

        {plan && (
          <Alert severity={plan.blocked ? "warning" : "info"} sx={{ mb: 1 }}>
            {plan.blocked ? (
              plan.blocked
            ) : (
              <>
                <b>
                  {plan.steps.length} field{plan.steps.length === 1 ? "" : "s"} ·{" "}
                  {plan.reconfigures} reconfigure{plan.reconfigures === 1 ? "" : "s"} · about{" "}
                  {Math.round(plan.seconds / 60)} min
                </b>
                {plan.reconfigures > 0 && (
                  <> — most of it spent settling, and some of it as no internet.</>
                )}
                {!!plan.skipped.length && (
                  <Typography variant="caption" sx={{ display: "block", mt: 0.5 }}>
                    Not stepped: {plan.skipped.map((s) => `${s.param} (${s.why})`).join("; ")}
                  </Typography>
                )}
              </>
            )}
          </Alert>
        )}

        <Stack direction="row" spacing={1} sx={{ mb: 2 }} flexWrap="wrap" useFlexGap>
          <Button
            variant="outlined"
            onClick={() => void runSweep()}
            disabled={
              busy || running || !!plan?.blocked || !plan?.steps.length ||
              !firewallTarget.trim() || !pipeUuid
            }
          >
            Sweep {sweepFields === null ? "every field" : `${sweepFields.length} field(s)`}
          </Button>
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

        {!shown && !running && (
          <Typography variant="body2" color="text.secondary">
            No probe has been run yet. The result appears here — one row per operation,
            with what each cost on both targets.
          </Typography>
        )}

        {shown && (
          <Box>
            <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
              <Chip
                size="small"
                label={shown.status}
                color={
                  shown.status === "complete" ? "success"
                    : shown.status === "failed" ? "error" : "default"
                }
              />
              <Typography variant="caption" color="text.secondary">
                probe #{shown.id}
                {shown.changes?.[0] &&
                  ` · ${shown.changes[0].param} → ${String(shown.changes[0].value)}`}
                {shown.firewall_target ? ` · pinging ${shown.firewall_target}` : ""}
              </Typography>
            </Stack>

            {/* A failure before the first step has no timeline at all, and its reason is
                the only thing worth showing — so it renders above the steps, not inside
                a block that exists only when there are steps. */}
            {shown.error && (
              <Alert severity="error" sx={{ mb: 1 }}>
                {shown.error}
              </Alert>
            )}
            {(shown.steps?.length ?? 0) === 0 && !shown.error && !running && (
              <Typography variant="body2" color="text.secondary">
                This probe recorded no steps and gave no reason — that is a bug worth
                reporting, not a result.
              </Typography>
            )}

            {(shown.steps ?? []).map((s) => (
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
          </Box>
        )}
      </CardContent>
    </Card>
  );
}
