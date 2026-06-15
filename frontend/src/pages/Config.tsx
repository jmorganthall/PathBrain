import { useCallback, useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import Divider from "@mui/material/Divider";
import FormControlLabel from "@mui/material/FormControlLabel";
import MenuItem from "@mui/material/MenuItem";
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
import ToggleButton from "@mui/material/ToggleButton";
import ToggleButtonGroup from "@mui/material/ToggleButtonGroup";
import Typography from "@mui/material/Typography";
import SaveIcon from "@mui/icons-material/Save";
import RestartAltIcon from "@mui/icons-material/RestartAlt";
import TravelExploreIcon from "@mui/icons-material/TravelExplore";

import { api } from "../api/client";
import type {
  BenchmarkConfig,
  ConfigSnapshot,
  FqCodelPipe,
  ProviderHealth,
} from "../api/types";
import Loading from "../components/Loading";
import JsonViewer from "../components/JsonViewer";
import StringListEditor from "../components/config/StringListEditor";
import HostPortListEditor from "../components/config/HostPortListEditor";
import DnsProviderListEditor from "../components/config/DnsProviderListEditor";
import { fmtDateTime } from "../utils/format";
import {
  vHostOrIp,
  vHostname,
  vHttpUrl,
  vIpOrLocal,
  vPort,
  vPositive,
} from "../utils/validate";

const WAIT_UNTIL = ["load", "domcontentloaded", "networkidle", "commit"];
const EXP_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const EXP_PARAMS = ["quantum", "limit", "target", "interval", "flows", "bandwidth"];

function NumberField(props: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  width?: number;
  fullWidth?: boolean;
  step?: number;
  min?: number;
  max?: number;
  error?: string | null;
}) {
  const { label, value, onChange, width = 150, fullWidth, step = 1, min, max, error } = props;
  return (
    <TextField
      size="small"
      label={label}
      type="number"
      fullWidth={fullWidth}
      value={Number.isFinite(value) ? value : ""}
      onChange={(e) => onChange(parseFloat(e.target.value))}
      error={Boolean(error)}
      helperText={error ?? undefined}
      sx={fullWidth ? undefined : { width }}
      inputProps={{ step, min, max }}
    />
  );
}

/** Count invalid fields so we can block Save when the config is malformed. */
function countErrors(d: BenchmarkConfig): number {
  let n = 0;
  const bad = (e: string | null) => {
    if (e) n += 1;
  };
  d.icmp.targets.forEach((tgt) => bad(vHostOrIp(tgt)));
  bad(vPositive(d.icmp.count));
  bad(vPositive(d.icmp.interval_s));
  bad(vPositive(d.icmp.timeout_s));
  d.dns.providers.forEach((p) => bad(vIpOrLocal(p.server)));
  d.dns.hostnames.forEach((h) => bad(vHostname(h)));
  bad(vPositive(d.dns.timeout_s));
  d.tcp.targets.forEach((tgt) => {
    bad(vHostOrIp(tgt.host));
    bad(vPort(tgt.port));
  });
  bad(vPositive(d.tcp.timeout_s));
  d.tls.targets.forEach((tgt) => {
    bad(vHostOrIp(tgt.host));
    bad(vPort(tgt.port));
  });
  bad(vPositive(d.tls.timeout_s));
  d.http.urls.forEach((u) => bad(vHttpUrl(u)));
  bad(vPositive(d.http.timeout_s));
  d.browser.urls.forEach((u) => bad(vHttpUrl(u)));
  bad(vPositive(d.browser.timeout_s));
  if (!(Number.isInteger(d.iterations) && d.iterations >= 1 && d.iterations <= 20)) n += 1;
  if (d.monitoring && !(Number.isInteger(d.monitoring.interval_minutes) && d.monitoring.interval_minutes >= 1)) {
    n += 1;
  }
  return n;
}

