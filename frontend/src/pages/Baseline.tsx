import { useCallback, useEffect, useRef, useState } from "react";
import { Link as RouterLink } from "react-router-dom";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import Divider from "@mui/material/Divider";
import FormControlLabel from "@mui/material/FormControlLabel";
import LinearProgress from "@mui/material/LinearProgress";
import Link from "@mui/material/Link";
import Snackbar from "@mui/material/Snackbar";
import Stack from "@mui/material/Stack";
import Switch from "@mui/material/Switch";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import PlayArrowIcon from "@mui/icons-material/PlayArrow";
import StopIcon from "@mui/icons-material/Stop";
import SaveIcon from "@mui/icons-material/Save";
import ScheduleIcon from "@mui/icons-material/Schedule";
import PowerOffIcon from "@mui/icons-material/PowerSettingsNew";

import { api } from "../api/client";
import type { BaselineConfig, BaselineTest } from "../api/types";
import { fmtDateTime } from "../utils/format";

const isActive = (t: BaselineTest | null) =>
  !!t && !!t.status && ["pending", "running"].includes(t.status);

const statusColor = (s: string | null | undefined) => {
  switch (s) {
    case "complete":
      return "success" as const;
    case "failed":
      return "error" as const;
    case "cancelled":
      return "warning" as const;
    default:
      return "info" as const;
  }
};

function NumField({
  label,
  value,
  onChange,
  disabled,
  min = 0,
  helper,
  width = 150,
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  disabled?: boolean;
  min?: number;
  helper?: string;
  width?: number;
}) {
  return (
    <TextField
      size="small"
      type="number"
      label={label}
      value={Number.isFinite(value) ? value : ""}
      disabled={disabled}
      helperText={helper}
      onChange={(e) => onChange(Math.max(min, Math.floor(Number(e.target.value) || 0)))}
      sx={{ width }}
      inputProps={{ min }}
    />
  );
}

