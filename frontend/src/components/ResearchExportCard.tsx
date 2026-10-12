import { useCallback, useEffect, useRef, useState } from "react";
import Alert from "@mui/material/Alert";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Checkbox from "@mui/material/Checkbox";
import FormControlLabel from "@mui/material/FormControlLabel";
import Link from "@mui/material/Link";
import Stack from "@mui/material/Stack";
import TextField from "@mui/material/TextField";
import Typography from "@mui/material/Typography";
import DownloadIcon from "@mui/icons-material/Download";

import { api, notifyJobsChanged } from "../api/client";
import type { ResearchExportEstimate, ResearchExportFile } from "../api/client";
import { parseApiDate } from "../utils/format";
import { HelpTip } from "./Explain";

const POLL_MS = 5000;

function sizeLabel(bytes: number): string {
  if (bytes >= 1e9) return `${(bytes / 1e9).toFixed(2)} GB`;
  if (bytes >= 1e6) return `${(bytes / 1e6).toFixed(1)} MB`;
  return `${Math.max(1, Math.round(bytes / 1e3))} kB`;
}

function durationLabel(s: number): string {
  if (s < 90) return `~${Math.max(1, Math.round(s))} s`;
  if (s < 5400) return `~${Math.round(s / 60)} min`;
  return `~${(s / 3600).toFixed(1)} h`;
}

// The research export: the whole measured record as a .zip of JSON-lines files, written by a
// background job (progress in the jobs dropdown) so it never ties up the server. Built to be
// handed to an analysis or a model — see docs/plan-learning-engine.md.
export default function ResearchExportCard() {
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");
  const [perSite, setPerSite] = useState(false);
  const [estimate, setEstimate] = useState<ResearchExportEstimate | null>(null);
  const [files, setFiles] = useState<ResearchExportFile[]>([]);
  const [busy, setBusy] = useState(false);
  const [waiting, setWaiting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const newestAtStart = useRef<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const r = await api.researchExports();
      setFiles(r.exports);
      if (newestAtStart.current !== null && r.exports[0] && r.exports[0].name !== newestAtStart.current) {
        setWaiting(false);
        newestAtStart.current = null;
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not list exports");
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    if (!waiting) return;
    const t = window.setInterval(() => void refresh(), POLL_MS);
    return () => window.clearInterval(t);
  }, [waiting, refresh]);

  const doEstimate = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      setEstimate(await api.researchExportEstimate(since || undefined, until || undefined));
    } catch (e) {
      setError(e instanceof Error ? e.message : "Estimate failed");
    } finally {
      setBusy(false);
    }
  }, [since, until]);

  const start = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      newestAtStart.current = files[0]?.name ?? "";
      await api.researchExportStart(since || undefined, until || undefined, perSite);
      notifyJobsChanged();
      setWaiting(true);
    } catch (e) {
      newestAtStart.current = null;
      setError(e instanceof Error ? e.message : "Could not start the export");
    } finally {
      setBusy(false);
    }
  }, [since, until, perSite, files]);

  return (
    <Card sx={{ mb: 3 }}>
      <CardContent>
        <Typography variant="h6" sx={{ mb: 0.5 }}>
          Research export
          <HelpTip title="Every run (each iteration's metrics, machine health, scores), every profile, every duel match with its per-round margins and weather, the lever and Explore prediction ledgers, the config and every methodology — as JSON-lines files in one .zip. Secrets, the firewall's address and Away-test IP/ISP data are left out. Built in the background; the newest few are kept." />
        </Typography>
        <Typography variant="body2" color="text.secondary" sx={{ mb: 2, maxWidth: 820 }}>
          The whole measured record in one file, for analysis off the box. Leave the dates empty for all history.
        </Typography>

        <Stack direction={{ xs: "column", sm: "row" }} spacing={2} alignItems={{ sm: "center" }} flexWrap="wrap" useFlexGap>
          <TextField
            label="From (UTC)"
            type="date"
            size="small"
            value={since}
            onChange={(e) => setSince(e.target.value)}
            InputLabelProps={{ shrink: true }}
          />
          <TextField
            label="Until (UTC, exclusive)"
            type="date"
            size="small"
            value={until}
            onChange={(e) => setUntil(e.target.value)}
            InputLabelProps={{ shrink: true }}
          />
          <FormControlLabel
            control={<Checkbox checked={perSite} onChange={(e) => setPerSite(e.target.checked)} />}
            label={
              <span>
                Include per-site metrics (slow)
                <HelpTip title="Re-derives FCP, LCP, stalls and the other browser metrics for every site of every iteration from the stored raw. Needed to test per-site paired duel margins. Press Estimate to see what it costs on your history." />
              </span>
            }
          />
          <Button variant="outlined" onClick={doEstimate} disabled={busy}>
            Estimate
          </Button>
          <Button variant="contained" onClick={start} disabled={busy || waiting}>
            {waiting ? "Exporting…" : "Start export"}
          </Button>
        </Stack>

        {estimate && (
          <Typography variant="body2" sx={{ mt: 1.5 }}>
            {estimate.runs.toLocaleString()} runs · {estimate.duels.toLocaleString()} duel sessions
            {" · per-site pass "}
            {estimate.per_site_seconds != null
              ? `${durationLabel(estimate.per_site_seconds)} extra (${estimate.per_site_basis})`
              : `not priced (${estimate.per_site_basis})`}
          </Typography>
        )}
        {waiting && (
          <Alert severity="info" sx={{ mt: 1.5 }}>
            Exporting in the background — progress is in the jobs menu. The file appears below when it's done.
          </Alert>
        )}
        {error && (
          <Alert severity="error" sx={{ mt: 1.5 }}>
            {error}
          </Alert>
        )}

        <Stack spacing={0.5} sx={{ mt: 2 }}>
          {files.length === 0 ? (
            <Typography variant="body2" color="text.secondary">
              No exports yet.
            </Typography>
          ) : (
            files.map((f) => (
              <Stack key={f.name} direction="row" spacing={1} alignItems="center" flexWrap="wrap" useFlexGap>
                <DownloadIcon fontSize="small" />
                <Link href={api.researchExportUrl(f.name)} download={f.name} sx={{ wordBreak: "break-all" }}>
                  {f.name}
                </Link>
                <Typography variant="body2" color="text.secondary">
                  {sizeLabel(f.bytes)} · {parseApiDate(f.created_at).toLocaleString()}
                </Typography>
              </Stack>
            ))
          )}
        </Stack>
      </CardContent>
    </Card>
  );
}
