import { useCallback, useEffect, useMemo, useState } from "react";
import type { ReactNode } from "react";
import Alert from "@mui/material/Alert";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import Card from "@mui/material/Card";
import CardContent from "@mui/material/CardContent";
import Checkbox from "@mui/material/Checkbox";
import Chip from "@mui/material/Chip";
import CircularProgress from "@mui/material/CircularProgress";
import Dialog from "@mui/material/Dialog";
import DialogActions from "@mui/material/DialogActions";
import DialogContent from "@mui/material/DialogContent";
import DialogContentText from "@mui/material/DialogContentText";
import DialogTitle from "@mui/material/DialogTitle";
import FormControlLabel from "@mui/material/FormControlLabel";
import Snackbar from "@mui/material/Snackbar";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TableRow from "@mui/material/TableRow";
import TableSortLabel from "@mui/material/TableSortLabel";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";

import { api } from "../api/client";
import type {
  ApplyProfileChange,
  ProfileDiff,
  ProfileFieldChange,
  SettingsDiagnostics,
  SettingsImpact,
  SettingsProfile,
} from "../api/types";
import Loading from "../components/Loading";
import EmptyState from "../components/EmptyState";
import InsightsIcon from "@mui/icons-material/Insights";
import PublishIcon from "@mui/icons-material/Publish";
import RestorePageIcon from "@mui/icons-material/Restore";
import { fmtDateTime } from "../utils/format";

