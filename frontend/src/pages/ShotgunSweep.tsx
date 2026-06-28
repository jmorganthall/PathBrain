import { useCallback, useEffect, useRef, useState } from "react";
import { Link as RouterLink } from "react-router-dom";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import FormControlLabel from "@mui/material/FormControlLabel";
import LinearProgress from "@mui/material/LinearProgress";
import Snackbar from "@mui/material/Snackbar";
import Stack from "@mui/material/Stack";
import Switch from "@mui/material/Switch";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import TextField from "@mui/material/TextField";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import StopIcon from "@mui/icons-material/Stop";

import { api } from "../api/client";
import type {
  Sweep,
  SweepField,
  SweepParamRange,
  SweepPipe,
  SweepPreview,
  SweepResult,
  SweepSpec,
} from "../api/types";
import { fmtDuration } from "../utils/format";
import { sopsColor } from "../theme";

const isActive = (s: Sweep | null) => !!s && ["pending", "running"].includes(s.status);

function Num({
  label,
  value,
  onChange,
  disabled,
  width = 96,
  step = 1,
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  disabled?: boolean;
  width?: number;
  step?: number;
}) {
  return (
    <TextField
      size="small"
      label={label}
      type="number"
      value={Number.isFinite(value) ? value : ""}
      onChange={(e) => onChange(parseFloat(e.target.value))}
      disabled={disabled}
      sx={{ width }}
      inputProps={{ step }}
    />
  );
}

function ParamCard({
  title,
  unit,
  range,
  onChange,
  hint,
}: {
  title: string;
  unit: string;
  range: SweepParamRange;
  onChange: (r: SweepParamRange) => void;
  hint?: string;
}) {
  return (
    <Card variant="outlined" sx={{ flex: 1, minWidth: 280 }}>
      <CardContent>
        <FormControlLabel
          control={
            <Switch checked={range.enabled} onChange={(e) => onChange({ ...range, enabled: e.target.checked })} />
          }
          label={<Typography sx={{ fontWeight: 600 }}>{title}</Typography>}
        />
        <Stack direction="row" spacing={1} sx={{ mt: 1 }}>
          <Num label={`Min${unit}`} value={range.min} disabled={!range.enabled} onChange={(v) => onChange({ ...range, min: v })} />
          <Num label={`Max${unit}`} value={range.max} disabled={!range.enabled} onChange={(v) => onChange({ ...range, max: v })} />
          <Num label="Step" value={range.step} disabled={!range.enabled} onChange={(v) => onChange({ ...range, step: v })} />
        </Stack>
        {hint && (
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>
            {hint}
          </Typography>
        )}
      </CardContent>
    </Card>
  );
}

function VsTypical({ r }: { r: SweepResult }) {
  const rel = r.relative;
  if (!rel || rel.delta == null) {
    return (
      <Typography component="span" variant="caption" color="text.secondary">
        —
      </Typography>
    );
  }
  const d = rel.delta;
  const neutral = rel.band === "typical" || Math.abs(d) < 0.5;
  const color = neutral ? "text.secondary" : d > 0 ? "success.main" : "error.main";
  return (
    <Tooltip
      arrow
      title={`vs the historical norm for when it ran${rel.percentile != null ? ` · ${rel.percentile}th pct` : ""}`}
    >
      <Typography component="span" sx={{ color, fontWeight: 600, cursor: "help" }}>
        {d > 0 ? "+" : ""}
        {d}
      </Typography>
    </Tooltip>
  );
}