/** Trim strings and drop empty rows before persisting. */
function buildPayload(d: BenchmarkConfig): BenchmarkConfig {
  const s = (v: string) => v.trim();
  return {
    ...d,
    icmp: { ...d.icmp, targets: d.icmp.targets.map(s).filter(Boolean) },
    dns: {
      ...d.dns,
      providers: d.dns.providers
        .map((p) => ({ name: p.name.trim(), server: p.server.trim() }))
        .filter((p) => p.server),
      hostnames: d.dns.hostnames.map(s).filter(Boolean),
    },
    tcp: { ...d.tcp, targets: d.tcp.targets.map((t) => ({ ...t, host: t.host.trim() })) },
    tls: { ...d.tls, targets: d.tls.targets.map((t) => ({ ...t, host: t.host.trim() })) },
    http: { ...d.http, urls: d.http.urls.map(s).filter(Boolean) },
    browser: { ...d.browser, urls: d.browser.urls.map(s).filter(Boolean) },
  };
}

export default function Config() {
  const [draft, setDraft] = useState<BenchmarkConfig | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const [provider, setProvider] = useState<ProviderHealth | null>(null);
  const [pipes, setPipes] = useState<FqCodelPipe[] | null>(null);
  const [snapshots, setSnapshots] = useState<ConfigSnapshot[]>([]);
  const [discovering, setDiscovering] = useState(false);

  const loadProvider = useCallback(async () => {
    try {
      const [p, snaps] = await Promise.all([api.providerHealth(), api.snapshots()]);
      setProvider(p);
      setSnapshots(snaps);
    } catch {
      /* provider info is best-effort */
    }
  }, []);

  useEffect(() => {
    (async () => {
      try {
        setDraft(await api.config());
        await loadProvider();
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to load config");
      } finally {
        setLoading(false);
      }
    })();
  }, [loadProvider]);

  const handleSave = useCallback(async () => {
    if (!draft) return;
    if (countErrors(draft) > 0) {
      setError("Please fix the highlighted fields before saving.");
      return;
    }
    setError(null);
    setSaving(true);
    try {
      const updated = await api.updateConfig(buildPayload(draft) as unknown as Record<string, unknown>);
      setDraft(updated);
      setToast("Config saved");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save config");
    } finally {
      setSaving(false);
    }
  }, [draft]);

  const handleReset = useCallback(async () => {
    setSaving(true);
    try {
      setDraft(await api.resetConfig());
      setToast("Config reset to defaults");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to reset config");
    } finally {
      setSaving(false);
    }
  }, []);

  const handleAdoptRubric = useCallback(async () => {
    setSaving(true);
    try {
      setDraft(await api.adoptRubric());
      setToast("Adopted perception-calibrated rubric — Re-score history to apply to past runs");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to adopt rubric");
    } finally {
      setSaving(false);
    }
  }, []);

  const handleRescore = useCallback(async () => {
    setSaving(true);
    try {
      const r = await api.rescoreHistory();
      setToast(`Re-scored ${r.rescored} run(s) with rubric "${r.rubric_version}"`);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to re-score history");
    } finally {
      setSaving(false);
    }
  }, []);

  const handleDiscover = useCallback(async () => {
    setDiscovering(true);
    setError(null);
    try {
      const res = await api.discover();
      setPipes(res.pipes);
      setToast(`Discovered ${res.pipes.length} pipe(s) via ${res.provider}`);
      await loadProvider();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Discovery failed");
    } finally {
      setDiscovering(false);
    }
  }, [loadProvider]);

  if (loading || !draft) return <Loading label="Loading config…" />;

  const d = draft;
  const errorCount = countErrors(d);
  const iterErr =
    Number.isInteger(d.iterations) && d.iterations >= 1 && d.iterations <= 20
      ? null
      : "1–20";

  return (
    <Box>
      <Stack
        direction={{ xs: "column", sm: "row" }}
        justifyContent="space-between"
        alignItems={{ xs: "flex-start", sm: "center" }}
        spacing={2}
        sx={{ mb: 3 }}
      >
        <Typography variant="h4">Configuration</Typography>
        <Stack direction="row" spacing={1} alignItems="center">
          {errorCount > 0 && (
            <Chip size="small" color="error" label={`${errorCount} field(s) to fix`} />
          )}
          <Button color="warning" startIcon={<RestartAltIcon />} onClick={handleReset} disabled={saving}>
            Reset
          </Button>
          <Button
            variant="contained"
            startIcon={<SaveIcon />}
            onClick={handleSave}
            disabled={saving || errorCount > 0}
          >
            Save
          </Button>
        </Stack>
      </Stack>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {/* General */}
      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Typography variant="h6" gutterBottom>
            General
          </Typography>
          <NumberField
            label="Default iterations"
            value={d.iterations}
            onChange={(v) => setDraft((p) => (p ? { ...p, iterations: v } : p))}
            width={170}
            min={1}
            max={20}
            error={iterErr}
          />
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
            How many times each run repeats the suite and averages (also selectable per run on the Dashboard).
          </Typography>
        </CardContent>
      </Card>

      {/* Monitoring */}
      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Typography variant="h6" gutterBottom>
            Continuous Monitoring
          </Typography>
          <Stack direction="row" spacing={2} alignItems="center" flexWrap="wrap" useFlexGap>
            <FormControlLabel
              control={
                <Switch
                  checked={d.monitoring.enabled}
                  onChange={(e) =>
                    setDraft((p) =>
                      p ? { ...p, monitoring: { ...p.monitoring, enabled: e.target.checked } } : p
                    )
                  }
                />
              }
              label="Enable scheduled runs"
            />
            <NumberField
              label="Interval (minutes)"
              width={180}
              value={d.monitoring.interval_minutes}
              min={1}
              onChange={(v) =>
                setDraft((p) =>
                  p ? { ...p, monitoring: { ...p.monitoring, interval_minutes: v } } : p
                )
              }
              error={
                Number.isInteger(d.monitoring.interval_minutes) && d.monitoring.interval_minutes >= 1
                  ? null
                  : "≥ 1"
              }
            />
          </Stack>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
            When enabled, PathBrain runs the suite automatically on this interval — building the
            history that powers the rolling "Current Responsiveness" score on the Dashboard. Takes
            effect after saving (no restart needed).
          </Typography>
        </CardContent>
      </Card>

      {/* Experiment Engine */}
      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Typography variant="h6" gutterBottom>
            Experiment Engine
          </Typography>
          <Alert severity="warning" sx={{ mb: 2 }}>
            When <b>armed</b> and not in dry-run, PathBrain will change your firewall's traffic shaper
            during the window. The pre-window config is restored when the window closes unless
            auto-promote is on. Start in dry-run to validate.
          </Alert>
          <Stack direction="row" spacing={2} flexWrap="wrap" useFlexGap alignItems="center">
            <FormControlLabel
              control={
                <Switch
                  checked={d.experiment.enabled}
                  onChange={(e) =>
                    setDraft((p) => (p ? { ...p, experiment: { ...p.experiment, enabled: e.target.checked } } : p))
                  }
                />
              }
              label="Armed"
            />
            <FormControlLabel
              control={
                <Switch
                  checked={d.experiment.dry_run}
                  onChange={(e) =>
                    setDraft((p) => (p ? { ...p, experiment: { ...p.experiment, dry_run: e.target.checked } } : p))
                  }
                />
              }
              label="Dry-run (no changes applied)"
            />
            <FormControlLabel
              control={
                <Switch
                  checked={d.experiment.auto_promote}
                  onChange={(e) =>
                    setDraft((p) => (p ? { ...p, experiment: { ...p.experiment, auto_promote: e.target.checked } } : p))
                  }
                />
              }
              label="Auto-promote winner"
            />
          </Stack>
          <Stack direction="row" spacing={2} sx={{ mt: 2 }} flexWrap="wrap" useFlexGap>
            <TextField
              select
              size="small"
              label="Parameter"
              value={d.experiment.param}
              onChange={(e) =>
                setDraft((p) => (p ? { ...p, experiment: { ...p.experiment, param: e.target.value } } : p))
              }
              sx={{ width: 160 }}
            >
              {EXP_PARAMS.map((pm) => (
                <MenuItem key={pm} value={pm}>
                  {pm}
                </MenuItem>
              ))}
            </TextField>
            <TextField
              size="small"
              label="Candidate values (comma-separated)"
              value={d.experiment.candidates.join(", ")}
              onChange={(e) => {
                const vals = e.target.value
                  .split(",")
                  .map((s) => s.trim())
                  .filter(Boolean)
                  .map((s) => (s !== "" && Number.isFinite(Number(s)) ? Number(s) : s));
                setDraft((p) => (p ? { ...p, experiment: { ...p.experiment, candidates: vals } } : p));
              }}
              sx={{ minWidth: 260, flex: 1 }}
            />
            <TextField
              size="small"
              label="Pipe UUID (optional)"
              value={d.experiment.pipe_uuid}
              onChange={(e) =>
                setDraft((p) => (p ? { ...p, experiment: { ...p.experiment, pipe_uuid: e.target.value } } : p))
              }
              sx={{ width: 220 }}
            />
          </Stack>
          <Stack direction="row" spacing={2} sx={{ mt: 2 }} flexWrap="wrap" useFlexGap>
            <NumberField
              label="Dwell (min)"
              value={d.experiment.dwell_minutes}
              min={0}
              onChange={(v) =>
                setDraft((p) => (p ? { ...p, experiment: { ...p.experiment, dwell_minutes: v } } : p))
              }
            />
            <NumberField
              label="Min trials / value"
              value={d.experiment.min_trials_per_value}
              min={1}
              onChange={(v) =>
                setDraft((p) => (p ? { ...p, experiment: { ...p.experiment, min_trials_per_value: v } } : p))
              }
            />
            <NumberField
              label="Improve % to promote"
              value={d.experiment.improve_pct}
              min={0}
              step={0.5}
              onChange={(v) =>
                setDraft((p) => (p ? { ...p, experiment: { ...p.experiment, improve_pct: v } } : p))
              }
            />
          </Stack>
          <Typography variant="subtitle2" sx={{ mt: 2 }}>
            Experimentation window (local time)
          </Typography>
          <ToggleButtonGroup
            size="small"
            sx={{ mt: 1, flexWrap: "wrap" }}
            value={d.experiment.window.days}
            onChange={(_e, days: number[]) =>
              setDraft((p) =>
                p ? { ...p, experiment: { ...p.experiment, window: { ...p.experiment.window, days } } } : p
              )
            }
          >
            {EXP_DAYS.map((lbl, idx) => (
              <ToggleButton key={idx} value={idx}>
                {lbl}
              </ToggleButton>
            ))}
          </ToggleButtonGroup>
          <Stack direction="row" spacing={2} sx={{ mt: 1 }}>
            <NumberField
              label="Start hour"
              value={d.experiment.window.start_hour}
              min={0}
              max={24}
              onChange={(v) =>
                setDraft((p) =>
                  p ? { ...p, experiment: { ...p.experiment, window: { ...p.experiment.window, start_hour: v } } } : p
                )
              }
            />
            <NumberField
              label="End hour"
              value={d.experiment.window.end_hour}
              min={0}
              max={24}
              onChange={(v) =>
                setDraft((p) =>
                  p ? { ...p, experiment: { ...p.experiment, window: { ...p.experiment.window, end_hour: v } } } : p
                )
              }
            />
          </Stack>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mt: 1 }}>
            Hours use the container's local time — set the <code>TZ</code> env var to your timezone.
            Start &gt; end means an overnight window. Manage running experiments on the Experiments page.
          </Typography>
        </CardContent>
      </Card>

      {/* ICMP */}
      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Typography variant="h6" gutterBottom>
            ICMP
          </Typography>
          <StringListEditor
            label="Targets"
            helperText="Hosts or IPs to ping for latency / jitter / packet loss."
            items={d.icmp.targets}
            onChange={(targets) => setDraft((p) => (p ? { ...p, icmp: { ...p.icmp, targets } } : p))}
            validate={vHostOrIp}
            placeholder="1.1.1.1"
            addLabel="Add target"
          />
          <Stack direction="row" spacing={2} sx={{ mt: 2 }} flexWrap="wrap" useFlexGap>
            <NumberField
              label="Count"
              value={d.icmp.count}
              onChange={(v) => setDraft((p) => (p ? { ...p, icmp: { ...p.icmp, count: v } } : p))}
              error={vPositive(d.icmp.count)}
            />
            <NumberField
              label="Interval (s)"
              value={d.icmp.interval_s}
              step={0.05}
              onChange={(v) => setDraft((p) => (p ? { ...p, icmp: { ...p.icmp, interval_s: v } } : p))}
              error={vPositive(d.icmp.interval_s)}
            />
            <NumberField
              label="Timeout (s)"
              value={d.icmp.timeout_s}
              step={0.5}
              onChange={(v) => setDraft((p) => (p ? { ...p, icmp: { ...p.icmp, timeout_s: v } } : p))}
              error={vPositive(d.icmp.timeout_s)}
            />
          </Stack>
        </CardContent>
      </Card>

      {/* DNS */}
      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Typography variant="h6" gutterBottom>
            DNS
          </Typography>
          <DnsProviderListEditor
            items={d.dns.providers}
            onChange={(providers) => setDraft((p) => (p ? { ...p, dns: { ...p.dns, providers } } : p))}
          />
          <Box sx={{ mt: 2 }}>
            <StringListEditor
              label="Hostnames"
              helperText="Names resolved against every resolver above."
              items={d.dns.hostnames}
              onChange={(hostnames) => setDraft((p) => (p ? { ...p, dns: { ...p.dns, hostnames } } : p))}
              validate={vHostname}
              placeholder="example.com"
              addLabel="Add hostname"
            />
          </Box>
          <Stack direction="row" spacing={2} sx={{ mt: 2 }}>
            <NumberField
              label="Timeout (s)"
              value={d.dns.timeout_s}
              step={0.5}
              onChange={(v) => setDraft((p) => (p ? { ...p, dns: { ...p.dns, timeout_s: v } } : p))}
              error={vPositive(d.dns.timeout_s)}
            />
          </Stack>
        </CardContent>
      </Card>

      {/* TCP */}
      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Typography variant="h6" gutterBottom>
            TCP
          </Typography>
          <HostPortListEditor
            label="Targets"
            helperText="Connection-establishment time is measured to each host:port."
            items={d.tcp.targets}
            onChange={(targets) => setDraft((p) => (p ? { ...p, tcp: { ...p.tcp, targets } } : p))}
          />
          <Stack direction="row" spacing={2} sx={{ mt: 2 }}>
            <NumberField
              label="Timeout (s)"
              value={d.tcp.timeout_s}
              step={0.5}
              onChange={(v) => setDraft((p) => (p ? { ...p, tcp: { ...p.tcp, timeout_s: v } } : p))}
              error={vPositive(d.tcp.timeout_s)}
            />
          </Stack>
        </CardContent>
      </Card>

      {/* TLS */}
      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Typography variant="h6" gutterBottom>
            TLS
          </Typography>
          <HostPortListEditor
            label="Targets"
            helperText="TLS handshake duration is measured to each host:port."
            items={d.tls.targets}
            onChange={(targets) => setDraft((p) => (p ? { ...p, tls: { ...p.tls, targets } } : p))}
          />
          <Stack direction="row" spacing={2} sx={{ mt: 2 }}>
            <NumberField
              label="Timeout (s)"
              value={d.tls.timeout_s}
              step={0.5}
              onChange={(v) => setDraft((p) => (p ? { ...p, tls: { ...p.tls, timeout_s: v } } : p))}
              error={vPositive(d.tls.timeout_s)}
            />
          </Stack>
        </CardContent>
      </Card>

      {/* HTTP */}
      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Typography variant="h6" gutterBottom>
            HTTP
          </Typography>
          <StringListEditor
            label="URLs"
            helperText="TTFB, download duration and transfer speed are measured per URL."
            items={d.http.urls}
            onChange={(urls) => setDraft((p) => (p ? { ...p, http: { ...p.http, urls } } : p))}
            validate={vHttpUrl}
            placeholder="https://example.com/"
            addLabel="Add URL"
          />
          <Stack direction="row" spacing={2} sx={{ mt: 2 }}>
            <NumberField
              label="Timeout (s)"
              value={d.http.timeout_s}
              step={0.5}
              onChange={(v) => setDraft((p) => (p ? { ...p, http: { ...p.http, timeout_s: v } } : p))}
              error={vPositive(d.http.timeout_s)}
            />
          </Stack>
        </CardContent>
      </Card>

      {/* Browser */}
      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Typography variant="h6" gutterBottom>
            Browser (headless Chromium)
          </Typography>
          <StringListEditor
            label="URLs"
            helperText="Real page-load timing and total render are measured per URL."
            items={d.browser.urls}
            onChange={(urls) => setDraft((p) => (p ? { ...p, browser: { ...p.browser, urls } } : p))}
            validate={vHttpUrl}
            placeholder="https://example.com/"
            addLabel="Add URL"
          />
          <Stack direction="row" spacing={2} sx={{ mt: 2 }} flexWrap="wrap" useFlexGap alignItems="center">
            <NumberField
              label="Timeout (s)"
              value={d.browser.timeout_s}
              step={1}
              onChange={(v) => setDraft((p) => (p ? { ...p, browser: { ...p.browser, timeout_s: v } } : p))}
              error={vPositive(d.browser.timeout_s)}
            />
            <TextField
              select
              size="small"
              label="Wait until"
              value={d.browser.wait_until}
              onChange={(e) =>
                setDraft((p) => (p ? { ...p, browser: { ...p.browser, wait_until: e.target.value } } : p))
              }
              sx={{ width: 190 }}
            >
              {WAIT_UNTIL.map((w) => (
                <MenuItem key={w} value={w}>
                  {w}
                </MenuItem>
              ))}
            </TextField>
            <FormControlLabel
              control={
                <Switch
                  checked={d.browser.headless}
                  onChange={(e) =>
                    setDraft((p) => (p ? { ...p, browser: { ...p.browser, headless: e.target.checked } } : p))
                  }
                />
              }
              label="Headless"
            />
            <FormControlLabel
              control={
                <Switch
                  checked={d.browser.screenshot}
                  onChange={(e) =>
                    setDraft((p) => (p ? { ...p, browser: { ...p.browser, screenshot: e.target.checked } } : p))
                  }
                />
              }
              label="Screenshot"
            />
            <FormControlLabel
              control={
                <Switch
                  checked={d.browser.har}
                  onChange={(e) =>
                    setDraft((p) => (p ? { ...p, browser: { ...p.browser, har: e.target.checked } } : p))
                  }
                />
              }
              label="HAR"
            />
          </Stack>
        </CardContent>
      </Card>

      {/* Scoring */}
      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Stack
            direction={{ xs: "column", sm: "row" }}
            justifyContent="space-between"
            alignItems={{ xs: "flex-start", sm: "center" }}
            spacing={1}
            sx={{ mb: 1 }}
          >
            <Typography variant="h6">Scoring</Typography>
            <Stack direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
              <Chip size="small" variant="outlined" label={`rubric: ${String(d.rubric_version ?? "—")}`} />
              <Button size="small" onClick={handleAdoptRubric} disabled={saving}>
                Adopt perceptual defaults
              </Button>
              <Button size="small" onClick={handleRescore} disabled={saving}>
                Re-score history
              </Button>
            </Stack>
          </Stack>
          <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 2 }}>
            Thresholds use a perception-calibrated log curve (Weber–Fechner). After editing weights
            or thresholds, click <b>Save</b>, then <b>Re-score history</b> to re-grade past runs so the
            timeline stays comparable.
          </Typography>
          <Typography variant="subtitle2" sx={{ mt: 1 }}>
            Weights
          </Typography>
          <Box
            sx={{
              display: "grid",
              gap: 2,
              mt: 1,
              gridTemplateColumns: { xs: "1fr 1fr", sm: "repeat(3, 1fr)", md: "repeat(4, 1fr)" },
            }}
          >
            {Object.keys(d.weights)
              .sort()
              .map((k) => (
                <NumberField
                  key={k}
                  label={k}
                  fullWidth
                  step={0.1}
                  value={d.weights[k]}
                  onChange={(v) =>
                    setDraft((p) => (p ? { ...p, weights: { ...p.weights, [k]: v } } : p))
                  }
                />
              ))}
          </Box>

          <Typography variant="subtitle2" sx={{ mt: 3 }}>
            Normalization thresholds
          </Typography>
          <Typography variant="caption" color="text.secondary">
            Lower-is-better: a metric at "best" scores 100, at "worst" scores 0.
          </Typography>
          <TableContainer sx={{ mt: 1 }}>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>Metric</TableCell>
                  <TableCell align="right">Best</TableCell>
                  <TableCell align="right">Worst</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {Object.keys(d.thresholds)
                  .sort()
                  .map((m) => (
                    <TableRow key={m}>
                      <TableCell>{m}</TableCell>
                      <TableCell align="right">
                        <NumberField
                          label=""
                          width={120}
                          step={0.1}
                          value={d.thresholds[m].best}
                          onChange={(v) =>
                            setDraft((p) =>
                              p
                                ? {
                                    ...p,
                                    thresholds: {
                                      ...p.thresholds,
                                      [m]: { ...p.thresholds[m], best: v },
                                    },
                                  }
                                : p
                            )
                          }
                        />
                      </TableCell>
                      <TableCell align="right">
                        <NumberField
                          label=""
                          width={120}
                          step={0.1}
                          value={d.thresholds[m].worst}
                          onChange={(v) =>
                            setDraft((p) =>
                              p
                                ? {
                                    ...p,
                                    thresholds: {
                                      ...p.thresholds,
                                      [m]: { ...p.thresholds[m], worst: v },
                                    },
                                  }
                                : p
                            )
                          }
                        />
                      </TableCell>
                    </TableRow>
                  ))}
              </TableBody>
            </Table>
          </TableContainer>
        </CardContent>
      </Card>

      {/* Effective config (read-only) */}
      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Typography variant="h6" gutterBottom>
            Effective config
          </Typography>
          <JsonViewer data={d as unknown as Record<string, unknown>} label="view raw JSON" />
        </CardContent>
      </Card>

      {/* Firewall discovery */}
      <Card>
        <CardContent>
          <Stack
            direction={{ xs: "column", sm: "row" }}
            justifyContent="space-between"
            alignItems={{ xs: "flex-start", sm: "center" }}
            spacing={2}
            sx={{ mb: 2 }}
          >
            <Box>
              <Typography variant="h6">Firewall Discovery</Typography>
              {provider && (
                <Stack direction="row" spacing={1} alignItems="center" sx={{ mt: 0.5 }}>
                  <Chip size="small" label={provider.provider} variant="outlined" />
                  <Chip
                    size="small"
                    color={provider.ok ? "success" : "error"}
                    label={provider.ok ? "healthy" : "unavailable"}
                  />
                </Stack>
              )}
              {provider && !provider.ok && provider.error != null && (
                <Typography
                  variant="caption"
                  color="error"
                  sx={{ display: "block", mt: 0.5, maxWidth: 560, wordBreak: "break-word" }}
                >
                  {String(provider.error)}
                </Typography>
              )}
            </Box>
            <Button
              variant="contained"
              startIcon={<TravelExploreIcon />}
              onClick={handleDiscover}
              disabled={discovering}
            >
              {discovering ? "Discovering…" : "Discover"}
            </Button>
          </Stack>

          {pipes && (
            <Box sx={{ mb: 3 }}>
              <Typography variant="subtitle2" gutterBottom>
                FQ-CoDel Pipes ({pipes.length})
              </Typography>
              {pipes.length === 0 ? (
                <Typography variant="body2" color="text.secondary">
                  No pipes returned.
                </Typography>
              ) : (
                <TableContainer>
                  <Table size="small">
                    <TableHead>
                      <TableRow>
                        <TableCell>Download</TableCell>
                        <TableCell>Upload</TableCell>
                        <TableCell>Target</TableCell>
                        <TableCell>Interval</TableCell>
                        <TableCell>Scheduler</TableCell>
                        <TableCell align="right">Flows</TableCell>
                        <TableCell align="right">Queues</TableCell>
                        <TableCell>ECN</TableCell>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {pipes.map((p, i) => (
                        <TableRow key={i}>
                          <TableCell>{p.download_bandwidth ?? "—"}</TableCell>
                          <TableCell>{p.upload_bandwidth ?? "—"}</TableCell>
                          <TableCell>{p.target ?? "—"}</TableCell>
                          <TableCell>{p.interval ?? "—"}</TableCell>
                          <TableCell>{p.scheduler ?? "—"}</TableCell>
                          <TableCell align="right">{p.flows ?? "—"}</TableCell>
                          <TableCell align="right">{p.queues ?? "—"}</TableCell>
                          <TableCell>{p.ecn == null ? "—" : p.ecn ? "on" : "off"}</TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </TableContainer>
              )}
            </Box>
          )}

          <Divider sx={{ my: 2 }} />

          <Typography variant="subtitle2" gutterBottom>
            Recent Snapshots ({snapshots.length})
          </Typography>
          {snapshots.length === 0 ? (
            <Typography variant="body2" color="text.secondary">
              No snapshots yet. Run a discovery to capture one.
            </Typography>
          ) : (
            <Stack spacing={1}>
              {snapshots.slice(0, 10).map((s) => (
                <Box
                  key={s.id}
                  sx={{ p: 1.5, borderRadius: 1.5, border: "1px solid rgba(255,255,255,0.06)" }}
                >
                  <Stack direction="row" justifyContent="space-between" alignItems="center">
                    <Stack direction="row" spacing={1} alignItems="center">
                      <Chip size="small" label={`#${s.id}`} variant="outlined" />
                      <Typography variant="body2">{s.provider}</Typography>
                      {s.label && (
                        <Typography variant="caption" color="text.secondary">
                          {s.label}
                        </Typography>
                      )}
                    </Stack>
                    <Typography variant="caption" color="text.secondary">
                      {fmtDateTime(s.created_at)}
                    </Typography>
                  </Stack>
                  <Box sx={{ mt: 1 }}>
                    <JsonViewer data={s.data} label="snapshot data" />
                  </Box>
                </Box>
              ))}
            </Stack>
          )}
        </CardContent>
      </Card>

      <Snackbar
        open={toast != null}
        autoHideDuration={3000}
        onClose={() => setToast(null)}
        message={toast ?? ""}
        anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
      />
    </Box>
  );
}