// State for the "Apply this profile" confirmation dialog: the previewed write
// plan for one profile, awaiting the user's go-ahead.
interface ApplyConfirm {
  fingerprint: string;
  label: string;
  changes: ApplyProfileChange[];
  warnings: string[];
  alreadyApplied: boolean;
}

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
        median Smoothness moved <b>{arrow} {Math.abs(impact.delta_pct)}%</b> (
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

const INFRA_LABELS: Record<string, string> = {
  dns: "DNS",
  tcp: "TCP",
  tls: "TLS",
  jitter: "jitter",
  packet_loss: "loss",
};

function completionSummary(p: SettingsProfile): string {
  const parts = Object.entries(p.completion_metrics).map(
    ([k, v]) => `${INFRA_LABELS[k] ?? k} ${v.median}${k === "packet_loss" ? "%" : "ms"}`
  );
  return parts.length ? parts.join(" · ") : "no completion metrics captured";
}

// "Above/below the historical norm for when it ran." Positive = this config beats
// the day×hour environment it was sampled in — the confound-controlled comparator.
function RelativeSopsCell({
  rel,
  confident,
}: {
  rel: SettingsProfile["relative_sops"];
  confident: boolean;
}) {
  if (!rel) {
    return (
      <Typography component="span" variant="caption" color="text.secondary">
        —
      </Typography>
    );
  }
  const d = rel.delta_median;
  const neutral = Math.abs(d) < 0.5;
  const color = neutral ? "text.secondary" : d > 0 ? "success.main" : "error.main";
  const arrow = neutral ? "" : d > 0 ? "▲ " : "▼ ";
  return (
    <Tooltip
      arrow
      title={`Median Smoothness minus the day×hour historical norm, over ${rel.count} run${
        rel.count === 1 ? "" : "s"
      } (IQR ${rel.p25} to ${rel.p75}). Positive = this profile performs above the typical score for the times it actually ran, with the time-of-day environment removed.`}
    >
      <Typography
        component="span"
        sx={{ color, fontWeight: 700, opacity: confident ? 1 : 0.6, cursor: "help" }}
      >
        {arrow}
        {d > 0 ? "+" : ""}
        {d}
      </Typography>
    </Tooltip>
  );
}

function dirArrow(d: ProfileFieldChange["direction"]): string {
  return d === "higher" ? "↑" : d === "lower" ? "↓" : "≠";
}

// "Higher/lower" is a neutral, numeric fact (the score chip carries good/bad).
function dirColor(d: ProfileFieldChange["direction"]): string {
  return d === "changed" ? "text.secondary" : "info.main";
}

// At-a-glance "what the best profile changed" vs the next-ranked one, with the
// resulting SOPS delta — the seed for experiment suggestions. SOPS is the headline;
// the Completion delta is an opt-in diagnostic (shown only when `showCompletion`).
export function ProfileDiffCard({
  diff,
  showCompletion,
}: {
  diff: ProfileDiff;
  showCompletion: boolean;
}) {
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
            label={`Smoothness ${improved ? "▲" : "▼"} ${diff.delta_abs >= 0 ? "+" : ""}${diff.delta_abs}${
              diff.delta_pct != null
                ? ` (${diff.delta_pct >= 0 ? "+" : ""}${diff.delta_pct}%)`
                : ""
            }`}
          />
          {diff.relative_delta != null && (
            <Tooltip
              arrow
              title="Smoothness gap once each profile's day×hour environment is removed. If this differs from the raw delta, the two profiles were sampled at different times — and this is the fairer number."
            >
              <Chip
                size="small"
                variant="outlined"
                color={diff.relative_delta >= 0 ? "success" : "warning"}
                label={`time-adj ${diff.relative_delta >= 0 ? "▲ +" : "▼ "}${diff.relative_delta}`}
              />
            </Tooltip>
          )}
          {showCompletion && diff.completion_delta != null && (
            <Typography variant="caption" color="text.secondary">
              completion {diff.completion_delta >= 0 ? "▲ +" : "▼ "}
              {diff.completion_delta}
            </Typography>
          )}
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

// Columns the Profiles table can be sorted by. Each maps to a comparable scalar
// (numbers compared numerically, the label/last_seen compared as strings — ISO
// dates sort lexicographically). Nulls always sort last regardless of direction.
type SortKey =
  | "label"
  | "count"
  | "iterations"
  | "median"
  | "speed"
  | "relative_sops"
  | "p25"
  | "min"
  | "completion"
  | "last_seen";

type SortDir = "asc" | "desc";

function sortValue(p: SettingsProfile, key: SortKey): number | string | null {
  switch (key) {
    case "label":
      return p.label.toLowerCase();
    case "count":
      return p.count;
    case "iterations":
      return p.iterations;
    case "median":
      return p.median;
    case "speed":
      return p.speed?.median ?? null;
    case "relative_sops":
      return p.relative_sops?.delta_median ?? null;
    case "p25":
      return p.p25;
    case "min":
      return p.min;
    case "completion":
      return p.completion?.median ?? null;
    case "last_seen":
      return p.last_seen;
  }
}

function compareProfiles(a: SettingsProfile, b: SettingsProfile, key: SortKey, order: SortDir): number {
  const va = sortValue(a, key);
  const vb = sortValue(b, key);
  if (va == null && vb == null) return 0;
  if (va == null) return 1; // nulls last
  if (vb == null) return -1;
  const cmp =
    typeof va === "string" && typeof vb === "string" ? va.localeCompare(vb) : (va as number) - (vb as number);
  return order === "asc" ? cmp : -cmp;
}

function SortHeader({
  id,
  label,
  align,
  orderBy,
  order,
  onSort,
}: {
  id: SortKey;
  label: ReactNode;
  align?: "right";
  orderBy: SortKey;
  order: SortDir;
  onSort: (key: SortKey) => void;
}) {
  const active = orderBy === id;
  return (
    <TableCell align={align} sortDirection={active ? order : false}>
      <TableSortLabel active={active} direction={active ? order : "asc"} onClick={() => onSort(id)}>
        {label}
      </TableSortLabel>
    </TableCell>
  );
}

export default function Settings() {
  const [profiles, setProfiles] = useState<SettingsProfile[] | null>(null);
  const [bestDiff, setBestDiff] = useState<ProfileDiff | null>(null);
  const [impact, setImpact] = useState<SettingsImpact | null>(null);
  const [diag, setDiag] = useState<SettingsDiagnostics | null>(null);
  const [minRuns, setMinRuns] = useState(5);
  // Completion (infra) is a secondary diagnostic — hidden by default so SOPS is
  // unmistakably the headline metric. Opt in via the toggle on the Profiles card.
  const [showCompletion, setShowCompletion] = useState(false);
  // Default to runs scored under the latest (paint) rubric so legacy data — which
  // scores its SOPS off a thinner metric set and reads high — doesn't skew things.
  const [completeOnly, setCompleteOnly] = useState(true);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [toast, setToast] = useState<string | null>(null);
  // Apply-this-profile flow: which row is loading its preview, the pending
  // confirmation, and whether a write is in flight.
  const [previewFp, setPreviewFp] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<ApplyConfirm | null>(null);
  const [applying, setApplying] = useState(false);
  // Profiles table sort. Defaults to median Smoothness descending — the ranking
  // axis the server already orders by, so the initial view is unchanged.
  const [orderBy, setOrderBy] = useState<SortKey>("median");
  const [order, setOrder] = useState<SortDir>("desc");

  const handleSort = useCallback((key: SortKey) => {
    setOrderBy((prev) => {
      if (prev === key) {
        setOrder((o) => (o === "asc" ? "desc" : "asc"));
        return prev;
      }
      setOrder("desc");
      return key;
    });
  }, []);

  const sortedProfiles = useMemo(
    () => (profiles ? [...profiles].sort((a, b) => compareProfiles(a, b, orderBy, order)) : profiles),
    [profiles, orderBy, order]
  );

  const load = useCallback(async () => {
    try {
      const [p, i, d] = await Promise.all([
        api.settingsProfiles(completeOnly),
        api.settingsImpact(completeOnly),
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
  }, [completeOnly]);

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

  // Step 1: fetch the exact field changes (preview, no write) and open the dialog.
  const handleApplyClick = useCallback(async (p: SettingsProfile) => {
    setPreviewFp(p.fingerprint);
    setError(null);
    try {
      const r = await api.applyProfile(p.fingerprint, true);
      setConfirm({
        fingerprint: p.fingerprint,
        label: r.label || p.label,
        changes: r.changes ?? [],
        warnings: r.warnings ?? [],
        alreadyApplied: r.already_applied,
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not preview the profile changes");
    } finally {
      setPreviewFp(null);
    }
  }, []);

  // Step 2: confirmed — write the profile to the firewall.
  const handleConfirmApply = useCallback(async () => {
    if (!confirm) return;
    setApplying(true);
    setError(null);
    try {
      const r = await api.applyProfile(confirm.fingerprint);
      setToast(
        r.applied && r.applied.length > 0
          ? `Wrote ${r.applied.length} change(s) to the firewall — now on ${r.label}`
          : `Firewall already on ${r.label} — no changes needed`
      );
      setConfirm(null);
      await load();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to apply profile");
    } finally {
      setApplying(false);
    }
  }, [confirm, load]);

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
      <Typography variant="body2" color="text.secondary" sx={{ mb: 1 }}>
        How your firewall/SQM configuration profiles correlate with the Seat of Pants Score. Each run
        is stamped with the settings live when it ran; a new profile appears whenever settings change.
        A profile needs ≥ {minRuns} runs before it's treated as confident.
      </Typography>
      <FormControlLabel
        sx={{ mb: 2 }}
        control={
          <Checkbox
            size="small"
            checked={completeOnly}
            onChange={(e) => setCompleteOnly(e.target.checked)}
          />
        }
        label={
          <Typography variant="body2" color="text.secondary">
            Only runs with the latest metrics (full paint data)
            {diag ? ` — ${diag.with_latest_metrics} of ${diag.total_completed} runs qualify` : ""}.
            Runs not comparable under the current methodology are excluded.
          </Typography>
        }
      />

      {error && (
        <Alert severity="error" sx={{ mb: 2 }} onClose={() => setError(null)}>
          {error}
        </Alert>
      )}

      {impact && <ImpactBanner impact={impact} />}

      {bestDiff && <ProfileDiffCard diff={bestDiff} showCompletion={showCompletion} />}

      {!profiles || profiles.length === 0 ? (
        <Card sx={{ mb: 2 }}>
          <CardContent>
            {completeOnly && diag && diag.legacy_metrics > 0 ? (
              <EmptyState
                icon={<InsightsIcon fontSize="inherit" />}
                title="No profiles with the latest metrics yet"
                description={`Your ${diag.legacy_metrics} run(s) predate paint capture, so they're filtered out as not comparable. Run a few benchmarks on the new build, or uncheck "Only runs with the latest metrics" above to include legacy data.`}
              />
            ) : (
              <EmptyState
                icon={<InsightsIcon fontSize="inherit" />}
                title="No settings profiles yet"
                description="Once runs capture your firewall settings (OPNsense provider with traffic-shaper access), each distinct configuration appears here with its score distribution. If you have older runs from before capture, use 'Attribute unstamped runs'."
              />
            )}
          </CardContent>
        </Card>
      ) : (
        <Card sx={{ mb: 2 }}>
          <CardContent>
            <Stack
              direction="row"
              justifyContent="space-between"
              alignItems="center"
              spacing={1}
              sx={{ mb: 0.5 }}
            >
              <Typography variant="h6">Profiles ({profiles.length})</Typography>
              <Chip
                size="small"
                variant={showCompletion ? "filled" : "outlined"}
                color={showCompletion ? "primary" : "default"}
                onClick={() => setShowCompletion((v) => !v)}
                label={showCompletion ? "Hide completion detail" : "Show completion detail"}
              />
            </Stack>
            <Typography variant="caption" color="text.secondary">
              Ranked by median <b>Smoothness</b> — how steadily content was delivered (the
              project's reason for being); higher is better, and it decides "best". Speed is shown alongside. "Best" is only
              awarded to a confident profile. Iterations count every measurement sweep — a 15‑iteration
              run carries far more signal than a single‑iteration one. <b>vs typical</b> is the
              time-adjusted edge: median SOPS minus the historical norm for the day &amp; hour each run
              landed on — positive means the profile beats its environment, which is the fair way to
              compare configs sampled at different times.
              {showCompletion && (
                <>
                  {" "}
                  <b>Compl.</b> is the secondary Completion score (raw DNS/TCP/TLS/jitter/loss) — a
                  diagnostic only; it doesn't decide ranking and can move opposite to SOPS.
                </>
              )}
            </Typography>
            <TableContainer sx={{ mt: 1 }}>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <SortHeader id="label" label="Profile" orderBy={orderBy} order={order} onSort={handleSort} />
                    <SortHeader
                      id="count"
                      label="Runs"
                      align="right"
                      orderBy={orderBy}
                      order={order}
                      onSort={handleSort}
                    />
                    <SortHeader
                      id="iterations"
                      label="Iterations"
                      align="right"
                      orderBy={orderBy}
                      order={order}
                      onSort={handleSort}
                    />
                    <SortHeader
                      id="median"
                      label="Smoothness"
                      align="right"
                      orderBy={orderBy}
                      order={order}
                      onSort={handleSort}
                    />
                    <SortHeader
                      id="speed"
                      label="Speed"
                      align="right"
                      orderBy={orderBy}
                      order={order}
                      onSort={handleSort}
                    />
                    <SortHeader
                      id="relative_sops"
                      label="vs typical"
                      align="right"
                      orderBy={orderBy}
                      order={order}
                      onSort={handleSort}
                    />
                    <SortHeader
                      id="p25"
                      label="IQR"
                      align="right"
                      orderBy={orderBy}
                      order={order}
                      onSort={handleSort}
                    />
                    <SortHeader
                      id="min"
                      label="Min–Max"
                      align="right"
                      orderBy={orderBy}
                      order={order}
                      onSort={handleSort}
                    />
                    {showCompletion && (
                      <SortHeader
                        id="completion"
                        label="Compl."
                        align="right"
                        orderBy={orderBy}
                        order={order}
                        onSort={handleSort}
                      />
                    )}
                    <SortHeader
                      id="last_seen"
                      label="Last seen"
                      orderBy={orderBy}
                      order={order}
                      onSort={handleSort}
                    />
                    <TableCell align="right">Apply</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {(sortedProfiles ?? []).map((p) => (
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
                      <TableCell align="right">{p.speed ? p.speed.median : "—"}</TableCell>
                      <TableCell align="right">
                        <RelativeSopsCell rel={p.relative_sops} confident={p.confident} />
                      </TableCell>
                      <TableCell align="right">
                        {p.p25}–{p.p75}
                      </TableCell>
                      <TableCell align="right">
                        {p.min}–{p.max}
                      </TableCell>
                      {showCompletion && (
                        <TableCell align="right">
                          {p.completion ? (
                            <Tooltip
                              arrow
                              title={`${completionSummary(p)} — median Completion over ${
                                p.completion.count
                              } run${p.completion.count === 1 ? "" : "s"}${
                                p.completion.confident ? "" : ` (need ${minRuns} to confirm)`
                              }`}
                            >
                              <Box component="span" sx={{ cursor: "help" }}>
                                <Typography
                                  component="span"
                                  color="text.secondary"
                                  sx={{ opacity: p.completion.confident ? 1 : 0.55 }}
                                >
                                  {p.completion.median}
                                </Typography>
                                {!p.completion.confident && (
                                  <Typography
                                    component="span"
                                    variant="caption"
                                    color="warning.main"
                                    sx={{ ml: 0.5 }}
                                  >
                                    {p.completion.count}/{minRuns}
                                  </Typography>
                                )}
                              </Box>
                            </Tooltip>
                          ) : (
                            <Typography component="span" variant="caption" color="text.secondary">
                              —
                            </Typography>
                          )}
                        </TableCell>
                      )}
                      <TableCell>{fmtDateTime(p.last_seen)}</TableCell>
                      <TableCell align="right">
                        <Tooltip title="Write this profile's shaper settings to the firewall now. You'll see the exact changes and confirm first.">
                          <span>
                            <Button
                              size="small"
                              variant="outlined"
                              startIcon={
                                previewFp === p.fingerprint ? (
                                  <CircularProgress size={14} />
                                ) : (
                                  <PublishIcon />
                                )
                              }
                              onClick={() => handleApplyClick(p)}
                              disabled={previewFp != null || applying}
                            >
                              Apply
                            </Button>
                          </span>
                        </Tooltip>
                      </TableCell>
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
              <Chip
                size="small"
                color={diag.with_latest_metrics > 0 ? "primary" : "default"}
                variant="outlined"
                label={`latest metrics: ${diag.with_latest_metrics}`}
              />
              {diag.legacy_metrics > 0 && (
                <Chip size="small" variant="outlined" label={`legacy: ${diag.legacy_metrics}`} />
              )}
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

      <Dialog open={confirm != null} onClose={() => !applying && setConfirm(null)} maxWidth="sm" fullWidth>
        <DialogTitle>Apply profile to firewall</DialogTitle>
        <DialogContent>
          {confirm && (
            <>
              <DialogContentText sx={{ mb: 1 }}>
                Write <b>{confirm.label}</b> to the firewall via the traffic shaper. This changes your
                live network shaping immediately and isn't auto-undone — to revert, apply a different
                profile.
              </DialogContentText>
              {confirm.alreadyApplied ? (
                <Alert severity="info" sx={{ mb: 1 }}>
                  The firewall already matches this profile — there's nothing to write.
                </Alert>
              ) : (
                <TableContainer>
                  <Table size="small">
                    <TableHead>
                      <TableRow>
                        <TableCell>Pipe</TableCell>
                        <TableCell>Field</TableCell>
                        <TableCell align="right">From</TableCell>
                        <TableCell align="right">To</TableCell>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {confirm.changes.map((c, i) => (
                        <TableRow key={`${c.pipe_uuid}-${c.field}-${i}`}>
                          <TableCell>{c.label}</TableCell>
                          <TableCell>{c.field_label}</TableCell>
                          <TableCell align="right">
                            <Typography component="span" variant="body2" color="text.secondary">
                              {String(c.from ?? "—")}
                            </Typography>
                          </TableCell>
                          <TableCell align="right" sx={{ fontWeight: 700 }}>
                            {String(c.to ?? "—")}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </TableContainer>
              )}
              {confirm.warnings.length > 0 && (
                <Alert severity="warning" sx={{ mt: 1 }}>
                  {confirm.warnings.map((w, i) => (
                    <div key={i}>{w}</div>
                  ))}
                </Alert>
              )}
            </>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setConfirm(null)} disabled={applying}>
            Cancel
          </Button>
          <Button
            variant="contained"
            color="warning"
            startIcon={applying ? <CircularProgress size={16} color="inherit" /> : <PublishIcon />}
            onClick={handleConfirmApply}
            disabled={applying || (confirm?.alreadyApplied ?? false)}
          >
            {applying ? "Writing…" : "Write to firewall"}
          </Button>
        </DialogActions>
      </Dialog>

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
