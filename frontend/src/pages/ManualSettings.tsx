// Manual Settings — a human-friendly editor for the firewall shaper pipes.
//
// This is a deliberate manual firewall-WRITE path (the experiment engine is the
// other). It is guarded server-side: the apply endpoint snapshots the live
// config first and refuses to write while an experiment is running. Here we
// surface those guards, only send fields the user actually changed, and require
// a confirm step before anything hits the firewall.
import { useCallback, useEffect, useMemo, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogContentText from "@mui/material/DialogContentText";
import DialogTitle from "@mui/material/DialogTitle";
import Divider from "@mui/material/Divider";
import FormControlLabel from "@mui/material/FormControlLabel";
import Snackbar from "@mui/material/Snackbar";
import Stack from "@mui/material/Stack";
import Switch from "@mui/material/Switch";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import RefreshIcon from "@mui/icons-material/Refresh";
import SaveIcon from "@mui/icons-material/Save";
import UndoIcon from "@mui/icons-material/Undo";

import { api, ApiError } from "../api/client";
import type { FqCodelPipe } from "../api/types";

// Editable draft for one pipe. Numbers are kept as strings while editing so the
// fields can be cleared/typed freely; they're parsed on apply.
interface PipeDraft {
  bandwidth: string;
  quantum: string;
  limit: string;
  flows: string;
  target: string;
  interval: string;
  ecn: boolean;
}

interface FieldMeta {
  key: keyof PipeDraft;
  label: string;
  kind: "number" | "text" | "bool";
  help: string;
  placeholder?: string;
}

// Human-friendly descriptions for each shaper knob.
const FIELDS: FieldMeta[] = [
  {
    key: "bandwidth",
    label: "Bandwidth",
    kind: "text",
    help: "The shaped rate for this pipe, e.g. 900Mbit or 40Mbit. Set just under your real line rate to keep the queue under PathBrain's control.",
    placeholder: "900Mbit",
  },
  {
    key: "quantum",
    label: "Quantum (bytes)",
    kind: "number",
    help: "Bytes a flow may send per turn. ~1514 (one packet) is typical; smaller favours latency-sensitive flows.",
    placeholder: "1514",
  },
  {
    key: "limit",
    label: "Queue limit (packets)",
    kind: "number",
    help: "Max packets buffered before tail-drop. Larger absorbs bursts but can add bloat.",
    placeholder: "10240",
  },
  {
    key: "flows",
    label: "Flows",
    kind: "number",
    help: "Number of FQ-CoDel hash buckets. More flows = finer per-flow fairness.",
    placeholder: "1024",
  },
  {
    key: "target",
    label: "CoDel target",
    kind: "text",
    help: "Acceptable standing queue delay before CoDel starts dropping, e.g. 5ms.",
    placeholder: "5ms",
  },
  {
    key: "interval",
    label: "CoDel interval",
    kind: "text",
    help: "Window CoDel uses to assess standing delay, e.g. 100ms (~ typical RTT).",
    placeholder: "100ms",
  },
  {
    key: "ecn",
    label: "ECN",
    kind: "bool",
    help: "Mark packets instead of dropping when supported. Usually on for fq_codel.",
  },
];

function pipeLabel(pipe: FqCodelPipe, idx: number): string {
  const extra = pipe.extra ?? {};
  const candidate =
    (extra.description as string) || (extra.pipe as string) || (extra.direction as string);
  return candidate || `Pipe ${idx + 1}`;
}

function pipeKey(pipe: FqCodelPipe, idx: number): string {
  return (pipe.extra?.uuid as string) || `idx-${idx}`;
}

function toDraft(pipe: FqCodelPipe): PipeDraft {
  const numStr = (v: number | null) => (v === null || v === undefined ? "" : String(v));
  return {
    // The provider stores a pipe's shaped rate on download_bandwidth.
    bandwidth: pipe.download_bandwidth ?? "",
    quantum: numStr(pipe.quantum),
    limit: numStr(pipe.limit),
    flows: numStr(pipe.flows),
    target: pipe.target ?? "",
    interval: pipe.interval ?? "",
    ecn: Boolean(pipe.ecn),
  };
}

// Build the {param: value} delta of fields that actually changed vs the
// originally-discovered pipe, coercing to the type the backend expects.
function buildChanges(orig: PipeDraft, draft: PipeDraft): Record<string, unknown> {
  const changes: Record<string, unknown> = {};
  for (const f of FIELDS) {
    const a = orig[f.key];
    const b = draft[f.key];
    if (a === b) continue;
    if (f.kind === "number") {
      const n = Number(b);
      if (b === "" || Number.isNaN(n)) continue; // skip blank/invalid
      changes[f.key] = n;
    } else if (f.kind === "bool") {
      changes[f.key] = Boolean(b);
    } else {
      changes[f.key] = String(b).trim();
    }
  }
  return changes;
}

function describeChanges(orig: PipeDraft, changes: Record<string, unknown>): string[] {
  return Object.keys(changes).map((key) => {
    const meta = FIELDS.find((f) => f.key === key);
    const from = orig[key as keyof PipeDraft];
    const fromStr = typeof from === "boolean" ? (from ? "on" : "off") : from || "—";
    const toRaw = changes[key];
    const toStr = typeof toRaw === "boolean" ? (toRaw ? "on" : "off") : String(toRaw);
    return `${meta?.label ?? key}: ${fromStr} → ${toStr}`;
  });
}

export default function ManualSettings() {
  const [pipes, setPipes] = useState<FqCodelPipe[]>([]);
  const [provider, setProvider] = useState<string>("");
  const [originals, setOriginals] = useState<Record<string, PipeDraft>>({});
  const [drafts, setDrafts] = useState<Record<string, PipeDraft>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [experimentActive, setExperimentActive] = useState(false);
  const [applying, setApplying] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<{ key: string; uuid: string | null; lines: string[] } | null>(
    null,
  );

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await api.discover();
      setProvider(res.provider);
      setPipes(res.pipes);
      const origs: Record<string, PipeDraft> = {};
      res.pipes.forEach((p, i) => {
        origs[pipeKey(p, i)] = toDraft(p);
      });
      setOriginals(origs);
      setDrafts(origs);
      // Best-effort: surface whether an experiment currently owns the firewall.
      try {
        const exp = await api.experiments();
        setExperimentActive(exp.status?.active_experiment_id != null);
      } catch {
        setExperimentActive(false);
      }
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Failed to read firewall settings");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const setField = (key: string, field: keyof PipeDraft, value: string | boolean) => {
    setDrafts((prev) => ({ ...prev, [key]: { ...prev[key], [field]: value } }));
  };

  const resetPipe = (key: string) => {
    setDrafts((prev) => ({ ...prev, [key]: originals[key] }));
  };

  const requestApply = (pipe: FqCodelPipe, idx: number) => {
    const key = pipeKey(pipe, idx);
    const changes = buildChanges(originals[key], drafts[key]);
    if (Object.keys(changes).length === 0) {
      setToast("No changes to apply on this pipe");
      return;
    }
    setConfirm({
      key,
      uuid: (pipe.extra?.uuid as string) || null,
      lines: describeChanges(originals[key], changes),
    });
  };

  const doApply = async () => {
    if (!confirm) return;
    const { key, uuid } = confirm;
    const changes = buildChanges(originals[key], drafts[key]);
    setConfirm(null);
    setApplying(key);
    setError(null);
    try {
      const res = await api.applyPipeChanges({ pipe_uuid: uuid, changes });
      const failed = res.results.filter((r) => !r.ok);
      if (failed.length > 0) {
        setError(
          `Applied ${res.applied}/${res.results.length}. Failed: ` +
            failed.map((f) => `${f.param} (${f.detail ?? "error"})`).join(", "),
        );
      } else {
        setToast(`Applied ${res.applied} change${res.applied === 1 ? "" : "s"} to the firewall`);
      }
      await load(); // re-read so originals reflect the live config
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setExperimentActive(true);
        setError(e.message);
      } else {
        setError(e instanceof ApiError ? e.message : "Failed to apply changes");
      }
    } finally {
      setApplying(null);
    }
  };

  const dirtyByKey = useMemo(() => {
    const map: Record<string, number> = {};
    for (const key of Object.keys(drafts)) {
      if (!originals[key]) continue;
      map[key] = Object.keys(buildChanges(originals[key], drafts[key])).length;
    }
    return map;
  }, [drafts, originals]);

  return (
    <Box>
      <Stack
        direction={{ xs: "column", sm: "row" }}
        justifyContent="space-between"
        alignItems={{ sm: "center" }}
        spacing={1}
        sx={{ mb: 2 }}
      >
        <Box>
          <Typography variant="h5" sx={{ fontWeight: 700 }}>
            Manual Settings
          </Typography>
          <Typography variant="body2" color="text.secondary">
            Edit the firewall shaper pipes directly, then apply to test upload/download tuning.
          </Typography>
        </Box>
        <Stack direction="row" spacing={1} alignItems="center">
          {provider && (
            <Chip
              size="small"
              label={`provider: ${provider}`}
              color={provider === "mock" ? "default" : "primary"}
              variant="outlined"
            />
          )}
          <Button startIcon={<RefreshIcon />} onClick={() => void load()} disabled={loading}>
            Reload
          </Button>
        </Stack>
      </Stack>

      {provider === "mock" && (
        <Alert severity="info" sx={{ mb: 2 }}>
          The <strong>mock</strong> provider is active — changes are simulated in-memory, not written
          to a real firewall. Set <code>PATHBRAIN_CONFIG_PROVIDER=opnsense</code> to apply for real.
        </Alert>
      )}

      {experimentActive && (
        <Alert severity="warning" sx={{ mb: 2 }}>
          An experiment is currently running and owns the firewall. Manual changes are blocked until
          it finishes or is aborted (Experiments → Abort).
        </Alert>
      )}

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {loading ? (
        <Box sx={{ display: "flex", justifyContent: "center", py: 6 }}>
          <CircularProgress />
        </Box>
      ) : pipes.length === 0 ? (
        <Alert severity="info">No shaper pipes were discovered from the provider.</Alert>
      ) : (
        <Stack spacing={2}>
          {pipes.map((pipe, idx) => {
            const key = pipeKey(pipe, idx);
            const draft = drafts[key];
            if (!draft) return null;
            const dirty = dirtyByKey[key] ?? 0;
            const busy = applying === key;
            return (
              <Card key={key} variant="outlined">
                <CardContent>
                  <Stack
                    direction="row"
                    justifyContent="space-between"
                    alignItems="center"
                    sx={{ mb: 1.5 }}
                  >
                    <Stack direction="row" spacing={1} alignItems="center">
                      <Typography variant="h6">{pipeLabel(pipe, idx)}</Typography>
                      {pipe.scheduler && (
                        <Chip size="small" label={pipe.scheduler} variant="outlined" />
                      )}
                      {dirty > 0 && (
                        <Chip
                          size="small"
                          color="warning"
                          label={`${dirty} unsaved`}
                          variant="outlined"
                        />
                      )}
                    </Stack>
                    {pipe.extra?.uuid ? (
                      <Typography variant="caption" color="text.secondary">
                        {String(pipe.extra.uuid)}
                      </Typography>
                    ) : null}
                  </Stack>
                  <Divider sx={{ mb: 2 }} />

                  <Box
                    sx={{
                      display: "grid",
                      gridTemplateColumns: { xs: "1fr", sm: "1fr 1fr", md: "1fr 1fr 1fr" },
                      gap: 2,
                    }}
                  >
                    {FIELDS.map((f) =>
                      f.kind === "bool" ? (
                        <Tooltip key={f.key} title={f.help} placement="top-start" arrow>
                          <FormControlLabel
                            control={
                              <Switch
                                checked={draft.ecn}
                                onChange={(e) => setField(key, "ecn", e.target.checked)}
                              />
                            }
                            label={f.label}
                          />
                        </Tooltip>
                      ) : (
                        <TextField
                          key={f.key}
                          label={f.label}
                          type={f.kind === "number" ? "number" : "text"}
                          value={draft[f.key] as string}
                          placeholder={f.placeholder}
                          onChange={(e) => setField(key, f.key, e.target.value)}
                          helperText={f.help}
                          size="small"
                          fullWidth
                        />
                      ),
                    )}
                  </Box>

                  <Stack direction="row" spacing={1} justifyContent="flex-end" sx={{ mt: 2 }}>
                    <Button
                      startIcon={<UndoIcon />}
                      onClick={() => resetPipe(key)}
                      disabled={dirty === 0 || busy}
                    >
                      Reset
                    </Button>
                    <Button
                      variant="contained"
                      startIcon={busy ? <CircularProgress size={16} /> : <SaveIcon />}
                      onClick={() => requestApply(pipe, idx)}
                      disabled={dirty === 0 || busy || experimentActive}
                    >
                      Apply to firewall
                    </Button>
                  </Stack>
                </CardContent>
              </Card>
            );
          })}
        </Stack>
      )}

      <Dialog open={confirm != null} onClose={() => setConfirm(null)}>
        <DialogTitle>Apply these changes to the firewall?</DialogTitle>
        <DialogContent>
          <DialogContentText component="div">
            PathBrain will snapshot the current config, then write:
            <Box component="ul" sx={{ mt: 1, mb: 0 }}>
              {confirm?.lines.map((line) => (
                <li key={line}>
                  <code>{line}</code>
                </li>
              ))}
            </Box>
          </DialogContentText>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirm(null)}>Cancel</Button>
          <Button variant="contained" color="warning" onClick={() => void doApply()}>
            Apply
          </Button>
        </DialogActions>
      </Dialog>

      <Snackbar
        open={toast != null}
        autoHideDuration={4000}
        onClose={() => setToast(null)}
        message={toast ?? ""}
        anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
      />
    </Box>
  );
}
