import { useCallback, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import CircularProgress from "@mui/material/CircularProgress";
import Snackbar from "@mui/material/Snackbar";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import DownloadIcon from "@mui/icons-material/Download";
import ContentCopyIcon from "@mui/icons-material/ContentCopy";
import DataObjectIcon from "@mui/icons-material/DataObject";

import { api } from "../api/client";
import type { DataDump as DataDumpPayload, OptimizerExport } from "../api/types";

// Save any object as a downloaded .json file.
function saveJson(obj: unknown, filename: string) {
  const blob = new Blob([JSON.stringify(obj, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

// Consolidated raw export: pulls the last N runs (with each plugin's immutable raw
// observations) into one JSON blob the user can view, copy, or download. The
// per-run /results view omits raw, so this is the only place to get it across runs.
export default function DataDump() {
  const [limit, setLimit] = useState(25);
  const [dump, setDump] = useState<DataDumpPayload | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  const generate = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const d = await api.dataDump(Math.max(1, Math.min(500, Math.round(limit))));
      setDump(d);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to generate the data dump");
    } finally {
      setLoading(false);
    }
  }, [limit]);

  const json = dump ? JSON.stringify(dump, null, 2) : "";

  const download = useCallback(() => {
    if (!dump) return;
    const stamp = dump.generated_at.replace(/[:.]/g, "-");
    const blob = new Blob([json], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `pathbrain-dump-${dump.count}runs-${stamp}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }, [dump, json]);

  const copy = useCallback(async () => {
    if (!json) return;
    try {
      await navigator.clipboard.writeText(json);
      setToast("Copied JSON to clipboard");
    } catch {
      setToast("Clipboard unavailable — use Download instead");
    }
  }, [json]);

  // ── AI optimizer export: profiles → runs → raw scoring metrics + the objective/levers ──
  const [runsPerProfile, setRunsPerProfile] = useState(50);
  const [optProfileLimit, setOptProfileLimit] = useState(25);
  const [optExport, setOptExport] = useState<OptimizerExport | null>(null);
  const [optLoading, setOptLoading] = useState(false);
  const optJson = optExport ? JSON.stringify(optExport, null, 2) : "";

  const generateOpt = useCallback(async () => {
    setOptLoading(true);
    setError(null);
    try {
      setOptExport(
        await api.optimizerExport(
          Math.max(1, Math.min(1000, Math.round(runsPerProfile))),
          Math.max(1, Math.min(1000, Math.round(optProfileLimit))),
        ),
      );
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to generate the optimizer export");
    } finally {
      setOptLoading(false);
    }
  }, [runsPerProfile, optProfileLimit]);

  const copyOpt = useCallback(async () => {
    if (!optJson) return;
    try {
      await navigator.clipboard.writeText(optJson);
      setToast("Copied optimizer JSON to clipboard");
    } catch {
      setToast("Clipboard unavailable — use Download instead");
    }
  }, [optJson]);

  return (
    <Box>
      <Typography variant="h4" sx={{ mb: 1 }}>
        Data Dump
      </Typography>

      {/* AI optimizer export — the headline export for feeding a model. */}
      <Typography variant="h6" sx={{ mb: 0.5 }}>
        AI optimizer export
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2, maxWidth: 820 }}>
        A profile-centric JSON built to feed an AI: every profile's <b>tunable shaper settings</b> (the
        levers), its <b>runs with the raw scoring metrics</b> (fcp / lcp / stall_time in ms, and every
        other scored metric), plus the <b>objective</b> (which metrics are the crown, lower-is-better,
        and the best values achieved so far) and the <b>shaper field model</b> (which params are writable
        and their sensible ranges). Hand it to a model and ask it to propose new, untested profiles likely
        to score faster than anything measured.
      </Typography>

      <Card sx={{ mb: 3 }}>
        <CardContent>
          <Stack direction="row" spacing={2} alignItems="center" flexWrap="wrap" useFlexGap>
            <TextField
              label="Top profiles"
              type="number"
              size="small"
              value={optProfileLimit}
              onChange={(e) => setOptProfileLimit(parseInt(e.target.value || "0", 10))}
              inputProps={{ min: 1, max: 1000 }}
              sx={{ width: 150 }}
              helperText="Best N by Overall"
            />
            <TextField
              label="Runs per profile"
              type="number"
              size="small"
              value={runsPerProfile}
              onChange={(e) => setRunsPerProfile(parseInt(e.target.value || "0", 10))}
              inputProps={{ min: 1, max: 1000 }}
              sx={{ width: 170 }}
              helperText="Most-recent samples per profile"
            />
            <Button
              variant="contained"
              onClick={generateOpt}
              disabled={optLoading}
              startIcon={optLoading ? <CircularProgress size={16} color="inherit" /> : <DataObjectIcon />}
            >
              {optLoading ? "Generating…" : "Generate"}
            </Button>
            {optExport && (
              <>
                <Button
                  variant="outlined"
                  startIcon={<DownloadIcon />}
                  onClick={() =>
                    saveJson(
                      optExport,
                      `pathbrain-optimizer-${optExport.profile_count}profiles-${optExport.generated_at.replace(/[:.]/g, "-")}.json`,
                    )
                  }
                >
                  Download .json
                </Button>
                <Button variant="outlined" startIcon={<ContentCopyIcon />} onClick={copyOpt}>
                  Copy
                </Button>
                <Typography variant="caption" color="text.secondary">
                  {optExport.profile_count} profile(s) · generated {optExport.generated_at}
                </Typography>
              </>
            )}
          </Stack>
        </CardContent>
      </Card>

      {optExport && (
        <Card sx={{ mb: 3 }}>
          <CardContent>
            <Box
              component="pre"
              sx={{
                m: 0,
                p: 1.5,
                maxHeight: "50vh",
                overflow: "auto",
                fontSize: 12,
                fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                bgcolor: "background.default",
                borderRadius: 1,
                whiteSpace: "pre",
              }}
            >
              {optJson}
            </Box>
          </CardContent>
        </Card>
      )}

      <Typography variant="h6" sx={{ mb: 0.5 }}>
        Raw run dump
      </Typography>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 2, maxWidth: 760 }}>
        A single consolidated JSON of the last <b>N</b> runs, including each run's settings,
        score, and the <b>raw observations</b> captured by every plugin (per iteration) — the immutable
        source of truth that the per-run view doesn't expose. Use it for offline analysis, debugging, or
        sharing a reproducible slice of history.
      </Typography>

      <Card sx={{ mb: 2 }}>
        <CardContent>
          <Stack direction="row" spacing={2} alignItems="center" flexWrap="wrap" useFlexGap>
            <TextField
              label="Last N runs"
              type="number"
              size="small"
              value={limit}
              onChange={(e) => setLimit(parseInt(e.target.value || "0", 10))}
              inputProps={{ min: 1, max: 500 }}
              sx={{ width: 160 }}
            />
            <Button
              variant="contained"
              onClick={generate}
              disabled={loading}
              startIcon={loading ? <CircularProgress size={16} color="inherit" /> : <DataObjectIcon />}
            >
              {loading ? "Generating…" : "Generate"}
            </Button>
            {dump && (
              <>
                <Button variant="outlined" startIcon={<DownloadIcon />} onClick={download}>
                  Download .json
                </Button>
                <Button variant="outlined" startIcon={<ContentCopyIcon />} onClick={copy}>
                  Copy
                </Button>
                <Typography variant="caption" color="text.secondary">
                  {dump.count} run(s) · generated {dump.generated_at}
                </Typography>
              </>
            )}
          </Stack>
        </CardContent>
      </Card>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {dump && (
        <Card>
          <CardContent>
            <Box
              component="pre"
              sx={{
                m: 0,
                p: 1.5,
                maxHeight: "65vh",
                overflow: "auto",
                fontSize: 12,
                fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
                bgcolor: "background.default",
                borderRadius: 1,
                whiteSpace: "pre",
              }}
            >
              {json}
            </Box>
          </CardContent>
        </Card>
      )}

      <Snackbar
        open={toast != null}
        autoHideDuration={2500}
        onClose={() => setToast(null)}
        message={toast ?? ""}
        anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
      />
    </Box>
  );
}
