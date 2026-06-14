import { useCallback, useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import Divider from "@mui/material/Divider";
import Snackbar from "@mui/material/Snackbar";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import TextField from "@mui/material/TextField";
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
import { fmtDateTime } from "../utils/format";

export default function Config() {
  const [config, setConfig] = useState<BenchmarkConfig | null>(null);
  const [jsonText, setJsonText] = useState("");
  const [weights, setWeights] = useState<Record<string, string>>({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const [provider, setProvider] = useState<ProviderHealth | null>(null);
  const [pipes, setPipes] = useState<FqCodelPipe[] | null>(null);
  const [snapshots, setSnapshots] = useState<ConfigSnapshot[]>([]);
  const [discovering, setDiscovering] = useState(false);

  const applyConfig = useCallback((c: BenchmarkConfig) => {
    setConfig(c);
    setJsonText(JSON.stringify(c, null, 2));
    const w: Record<string, string> = {};
    for (const [k, v] of Object.entries(c.weights ?? {})) w[k] = String(v);
    setWeights(w);
  }, []);

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
        const c = await api.config();
        applyConfig(c);
        await loadProvider();
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to load config");
      } finally {
        setLoading(false);
      }
    })();
  }, [applyConfig, loadProvider]);

  const handleSave = useCallback(async () => {
    setJsonError(null);
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(jsonText);
    } catch (e) {
      setJsonError(e instanceof Error ? e.message : "Invalid JSON");
      return;
    }
    // Merge friendly weight edits over the parsed JSON.
    const numericWeights: Record<string, number> = {};
    for (const [k, v] of Object.entries(weights)) {
      const n = Number(v);
      if (!Number.isNaN(n)) numericWeights[k] = n;
    }
    if (Object.keys(numericWeights).length > 0) {
      parsed.weights = { ...(parsed.weights as object), ...numericWeights };
    }
    setSaving(true);
    try {
      const updated = await api.updateConfig(parsed);
      applyConfig(updated);
      setToast("Config saved");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to save config");
    } finally {
      setSaving(false);
    }
  }, [jsonText, weights, applyConfig]);

  const handleReset = useCallback(async () => {
    setSaving(true);
    try {
      const updated = await api.resetConfig();
      applyConfig(updated);
      setToast("Config reset to defaults");
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to reset config");
    } finally {
      setSaving(false);
    }
  }, [applyConfig]);

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

  if (loading) return <Loading label="Loading config…" />;

  return (
    <Box>
      <Typography variant="h4" sx={{ mb: 3 }}>
        Configuration
      </Typography>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {/* Weights */}
      {config && Object.keys(weights).length > 0 && (
        <Card sx={{ mb: 2 }}>
          <CardContent>
            <Typography variant="h6" gutterBottom>
              Scoring Weights
            </Typography>
            <Typography variant="body2" color="text.secondary" sx={{ mb: 2 }}>
              Adjust per-metric weights. These are merged into the config on save.
            </Typography>
            <Box
              sx={{
                display: "grid",
                gap: 2,
                gridTemplateColumns: { xs: "1fr 1fr", sm: "repeat(3, 1fr)", md: "repeat(4, 1fr)" },
              }}
            >
              {Object.keys(weights)
                .sort()
                .map((k) => (
                  <TextField
                    key={k}
                    label={k}
                    type="number"
                    size="small"
                    value={weights[k]}
                    onChange={(e) => setWeights((w) => ({ ...w, [k]: e.target.value }))}
                    inputProps={{ step: 0.1 }}
                  />
                ))}
            </Box>
          </CardContent>
        </Card>
      )}

      {/* JSON editor */}
      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Typography variant="h6" gutterBottom>
            Benchmark Config (JSON)
          </Typography>
          {jsonError && (
            <Alert severity="error" sx={{ mb: 2 }}>
              {jsonError}
            </Alert>
          )}
          <TextField
            multiline
            fullWidth
            minRows={16}
            maxRows={40}
            value={jsonText}
            onChange={(e) => {
              setJsonText(e.target.value);
              setJsonError(null);
            }}
            spellCheck={false}
            slotProps={{
              input: { sx: { fontFamily: "monospace", fontSize: 13 } },
            }}
          />
          <Stack direction="row" spacing={1} sx={{ mt: 2 }}>
            <Button
              variant="contained"
              startIcon={<SaveIcon />}
              onClick={handleSave}
              disabled={saving}
            >
              Save
            </Button>
            <Button
              color="warning"
              startIcon={<RestartAltIcon />}
              onClick={handleReset}
              disabled={saving}
            >
              Reset to Defaults
            </Button>
          </Stack>
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
                  sx={{
                    p: 1.5,
                    borderRadius: 1.5,
                    border: "1px solid rgba(255,255,255,0.06)",
                  }}
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
