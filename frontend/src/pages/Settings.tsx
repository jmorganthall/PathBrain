import { useCallback, useEffect, useState } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Chip from "@mui/material/Chip";
import Snackbar from "@mui/material/Snackbar";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";

import { api } from "../api/client";
import type { SettingsImpact, SettingsProfile } from "../api/types";
import Loading from "../components/Loading";
import EmptyState from "../components/EmptyState";
import InsightsIcon from "@mui/icons-material/Insights";
import RestorePageIcon from "@mui/icons-material/Restore";
import { fmtDateTime } from "../utils/format";

export function ImpactBanner({ impact }: { impact: SettingsImpact }) {
  if (!impact.changed || impact.delta_pct == null) return null;
  const improved = (impact.delta_abs ?? 0) >= 0;
  const arrow = improved ? "▲" : "▼";
  const collecting = impact.enough_data === false;
  const severity = collecting ? "info" : !impact.significant ? "info" : improved ? "success" : "warning";
  const nBefore = impact.before?.count ?? 0;
  const nAfter = impact.after?.count ?? 0;
  return (
    <Alert severity={severity} icon={<InsightsIcon />} sx={{ mb: 2 }}>
      <Typography variant="body2">
        Since the settings changed{impact.changed_at ? ` (${fmtDateTime(impact.changed_at)})` : ""},
        median SOPS moved <b>{arrow} {Math.abs(impact.delta_pct)}%</b> (
        {impact.before?.median} → {impact.after?.median}).{" "}
        {collecting
          ? `Collecting data before calling it — ${nBefore}/${nAfter} runs (need ${impact.min_runs} each).`
          : impact.significant
            ? "This exceeds your significance threshold."
            : `Below the ${impact.threshold_pct}% significance threshold.`}
      </Typography>
      <Typography variant="caption" color="text.secondary">
        {impact.before?.label} → {impact.after?.label}
      </Typography>
    </Alert>
  );
}

export default function Settings() {
  const [profiles, setProfiles] = useState<SettingsProfile[] | null>(null);
  const [impact, setImpact] = useState<SettingsImpact | null>(null);
  const [minRuns, setMinRuns] = useState(5);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [p, i] = await Promise.all([api.settingsProfiles(), api.settingsImpact()]);
      setProfiles(p.profiles);
      setMinRuns(p.min_runs);
      setImpact(i);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to load settings analysis");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const handleBackfill = useCallback(async () => {
    setBusy(true);
    try {
      const r = await api.settingsBackfill();
      setToast(`Attributed ${r.updated} unstamped run(s) to the current profile`);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Backfill failed");
    } finally {
      setBusy(false);
    }
  }, [load]);

  if (loading) return <Loading label="Loading settings analysis…" />;

  const bestFingerprint = profiles?.find((p) => p.confident)?.fingerprint;

  return (
    <Box>
      <Stack
        direction={{ xs: "column", sm: "row" }}
        justifyContent="space-between"
        alignItems={{ xs: "flex-start", sm: "center" }}
        spacing={1}
        sx={{ mb: 1 }}
      >
        <Typography variant="h4">Settings Impact</Typography>
        <Tooltip title="Stamp the current firewall settings onto past runs that captured none (e.g. before discovery worked). Only do this if the firewall is unchanged since those runs.">
          <span>
            <Button
              startIcon={<RestorePageIcon />}
              onClick={handleBackfill}
              disabled={busy}
              size="small"
            >
              Attribute unstamped runs
            </Button>
          </span>
        </Tooltip>
      </Stack>
      <Typography variant="body2" color="text.secondary" sx={{ mb: 3 }}>
        How your firewall/SQM configuration profiles correlate with the Seat of Pants Score. Each run
        is stamped with the settings live when it ran; a new profile appears whenever settings change.
        A profile needs ≥ {minRuns} runs before it's treated as confident.
      </Typography>

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {impact && <ImpactBanner impact={impact} />}

      {!profiles || profiles.length === 0 ? (
        <Card>
          <CardContent>
            <EmptyState
              icon={<InsightsIcon fontSize="inherit" />}
              title="No settings profiles yet"
              description="Once runs capture your firewall settings (OPNsense provider with traffic-shaper access), each distinct configuration appears here with its score distribution. If you have older runs from before capture, use 'Attribute unstamped runs'."
            />
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardContent>
            <Typography variant="h6" gutterBottom>
              Profiles ({profiles.length})
            </Typography>
            <Typography variant="caption" color="text.secondary">
              Ranked by median SOPS (higher is better). "Best" is only awarded to a confident profile.
            </Typography>
            <TableContainer sx={{ mt: 1 }}>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>Profile</TableCell>
                    <TableCell align="right">Runs</TableCell>
                    <TableCell align="right">Median</TableCell>
                    <TableCell align="right">IQR</TableCell>
                    <TableCell align="right">Min–Max</TableCell>
                    <TableCell>Last seen</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {profiles.map((p) => (
                    <TableRow key={p.fingerprint}>
                      <TableCell sx={{ maxWidth: 360 }}>
                        <Box sx={{ display: "flex", alignItems: "center", gap: 1, flexWrap: "wrap" }}>
                          <Typography variant="body2" sx={{ wordBreak: "break-word" }}>
                            {p.label}
                          </Typography>
                          {p.fingerprint === bestFingerprint && (
                            <Chip size="small" color="success" label="best" />
                          )}
                          {!p.confident && (
                            <Chip size="small" variant="outlined" color="warning" label="limited data" />
                          )}
                        </Box>
                        <Typography variant="caption" color="text.secondary">
                          {p.fingerprint}
                        </Typography>
                      </TableCell>
                      <TableCell align="right">{p.count}</TableCell>
                      <TableCell align="right" sx={{ fontWeight: 700 }}>
                        {p.median}
                      </TableCell>
                      <TableCell align="right">
                        {p.p25}–{p.p75}
                      </TableCell>
                      <TableCell align="right">
                        {p.min}–{p.max}
                      </TableCell>
                      <TableCell>{fmtDateTime(p.last_seen)}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableContainer>
          </CardContent>
        </Card>
      )}

      <Snackbar
        open={toast != null}
        autoHideDuration={3500}
        onClose={() => setToast(null)}
        message={toast ?? ""}
        anchorOrigin={{ vertical: "bottom", horizontal: "center" }}
      />
    </Box>
  );
}