export default function Baseline() {
  const [cfg, setCfg] = useState<BaselineConfig | null>(null);
  const [saving, setSaving] = useState(false);
  const [savedAt, setSavedAt] = useState<string | null>(null);

  // On-demand run controls (seeded from the saved defaults once loaded).
  const [iterations, setIterations] = useState(10);
  const [settle, setSettle] = useState(30);
  const [seeded, setSeeded] = useState(false);

  const [test, setTest] = useState<BaselineTest | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const pollRef = useRef<number | null>(null);

  const loadConfig = useCallback(async () => {
    try {
      const c = await api.baselineConfig();
      setCfg(c);
      if (!seeded) {
        setIterations(c.iterations);
        setSettle(c.settle_seconds);
        setSeeded(true);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }, [seeded]);

  const loadStatus = useCallback(async () => {
    try {
      const t = await api.baselineTestStatus();
      setTest(t.status ? t : null);
    } catch {
      /* transient */
    }
  }, []);

  useEffect(() => {
    loadConfig();
    loadStatus();
  }, [loadConfig, loadStatus]);

  // Poll the status while a test is active (or just finished) so the stage readout is live.
  const active = isActive(test);
  useEffect(() => {
    if (pollRef.current) window.clearInterval(pollRef.current);
    if (!active) return;
    pollRef.current = window.setInterval(loadStatus, 2000);
    return () => {
      if (pollRef.current) window.clearInterval(pollRef.current);
    };
  }, [active, loadStatus]);

  const patchConfig = (patch: Partial<BaselineConfig>) =>
    setCfg((c) => (c ? { ...c, ...patch } : c));

  const saveConfig = async () => {
    if (!cfg) return;
    setSaving(true);
    setError(null);
    try {
      const saved = await api.baselineConfigSave({
        enabled: cfg.enabled,
        hour: cfg.hour,
        minute: cfg.minute,
        iterations: cfg.iterations,
        settle_seconds: cfg.settle_seconds,
      });
      setCfg(saved);
      setSavedAt(new Date().toISOString());
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const runNow = async () => {
    setBusy(true);
    setError(null);
    try {
      const t = await api.baselineTestStart({ iterations, settle_seconds: settle });
      setTest(t);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const cancel = async () => {
    setBusy(true);
    try {
      await api.baselineTestCancel();
      await loadStatus();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  const progress =
    test && test.iterations
      ? Math.min(100, Math.round((100 * (test.iterations_run || 0)) / test.iterations))
      : 0;

  const hhmm =
    cfg != null
      ? `${String(cfg.hour).padStart(2, "0")}:${String(cfg.minute).padStart(2, "0")}`
      : "01:00";

  return (
    <Box>
      <Stack direction="row" spacing={1.5} alignItems="center" sx={{ mb: 1 }}>
        <PowerOffIcon color="primary" />
        <Typography variant="h4" sx={{ fontWeight: 700 }}>
          Test baseline behavior
        </Typography>
      </Stack>
      <Typography color="text.secondary" sx={{ mb: 3, maxWidth: 760 }}>
        Occasionally measure the link with <strong>SQM turned off</strong> — the honest
        baseline for what your shaper is actually buying. A baseline test disables FQ-CoDel on
        every pipe, waits a settle interval for the link to stabilize, benchmarks the unshaped
        path, then restores each pipe's prior state. Its runs group into their own “SQM off”
        profile, so they never pollute a shaped profile's scores.
      </Typography>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      <Stack direction={{ xs: "column", md: "row" }} spacing={2} sx={{ mb: 2 }} alignItems="stretch">
        {/* Nightly schedule */}
        <Card sx={{ flex: 1 }}>
          <CardContent>
            <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1.5 }}>
              <ScheduleIcon fontSize="small" color="action" />
              <Typography variant="h6">Nightly schedule</Typography>
            </Stack>
            <FormControlLabel
              control={
                <Switch
                  checked={!!cfg?.enabled}
                  onChange={(e) => patchConfig({ enabled: e.target.checked })}
                  disabled={!cfg}
                />
              }
              label={cfg?.enabled ? "Armed — runs automatically" : "Off — manual only"}
            />
            <Stack direction="row" spacing={1.5} sx={{ mt: 1.5 }} flexWrap="wrap" useFlexGap>
              <TextField
                size="small"
                type="time"
                label="Run at (local)"
                value={hhmm}
                disabled={!cfg}
                onChange={(e) => {
                  const [h, m] = e.target.value.split(":").map((x) => parseInt(x, 10));
                  patchConfig({ hour: h || 0, minute: m || 0 });
                }}
                sx={{ width: 150 }}
                InputLabelProps={{ shrink: true }}
              />
              <NumField
                label="Iterations"
                value={cfg?.iterations ?? 10}
                min={1}
                disabled={!cfg}
                onChange={(v) => patchConfig({ iterations: Math.max(1, v) })}
              />
              <NumField
                label="Settle (seconds)"
                value={cfg?.settle_seconds ?? 30}
                disabled={!cfg}
                onChange={(v) => patchConfig({ settle_seconds: v })}
              />
            </Stack>
            <Stack direction="row" spacing={1.5} alignItems="center" sx={{ mt: 2 }}>
              <Button
                variant="contained"
                startIcon={<SaveIcon />}
                onClick={saveConfig}
                disabled={!cfg || saving}
              >
                {saving ? "Saving…" : "Save schedule"}
              </Button>
              {cfg?.enabled && cfg.next_run_at && (
                <Typography variant="body2" color="text.secondary">
                  Next: {fmtDateTime(cfg.next_run_at)}
                </Typography>
              )}
              {savedAt && (
                <Chip size="small" color="success" variant="outlined" label="Saved" />
              )}
            </Stack>
          </CardContent>
        </Card>

        {/* Run now */}
        <Card sx={{ flex: 1 }}>
          <CardContent>
            <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1.5 }}>
              <PlayArrowIcon fontSize="small" color="action" />
              <Typography variant="h6">Run now</Typography>
            </Stack>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
              Kick a baseline test on demand — disables SQM, settles, benchmarks the chosen
              iterations, then restores. Runs in chunks so partial data is kept if interrupted.
            </Typography>
            <Stack direction="row" spacing={1.5} flexWrap="wrap" useFlexGap>
              <NumField
                label="Iterations"
                value={iterations}
                min={1}
                disabled={active}
                onChange={(v) => setIterations(Math.max(1, v))}
              />
              <NumField
                label="Settle (seconds)"
                value={settle}
                disabled={active}
                onChange={setSettle}
              />
            </Stack>
            <Stack direction="row" spacing={1.5} sx={{ mt: 2 }}>
              <Button
                variant="contained"
                color="primary"
                startIcon={<PlayArrowIcon />}
                onClick={runNow}
                disabled={active || busy}
              >
                Run baseline now
              </Button>
              {active && (
                <Button
                  variant="outlined"
                  color="warning"
                  startIcon={<StopIcon />}
                  onClick={cancel}
                  disabled={busy}
                >
                  Cancel
                </Button>
              )}
            </Stack>
          </CardContent>
        </Card>
      </Stack>

      {/* Live / last status */}
      <Card>
        <CardContent>
          <Stack direction="row" spacing={1} alignItems="center" sx={{ mb: 1 }}>
            <Typography variant="h6">{active ? "In progress" : "Latest baseline test"}</Typography>
            {test?.status && (
              <Chip size="small" color={statusColor(test.status)} label={test.status} />
            )}
            {test?.trigger && (
              <Chip size="small" variant="outlined" label={test.trigger} />
            )}
          </Stack>

          {!test && (
            <Typography color="text.secondary">
              No baseline test has run yet. Arm the nightly schedule or run one now.
            </Typography>
          )}

          {test && (
            <Box>
              {active && (
                <LinearProgress
                  variant={test.status === "running" ? "determinate" : "indeterminate"}
                  value={progress}
                  sx={{ mb: 1.5, borderRadius: 1, height: 8 }}
                />
              )}
              <Typography variant="body2" sx={{ mb: 1 }}>
                {test.stage || "—"}
              </Typography>
              <Stack direction="row" spacing={3} flexWrap="wrap" useFlexGap sx={{ mb: 1 }}>
                <Typography variant="body2" color="text.secondary">
                  {test.iterations_run}/{test.iterations} iteration
                  {test.iterations === 1 ? "" : "s"} · {test.runs_created} run
                  {test.runs_created === 1 ? "" : "s"}
                </Typography>
                <Typography variant="body2" color="text.secondary">
                  Settle: {test.settle_s}s
                </Typography>
                {test.started_at && (
                  <Typography variant="body2" color="text.secondary">
                    Started {fmtDateTime(test.started_at)}
                  </Typography>
                )}
                {test.finished_at && (
                  <Typography variant="body2" color="text.secondary">
                    Finished {fmtDateTime(test.finished_at)}
                  </Typography>
                )}
              </Stack>

              {test.lock_owner && active && test.status === "pending" && (
                <Alert severity="info" sx={{ mb: 1 }}>
                  Queued behind <strong>{test.lock_owner}</strong> — it will start once that
                  finishes.
                </Alert>
              )}
              {test.error && (
                <Alert severity="error" sx={{ mb: 1 }}>
                  {test.error}
                </Alert>
              )}

              {test.run_ids.length > 0 && (
                <>
                  <Divider sx={{ my: 1.5 }} />
                  <Typography variant="body2" color="text.secondary" sx={{ mb: 0.5 }}>
                    Runs collected:
                  </Typography>
                  <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap>
                    {test.run_ids.map((id) => (
                      <Link key={id} component={RouterLink} to={`/runs/${id}`} underline="hover">
                        #{id}
                      </Link>
                    ))}
                  </Stack>
                  <Typography variant="body2" sx={{ mt: 1.5 }}>
                    Compare against your shaped profiles on the{" "}
                    <Link component={RouterLink} to="/settings" underline="hover">
                      Settings Impact
                    </Link>{" "}
                    page — the SQM-off runs form their own profile.
                  </Typography>
                </>
              )}
            </Box>
          )}
        </CardContent>
      </Card>

      <Snackbar
        open={!!savedAt}
        autoHideDuration={2500}
        onClose={() => setSavedAt(null)}
        message="Schedule saved"
      />
    </Box>
  );
}