export default function ShotgunSweep() {
  const [mtu, setMtu] = useState(1500);
  // The sweepable fields (from the registry) and a range per field, keyed by field key.
  // Both are populated from /sweep/fields, so marking a field sweepable in the backend
  // automatically gives it a control here — no hardcoded quantum/target.
  const [fields, setFields] = useState<SweepField[]>([]);
  const [ranges, setRanges] = useState<Record<string, SweepParamRange>>({});
  const [iterations, setIterations] = useState(2);
  const [dwellMinutes, setDwellMinutes] = useState(0);
  const [dryRun, setDryRun] = useState(false);

  const [preview, setPreview] = useState<SweepPreview | null>(null);
  const [sweep, setSweep] = useState<Sweep | null>(null);
  const [pipes, setPipes] = useState<SweepPipe[]>([]);
  const [selectedPipes, setSelectedPipes] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const pollRef = useRef<number | null>(null);

  useEffect(() => {
    api.sweepPipes().then((r) => setPipes(r.pipes)).catch(() => setPipes([]));
    api
      .sweepFields()
      .then((r) => {
        setFields(r.fields);
        setRanges(Object.fromEntries(r.fields.map((f) => [f.key, { ...f.default }])));
      })
      .catch(() => setFields([]));
  }, []);

  const setRange = useCallback((key: string, r: SweepParamRange) => {
    setRanges((prev) => ({ ...prev, [key]: r }));
  }, []);

  const chosenPipes = pipes.filter((p) => selectedPipes.includes(p.uuid));
  const spec: SweepSpec = { ...ranges, ...(chosenPipes.length ? { pipes: chosenPipes } : {}) };
  const rangesKey = JSON.stringify(ranges);

  // Live variant count + ETA (debounced).
  useEffect(() => {
    const h = window.setTimeout(() => {
      api
        .sweepPreview({ spec, iterations, dwell_minutes: dwellMinutes })
        .then(setPreview)
        .catch(() => setPreview(null));
    }, 300);
    return () => window.clearTimeout(h);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rangesKey, iterations, dwellMinutes, selectedPipes]);

  const poll = useCallback(() => {
    if (pollRef.current) window.clearInterval(pollRef.current);
    pollRef.current = window.setInterval(async () => {
      try {
        const { sweep: s } = await api.sweepCurrent();
        setSweep(s);
        if (!isActive(s)) {
          if (pollRef.current) window.clearInterval(pollRef.current);
          pollRef.current = null;
        }
      } catch {
        /* keep polling */
      }
    }, 2000);
  }, []);

  useEffect(() => {
    api
      .sweepCurrent()
      .then(({ sweep: s }) => {
        setSweep(s);
        if (isActive(s)) poll();
      })
      .catch(() => {});
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
    };
  }, [poll]);

  const handleStart = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const s = await api.startSweep({ spec, iterations, dwell_minutes: dwellMinutes, dry_run: dryRun });
      setSweep(s);
      poll();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to start sweep");
    } finally {
      setBusy(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [rangesKey, iterations, dwellMinutes, dryRun, poll]);

  const handleCancel = useCallback(async () => {
    if (!sweep) return;
    try {
      await api.cancelSweep(sweep.id);
      setToast("Cancelling — restoring the original config…");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to cancel");
    }
  }, [sweep]);

  const handleApplyBest = useCallback(async () => {
    if (!sweep) return;
    try {
      const r = await api.applySweepBest(sweep.id);
      setToast(`Applied winner: ${JSON.stringify(r.applied)} (SOPS ${r.sops ?? "—"})`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to apply winner");
    }
  }, [sweep]);

  const running = isActive(sweep);
  const halfMtu = Math.round(mtu / 2);
  const variantsOverCap = preview != null && preview.total_variants > preview.cap;

  return (
    <Box>
      <Stack
        direction={{ xs: "column", sm: "row" }}
        justifyContent="space-between"
        alignItems={{ xs: "flex-start", sm: "center" }}
        spacing={2}
        sx={{ mb: 1 }}
      >
        <Typography variant="h4">Shotgun Sweep</Typography>
        <Stack direction="row" spacing={1} alignItems="center">
          {running ? (
            <Button color="warning" variant="outlined" startIcon={<StopIcon />} onClick={handleCancel}>
              Cancel
            </Button>
          ) : (
            <Button
              variant="contained"
              startIcon={<PlayArrowIcon />}
              onClick={handleStart}
              disabled={busy || !preview || preview.total_variants === 0 || variantsOverCap}
            >
              {busy ? "Starting…" : "Start sweep"}
            </Button>
          )}
        </Stack>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2, maxWidth: 760 }}>
        Run a broad experiment quickly: sweep shaper values across a grid, benchmarking each with the
        normal suite, then ranked by SOPS and how each did <em>vs. its historical norm</em>. The
        original config is restored when the sweep ends. Sweep runs appear in History tagged{" "}
        <code>sweep · …</code>.
      </Typography>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {!dryRun && (
        <Alert severity="warning" sx={{ mb: 2 }}>
          This applies real changes to your firewall for each variant (then restores the original).
          Toggle <b>Dry-run</b> to rehearse without writing.
        </Alert>
      )}

      {/* Configuration */}
      <Card sx={{ mb: 2 }}>
        <CardContent>
          {pipes.length > 0 && (
            <Box sx={{ mb: 2 }}>
              <Typography sx={{ fontWeight: 600, mb: 0.5 }}>Pipes to sweep</Typography>
              <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
                {pipes.map((p) => {
                  const on = selectedPipes.includes(p.uuid);
                  return (
                    <Chip
                      key={p.uuid}
                      label={p.label}
                      color={on ? "primary" : "default"}
                      variant={on ? "filled" : "outlined"}
                      onClick={() =>
                        setSelectedPipes((s) =>
                          on ? s.filter((x) => x !== p.uuid) : [...s, p.uuid],
                        )
                      }
                    />
                  );
                })}
              </Stack>
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 0.5 }}>
                {selectedPipes.length === 0
                  ? "None selected → sweeps the default (download) pipe."
                  : `Each parameter value is tried on each selected pipe, one at a time — ${selectedPipes.length} pipe(s) × the grid.`}
              </Typography>
            </Box>
          )}
          <Stack direction={{ xs: "column", md: "row" }} spacing={2} flexWrap="wrap" useFlexGap>
            {fields.map((f) => {
              const range = ranges[f.key];
              if (!range) return null;
              return (
                <ParamCard
                  key={f.key}
                  title={f.label}
                  unit={f.unit ? ` (${f.unit})` : ""}
                  range={range}
                  onChange={(r) => setRange(f.key, r)}
                  hint={
                    f.key === "quantum"
                      ? `Bytes per scheduler turn. Step defaults to ½ MTU = ${halfMtu}.`
                      : undefined
                  }
                />
              );
            })}
          </Stack>
          <Stack direction="row" spacing={2} sx={{ mt: 2 }} flexWrap="wrap" useFlexGap alignItems="center">
            {ranges.quantum && (
              <Num label="MTU" value={mtu} width={100} onChange={(v) => {
                setMtu(v);
                setRange("quantum", { ...ranges.quantum, step: Math.round(v / 2) });
              }} />
            )}
            <Num label="Iterations / variant" value={iterations} width={160} onChange={(v) => setIterations(Math.max(1, Math.round(v)))} />
            <Num label="Dwell (min)" value={dwellMinutes} width={120} step={0.5} onChange={(v) => setDwellMinutes(Math.max(0, v))} />
            <FormControlLabel
              control={<Switch checked={dryRun} onChange={(e) => setDryRun(e.target.checked)} />}
              label="Dry-run"
            />
          </Stack>
          <Box sx={{ mt: 2 }}>
            {preview ? (
              <Typography variant="body2">
                <b>{preview.total_variants}</b> variant{preview.total_variants === 1 ? "" : "s"}
                {variantsOverCap && (
                  <Typography component="span" color="error.main" sx={{ ml: 1 }}>
                    (over the {preview.cap} cap — narrow the range)
                  </Typography>
                )}
                {preview.eta_ms != null ? (
                  <Typography component="span" color="text.secondary" sx={{ ml: 1 }}>
                    · ETA ~{fmtDuration(preview.eta_ms)}
                  </Typography>
                ) : (
                  <Typography component="span" color="text.secondary" sx={{ ml: 1 }}>
                    · ETA available after the first benchmark run
                  </Typography>
                )}
              </Typography>
            ) : (
              <Typography variant="body2" color="text.secondary">
                Enable at least one parameter with a valid range.
              </Typography>
            )}
          </Box>
        </CardContent>
      </Card>

      {/* Progress + results */}
      {sweep && (
        <Card>
          <CardContent>
            <Stack direction="row" justifyContent="space-between" alignItems="center" sx={{ mb: 1 }} flexWrap="wrap" useFlexGap>
              <Stack direction="row" spacing={1} alignItems="center">
                <Typography variant="h6">Sweep #{sweep.id}</Typography>
                <Chip
                  size="small"
                  color={
                    sweep.status === "complete"
                      ? "success"
                      : sweep.status === "running" || sweep.status === "pending"
                        ? "info"
                        : sweep.status === "failed"
                          ? "error"
                          : "default"
                  }
                  label={sweep.status}
                />
                {sweep.dry_run && <Chip size="small" variant="outlined" label="dry-run" />}
              </Stack>
              {sweep.results.length > 0 && !running && (
                <Tooltip title="Apply the top-ranked variant's swept field values to the firewall.">
                  <span>
                    <Button size="small" variant="outlined" onClick={handleApplyBest} disabled={sweep.dry_run}>
                      Apply winner
                    </Button>
                  </span>
                </Tooltip>
              )}
            </Stack>

            {running && (
              <Box sx={{ mb: 1.5 }}>
                <LinearProgress
                  variant="determinate"
                  value={sweep.total_variants ? (sweep.completed_variants / sweep.total_variants) * 100 : 0}
                />
                <Typography variant="caption" color="text.secondary">
                  Variant {Math.min(sweep.completed_variants + 1, sweep.total_variants)} of {sweep.total_variants}…
                </Typography>
              </Box>
            )}
            {sweep.error && (
              <Alert severity="error" sx={{ mb: 1 }}>
                {sweep.error}
              </Alert>
            )}
            {sweep.baseline && (
              <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1 }}>
                Baseline (restored at end):{" "}
                {fields
                  .map((f) => `${f.label.toLowerCase()} ${String(sweep.baseline?.[f.key] ?? "—")}`)
                  .join(" · ")}
              </Typography>
            )}

            {sweep.results.length === 0 ? (
              <Typography variant="body2" color="text.secondary">
                No results yet.
              </Typography>
            ) : (
              <TableContainer>
                <Table size="small">
                  <TableHead>
                    <TableRow>
                      <TableCell>Rank</TableCell>
                      <TableCell>Pipe</TableCell>
                      {fields.map((f) => (
                        <TableCell key={f.key} align="right">{f.label}</TableCell>
                      ))}
                      <TableCell align="right">SOPS</TableCell>
                      <TableCell align="right">vs typical</TableCell>
                      <TableCell>Run</TableCell>
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {sweep.results.map((r, i) => (
                      <TableRow key={r.index} selected={i === 0 && r.sops != null}>
                        <TableCell>{i + 1}</TableCell>
                        <TableCell>{r.pipe_label ?? "—"}</TableCell>
                        {fields.map((f) => (
                          <TableCell key={f.key} align="right">
                            {(r[f.key] as number | string | null) ?? "—"}
                          </TableCell>
                        ))}
                        <TableCell align="right" sx={{ fontWeight: 700, color: sopsColor(r.sops) }}>
                          {r.sops != null ? r.sops.toFixed(1) : "—"}
                        </TableCell>
                        <TableCell align="right">
                          <VsTypical r={r} />
                        </TableCell>
                        <TableCell>
                          {r.run_id ? (
                            <Chip
                              size="small"
                              variant="outlined"
                              component={RouterLink}
                              to={`/runs/${r.run_id}`}
                              clickable
                              label={`#${r.run_id}`}
                            />
                          ) : (
                            "—"
                          )}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableContainer>
            )}
          </CardContent>
        </Card>
      )}

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
