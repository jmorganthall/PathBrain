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
import type {
  ProfileDiff,
  ProfileFieldChange,
  SettingsDiagnostics,
  SettingsImpact,
  SettingsProfile,
} from "../api/types";
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

function fmtFieldValue(v: string | number | boolean | null): string {
  if (v == null) return "—";
  if (typeof v === "boolean") return v ? "on" : "off";
  return String(v);
}

function dirArrow(d: ProfileFieldChange["direction"]): string {
  return d === "higher" ? "↑" : d === "lower" ? "↓" : "≠";
}

// "Higher/lower" is a neutral, numeric fact (the score chip carries good/bad).
function dirColor(d: ProfileFieldChange["direction"]): string {
  return d === "changed" ? "text.secondary" : "info.main";
}

// At-a-glance "what the best profile changed" vs the next-ranked one, with the
// resulting SOPS delta — the seed for experiment suggestions.
export function ProfileDiffCard({ diff }: { diff: ProfileDiff }) {
  const improved = diff.delta_abs >= 0;
  const distinctPipes = new Set(diff.changes.map((c) => c.pipe)).size;
  return (
    <Card sx={{ mb: 2 }}>
      <CardContent>
        <Stack direction="row" alignItems="center" spacing={1} flexWrap="wrap" useFlexGap sx={{ mb: 0.5 }}>
          <Typography variant="subtitle1">What the best profile changed</Typography>
          <Chip
            size="small"
            color={improved ? "success" : "warning"}
            label={`SOPS ${improved ? "▲" : "▼"} ${diff.delta_abs >= 0 ? "+" : ""}${diff.delta_abs}${
              diff.delta_pct != null
                ? ` (${diff.delta_pct >= 0 ? "+" : ""}${diff.delta_pct}%)`
                : ""
            }`}
          />
        </Stack>
        <Typography variant="caption" color="text.secondary" sx={{ display: "block", mb: 1.5 }}>
          Best profile <b>{diff.best.label}</b> ({diff.best.fingerprint}) vs next‑best{" "}
          <b>{diff.comparison.label}</b> ({diff.comparison.fingerprint}). Shaper fields that differ —
          candidates to push further in experiments.
        </Typography>
        {diff.changes.length === 0 ? (
          <Typography variant="body2" color="text.secondary">
            No shaper fields differ between these two profiles — the score gap is from other factors
            or noise.
          </Typography>
        ) : (
          <Stack spacing={1}>
            {diff.changes.map((c, i) => (
              <Box key={i} sx={{ display: "flex", alignItems: "center", gap: 1, flexWrap: "wrap" }}>
                <Typography variant="body2" sx={{ minWidth: 150, fontWeight: 600 }}>
                  {c.field_label}
                </Typography>
                <Chip size="small" variant="outlined" label={fmtFieldValue(c.from_value)} />
                <Typography component="span" sx={{ color: "text.secondary" }}>
                  →
                </Typography>
                <Chip size="small" color="primary" variant="outlined" label={fmtFieldValue(c.to_value)} />
                <Typography
                  component="span"
                  variant="caption"
                  sx={{ color: dirColor(c.direction), fontWeight: 700 }}
                >
                  {dirArrow(c.direction)} {c.direction}
                </Typography>
                {distinctPipes > 1 && !c.pipe.startsWith("pipe") && (
                  <Typography component="span" variant="caption" color="text.secondary">
                    {c.pipe}
                  </Typography>
                )}
              </Box>
            ))}
          </Stack>
        )}
      </CardContent>
    </Card>
  );
}

export default function Settings() {
  const [profiles, setProfiles] = useState<SettingsProfile[] | null>(null);
  const [bestDiff, setBestDiff] = useState<ProfileDiff | null>(null);
  const [impact, setImpact] = useState<SettingsImpact | null>(null);
  const [diag, setDiag] = useState<SettingsDiagnostics | null>(null);
  const [minRuns, setMinRuns] = useState(5);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [p, i, d] = await Promise.all([
        api.settingsProfiles(),
        api.settingsImpact(),
        api.settingsDiagnostics(),
      ]);
      setProfiles(p.profiles);
      setBestDiff(p.best_diff);
      setMinRuns(p.min_runs);
      setImpact(i);
      setDiag(d);
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

      {bestDiff && <ProfileDiffCard diff={bestDiff} />}

      {!profiles || profiles.length === 0 ? (
        <Card sx={{ mb: 2 }}>
          <CardContent>
            <EmptyState
              icon={<InsightsIcon fontSize="inherit" />}
              title="No settings profiles yet"
              description="Once runs capture your firewall settings (OPNsense provider with traffic-shaper access), each distinct configuration appears here with its score distribution. If you have older runs from before capture, use 'Attribute unstamped runs'."
            />
          </CardContent>
        </Card>
      ) : (
        <Card sx={{ mb: 2 }}>
          <CardContent>
            <Typography variant="h6" gutterBottom>
              Profiles ({profiles.length})
            </Typography>
            <Typography variant="caption" color="text.secondary">
              Ranked by median SOPS (higher is better). "Best" is only awarded to a confident profile.
              Iterations count every measurement sweep — a 15‑iteration run carries far more signal
              than a single‑iteration one.
            </Typography>
            <TableContainer sx={{ mt: 1 }}>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>Profile</TableCell>
                    <TableCell align="right">Runs</TableCell>
                    <TableCell align="right">Iterations</TableCell>
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
                      <TableCell align="right">{p.iterations}</TableCell>
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

      {diag && (
        <Card sx={{ mb: 2 }}>
          <CardContent>
            <Typography variant="subtitle1" gutterBottom>
              Capture diagnostics
            </Typography>
            <Stack direction="row" spacing={1} flexWrap="wrap" useFlexGap sx={{ mb: 1 }}>
              <Chip size="small" label={`completed: ${diag.total_completed}`} />
              <Chip
                size="small"
                color={diag.stamped > 0 ? "success" : "default"}
                label={`stamped: ${diag.stamped}`}
              />
              <Chip
                size="small"
                variant="outlined"
                color={diag.unstamped > 0 ? "warning" : "default"}
                label={`unstamped: ${diag.unstamped}`}
              />
              <Chip size="small" label={`distinct profiles: ${diag.distinct_profiles}`} />
            </Stack>
            <Typography variant="caption" color="text.secondary">
              {diag.stamped > 1 && diag.distinct_profiles >= diag.stamped
                ? "⚠ Every stamped run has a different fingerprint — the firewall config is being read inconsistently each run (a bug to fix), not your settings changing."
                : diag.unstamped > 0
                  ? "Some completed runs captured no settings (they ran before capture existed or while discovery was failing). Use “Attribute unstamped runs” if the firewall is unchanged since."
                  : "Recent runs and the profile fingerprint captured for each:"}
            </Typography>
            <TableContainer sx={{ mt: 1 }}>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>Run</TableCell>
                    <TableCell>When</TableCell>
                    <TableCell>Fingerprint</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {diag.recent.map((r) => (
                    <TableRow key={r.id}>
                      <TableCell>#{r.id}</TableCell>
                      <TableCell>{fmtDateTime(r.created_at)}</TableCell>
                      <TableCell>
                        {r.fingerprint ?? (
                          <Typography component="span" variant="caption" color="text.secondary">
                            — none —
                          </Typography>
                        )}
                      </TableCell>
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
