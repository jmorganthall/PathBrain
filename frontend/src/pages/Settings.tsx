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
import FormControl from "@mui/material/FormControl";
import FormControlLabel from "@mui/material/FormControlLabel";
import InputLabel from "@mui/material/InputLabel";
import ListItemIcon from "@mui/material/ListItemIcon";
import ListSubheader from "@mui/material/ListSubheader";
import ListItemText from "@mui/material/ListItemText";
import Menu from "@mui/material/Menu";
import MenuItem from "@mui/material/MenuItem";
import Select from "@mui/material/Select";
import Snackbar from "@mui/material/Snackbar";
import Stack from "@mui/material/Stack";
import Table from "@mui/material/Table";
import TableBody from "@mui/material/TableBody";
import TableCell from "@mui/material/TableCell";
import TableContainer from "@mui/material/TableContainer";
import TableHead from "@mui/material/TableHead";
import TablePagination from "@mui/material/TablePagination";
import TableRow from "@mui/material/TableRow";
import TableSortLabel from "@mui/material/TableSortLabel";
import Tooltip from "@mui/material/Tooltip";
import Typography from "@mui/material/Typography";

import { api } from "../api/client";
import type {
  ApplyProfileChange,
  ChallengerRace,
  ProfileDiff,
  ProfileFieldChange,
  ProfileTest,
  SettingsDiagnostics,
  SettingsImpact,
  SettingsProfile,
} from "../api/types";
import Loading from "../components/Loading";
import EmptyState from "../components/EmptyState";
import ProfileQuadrant from "../components/ProfileQuadrant";
import InsightsIcon from "@mui/icons-material/Insights";
import PublishIcon from "@mui/icons-material/Publish";
import RestorePageIcon from "@mui/icons-material/Restore";
import ScienceIcon from "@mui/icons-material/Science";
import ViewColumnIcon from "@mui/icons-material/ViewColumn";
import CheckIcon from "@mui/icons-material/Check";
import { fmtDateTime } from "../utils/format";
import { useMetricMeta } from "../utils/metrics";
import type { ProfileField } from "../api/types";
import { buildFields, fmtFieldValue as fmtNumField, profileValue } from "../utils/profileFields";
import type { FieldDef } from "../utils/profileFields";

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
  const iBefore = impact.before?.iterations ?? 0;
  const iAfter = impact.after?.iterations ?? 0;
  const need = impact.min_iterations ?? 15;
  return (
    <Alert severity={severity} icon={<InsightsIcon />} sx={{ mb: 2 }}>
      <Typography variant="body2">
        Since the settings changed{impact.changed_at ? ` (${fmtDateTime(impact.changed_at)})` : ""},
        median Smoothness moved <b>{arrow} {Math.abs(impact.delta_pct)}%</b> (
        {impact.before?.median} → {impact.after?.median}).{" "}
        {collecting
          ? `Collecting data before calling it — ${iBefore}/${iAfter} iterations (need ${need} each).`
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
  rel: SettingsProfile["relative_overall"];
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
      title={`Median Overall minus the day×hour historical norm, over ${rel.count} run${
        rel.count === 1 ? "" : "s"
      } (IQR ${rel.p25} to ${rel.p75}). Positive = this profile performs above the typical Overall for the times it actually ran, with the time-of-day environment removed.`}
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

// Columns the Profiles table can be sorted by. Built-in column keys plus any dynamic
// field key (overall / axis score / metric), resolved via profileValue. Numbers
// compare numerically, label/last_seen as strings; nulls always sort last.
type SortKey = string;

type SortDir = "asc" | "desc";

function sortValue(p: SettingsProfile, key: SortKey): number | string | null {
  switch (key) {
    case "label":
      return p.label.toLowerCase();
    case "median":
      return p.median;
    case "speed":
      return p.speed?.median ?? null;
    case "relative_sops":
      return p.relative_overall?.delta_median ?? null;
    case "p25":
      return p.p25;
    case "overall_p25":
      return p.overall_p25 ?? null;
    case "min":
      return p.min;
    case "completion":
      return p.completion?.median ?? null;
    case "last_seen":
      return p.last_seen;
  }
  // Dynamic keys: overall, axis scores, run stats, and any metric.
  return profileValue(p, key);
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

// Numeric fields that already have a dedicated fixed column, so they're excluded
// from the optional column-selector menu (no duplicate columns).
const FIXED_COLUMN_KEYS = new Set([
  "overall",
  "responsiveness",
  "speed",
  "smoothness",
  "iterations",
  "count",
  "relative_smoothness",
]);

// Group a field list by its `group` for the axis-picker / column menus.
function groupFields(fields: FieldDef[]): { name: string; items: FieldDef[] }[] {
  const groups: { name: string; items: FieldDef[] }[] = [];
  for (const f of fields) {
    let g = groups.find((x) => x.name === f.group);
    if (!g) {
      g = { name: f.group, items: [] };
      groups.push(g);
    }
    g.items.push(f);
  }
  return groups;
}

// A grouped dropdown for picking which numeric field a chart axis plots.
function AxisSelect({
  label,
  value,
  fields,
  onChange,
}: {
  label: string;
  value: string;
  fields: FieldDef[];
  onChange: (key: string) => void;
}) {
  return (
    <FormControl size="small" sx={{ minWidth: 170 }}>
      <InputLabel>{label}</InputLabel>
      <Select label={label} value={value} onChange={(e) => onChange(e.target.value)}>
        {groupFields(fields).flatMap((g) => [
          <ListSubheader key={`h-${g.name}`}>{g.name}</ListSubheader>,
          ...g.items.map((f) => (
            <MenuItem key={f.key} value={f.key}>
              {f.label}
            </MenuItem>
          )),
        ])}
      </Select>
    </FormControl>
  );
}

export default function Settings() {
  const [profiles, setProfiles] = useState<SettingsProfile[] | null>(null);
  const [bestDiff, setBestDiff] = useState<ProfileDiff | null>(null);
  const [impact, setImpact] = useState<SettingsImpact | null>(null);
  const [diag, setDiag] = useState<SettingsDiagnostics | null>(null);
  // Total iterations a profile needs before it's "confident" (the unit of signal).
  const [minIterations, setMinIterations] = useState(15);
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
  // "Test to minimum" flow: the pending confirmation (a limited-data profile + the
  // exact firewall diff that would be written) and the in-progress test status.
  const [testConfirm, setTestConfirm] = useState<ApplyConfirm | null>(null);
  const [testPreviewFp, setTestPreviewFp] = useState<string | null>(null);
  const [activeTest, setActiveTest] = useState<ProfileTest | null>(null);
  // The crowned "best" profile (closest to the top-right corner) + the selectable
  // numeric fields, both from the server.
  const [bestFingerprint, setBestFingerprint] = useState<string | null>(null);
  const [currentFingerprint, setCurrentFingerprint] = useState<string | null>(null);
  const [responseFields, setResponseFields] = useState<ProfileField[]>([]);
  // Dynamic quadrant axes — default to the Overall scoring corner's three inputs
  // (FCP × page-load × total-stall), with the third encoded as Shade opacity; the
  // crowned profile is ringed. So the default view demonstrates how Overall is scored.
  const [xKey, setXKey] = useState("fcp");
  const [yKey, setYKey] = useState("load_event");
  const [sizeKey, setSizeKey] = useState("total_stall");
  // Optional extra table columns (dynamic field keys), persisted across reloads.
  const [extraCols, setExtraCols] = useState<string[]>(() => {
    try {
      return JSON.parse(localStorage.getItem("settingsImpactColumns") || "[]");
    } catch {
      return [];
    }
  });
  const [colMenu, setColMenu] = useState<HTMLElement | null>(null);
  // Profiles table sort. Defaults to Overall (corner) descending — so the crowned,
  // closest-to-top-right profile is on top.
  const [orderBy, setOrderBy] = useState<SortKey>("overall");
  const [order, setOrder] = useState<SortDir>("desc");

  const metricMeta = useMetricMeta();
  const allFields = useMemo(
    () => buildFields(profiles ?? [], responseFields, metricMeta),
    [profiles, responseFields, metricMeta]
  );
  const fieldByKey = useMemo(() => {
    const m = new Map(allFields.map((f) => [f.key, f]));
    return (k: string) => m.get(k);
  }, [allFields]);

  const toggleColumn = useCallback((key: string) => {
    setExtraCols((prev) => {
      const next = prev.includes(key) ? prev.filter((k) => k !== key) : [...prev, key];
      localStorage.setItem("settingsImpactColumns", JSON.stringify(next));
      return next;
    });
  }, []);

  // Fields offered in the column menu (excluding those with a fixed column), and the
  // currently-selected extra columns resolved to field defs.
  const columnMenuFields = useMemo(
    () => allFields.filter((f) => !FIXED_COLUMN_KEYS.has(f.key)),
    [allFields]
  );
  const extraFields = useMemo(
    () =>
      extraCols
        .filter((k) => !FIXED_COLUMN_KEYS.has(k))  // never duplicate a now-standard column
        .map((k) => fieldByKey(k))
        .filter((f): f is FieldDef => f != null),
    [extraCols, fieldByKey]
  );

  const handleSort = useCallback((key: SortKey) => {
    setPage(0); // re-sorting should land you on the first page
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

  // Paginate the (sorted) profiles table — 25/page by default. Sorting + the column
  // picker still operate on the full set; only the rendered rows are sliced.
  const [page, setPage] = useState(0);
  const [rowsPerPage, setRowsPerPage] = useState(25);
  const rowCount = sortedProfiles?.length ?? 0;
  // Keep the page in range when the result set shrinks (filter toggles, reloads).
  useEffect(() => {
    const maxPage = Math.max(0, Math.ceil(rowCount / rowsPerPage) - 1);
    if (page > maxPage) setPage(maxPage);
  }, [rowCount, rowsPerPage, page]);
  const pagedProfiles = useMemo(
    () => (sortedProfiles ? sortedProfiles.slice(page * rowsPerPage, page * rowsPerPage + rowsPerPage) : sortedProfiles),
    [sortedProfiles, page, rowsPerPage]
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
      setMinIterations(p.min_iterations);
      setBestFingerprint(p.best_fingerprint);
      setCurrentFingerprint(p.current_fingerprint);
      setResponseFields(p.fields);
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

  // "Test to minimum" step 1: preview the exact firewall diff this test would write,
  // and open the confirmation dialog.
  const handleTestClick = useCallback(async (p: SettingsProfile) => {
    setTestPreviewFp(p.fingerprint);
    setError(null);
    try {
      const r = await api.applyProfile(p.fingerprint, true);
      setTestConfirm({
        fingerprint: p.fingerprint,
        label: r.label || p.label,
        changes: r.changes ?? [],
        warnings: r.warnings ?? [],
        alreadyApplied: r.already_applied,
      });
    } catch (e) {
      setError(e instanceof Error ? e.message : "Could not preview the profile changes");
    } finally {
      setTestPreviewFp(null);
    }
  }, []);

  // "Test to minimum" step 2: kick off the test (applies → runs → restores). The
  // run queues behind any other firewall operation via the coordination lock.
  const handleConfirmTest = useCallback(async () => {
    if (!testConfirm) return;
    setError(null);
    try {
      const r = await api.testProfile(testConfirm.fingerprint);
      setToast(
        `Testing ${testConfirm.label}: running ${r.iterations} iteration(s) to reach the ${r.min_iterations}-iteration minimum`
      );
      setTestConfirm(null);
      // Show the live status immediately; the poller below keeps it fresh.
      const cur = await api.profileTestCurrent();
      setActiveTest(cur.test);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to start the profile test");
    }
  }, [testConfirm]);

  // Poll the active profile test until it finishes, then reload + clear.
  useEffect(() => {
    if (!activeTest || (activeTest.status !== "running" && activeTest.status !== "pending")) return;
    const t = setInterval(async () => {
      try {
        const cur = await api.profileTestCurrent();
        setActiveTest(cur.test);
        if (cur.test && (cur.test.status === "complete" || cur.test.status === "failed")) {
          if (cur.test.status === "failed") {
            setError(`Profile test failed: ${cur.test.error ?? "unknown error"}`);
          } else {
            setToast(`Profile test finished — ${cur.test.label ?? cur.test.fingerprint}`);
          }
          await load();
        }
      } catch {
        /* transient; keep polling */
      }
    }, 2000);
    return () => clearInterval(t);
  }, [activeTest, load]);

  const testRunning = activeTest != null && (activeTest.status === "running" || activeTest.status === "pending");

  // Challenger race: a time-boxed, adaptive race of limited-data profiles against the
  // best (one iteration at a time). Dialog inputs + live status.
  const [raceOpen, setRaceOpen] = useState(false);
  const [raceMinutes, setRaceMinutes] = useState(10);
  const [raceAutoPromote, setRaceAutoPromote] = useState(false);
  const [activeRace, setActiveRace] = useState<ChallengerRace | null>(null);
  const raceRunning =
    activeRace != null && (activeRace.status === "running" || activeRace.status === "pending");

  const handleStartRace = useCallback(async () => {
    setError(null);
    try {
      await api.startRace(raceMinutes, raceAutoPromote);
      setRaceOpen(false);
      setToast(
        `Racing challengers for up to ${raceMinutes} min${raceAutoPromote ? " — winner will be auto-applied" : ""}`
      );
      const cur = await api.raceCurrent();
      setActiveRace(cur.race);
    } catch (e) {
      setError(e instanceof Error ? e.message : "Failed to start the challenger race");
    }
  }, [raceMinutes, raceAutoPromote]);

  // Poll the active race until it finishes, then reload + report the outcome.
  useEffect(() => {
    if (!activeRace || (activeRace.status !== "running" && activeRace.status !== "pending")) return;
    const t = setInterval(async () => {
      try {
        const cur = await api.raceCurrent();
        setActiveRace(cur.race);
        const st = cur.race?.status;
        if (cur.race && st && st !== "running" && st !== "pending") {
          if (st === "failed") {
            setError(`Challenger race failed: ${cur.race.error ?? "unknown error"}`);
          } else if (cur.race.winner_fingerprint) {
            setToast(
              cur.race.promoted
                ? `Race done — promoted a new best (${cur.race.winner_fingerprint.slice(0, 8)})`
                : `Race done — found a new best (${cur.race.winner_fingerprint.slice(0, 8)}); baseline restored`
            );
          } else {
            setToast(`Race ${st} — no challenger beat the best; baseline restored`);
          }
          await load();
        }
      } catch {
        /* transient; keep polling */
      }
    }, 2000);
    return () => clearInterval(t);
  }, [activeRace, load]);

  if (loading) return <Loading label="Loading settings analysis…" />;

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
        A profile needs ≥ {minIterations} total iterations before it's treated as confident — a
        15-iteration run carries far more signal than a single-iteration one, so iterations (not run
        count) are the bar.
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

      {activeTest && (activeTest.status === "running" || activeTest.status === "pending") && (
        <Alert severity="info" icon={<CircularProgress size={18} />} sx={{ mb: 2 }}>
          Testing <b>{activeTest.label ?? activeTest.fingerprint}</b> — running {activeTest.iterations}{" "}
          iteration(s){activeTest.run_id ? ` (run #${activeTest.run_id})` : ""}, then restoring your
          current settings.
          {activeTest.lock_owner && activeTest.lock_owner !== `profile-test#${activeTest.id}`
            ? ` Waiting on ${activeTest.lock_owner} to finish first…`
            : ""}
        </Alert>
      )}

      {raceRunning && activeRace && (
        <Alert
          severity="info"
          icon={<CircularProgress size={18} />}
          sx={{ mb: 2 }}
          action={
            <Button color="inherit" size="small" onClick={() => api.cancelRace().catch(() => {})}>
              Cancel
            </Button>
          }
        >
          Racing challengers — {activeRace.iterations_run} iteration(s) run
          {activeRace.leader_label ? `, leading: ${activeRace.leader_label}` : ""}
          {(activeRace.eliminated?.length ?? 0) > 0
            ? `, ${activeRace.eliminated.length} eliminated`
            : ""}
          {(activeRace.incumbent_refreshes ?? 0) > 0
            ? `, ${activeRace.incumbent_refreshes} incumbent refresh(es)`
            : ""}
          {activeRace.auto_promote ? " · winner will be auto-applied" : " · baseline restored at end"}.
        </Alert>
      )}

      {impact && <ImpactBanner impact={impact} />}

      {bestDiff && <ProfileDiffCard diff={bestDiff} showCompletion={showCompletion} />}

      {profiles && profiles.length >= 2 && allFields.length > 0 && (
        <Card sx={{ mb: 2 }}>
          <CardContent>
            <Stack
              direction={{ xs: "column", sm: "row" }}
              justifyContent="space-between"
              alignItems={{ xs: "flex-start", sm: "center" }}
              spacing={1}
              sx={{ mb: 1 }}
            >
              <Typography variant="h6">Profile scatter</Typography>
              <Stack direction="row" spacing={1} alignItems="center">
                <Tooltip title="Adaptively test promising limited-data profiles one iteration at a time, eliminating any that can't overtake the best.">
                  <span>
                    <Button
                      size="small"
                      variant="outlined"
                      onClick={() => setRaceOpen(true)}
                      disabled={raceRunning || testRunning || applying}
                    >
                      Race challengers
                    </Button>
                  </span>
                </Tooltip>
                <AxisSelect label="X axis" value={xKey} fields={allFields} onChange={setXKey} />
                <AxisSelect label="Y axis" value={yKey} fields={allFields} onChange={setYKey} />
                <AxisSelect label="Shade" value={sizeKey} fields={allFields} onChange={setSizeKey} />
              </Stack>
            </Stack>
            <ProfileQuadrant
              profiles={profiles}
              xField={fieldByKey(xKey) ?? allFields[0]}
              yField={fieldByKey(yKey) ?? allFields[0]}
              shadeField={fieldByKey(sizeKey) ?? null}
              bestFingerprint={bestFingerprint}
              currentFingerprint={currentFingerprint}
            />
          </CardContent>
        </Card>
      )}

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
              <Stack direction="row" spacing={1} alignItems="center">
                <Button
                  size="small"
                  variant="outlined"
                  startIcon={<ViewColumnIcon />}
                  onClick={(e) => setColMenu(e.currentTarget)}
                >
                  Columns{extraCols.length ? ` (${extraCols.length})` : ""}
                </Button>
                <Menu anchorEl={colMenu} open={Boolean(colMenu)} onClose={() => setColMenu(null)}>
                  {groupFields(columnMenuFields).flatMap((g) => [
                    <ListSubheader key={`h-${g.name}`}>{g.name}</ListSubheader>,
                    ...g.items.map((f) => (
                      <MenuItem key={f.key} onClick={() => toggleColumn(f.key)} dense>
                        <ListItemIcon sx={{ minWidth: 28 }}>
                          {extraCols.includes(f.key) && <CheckIcon fontSize="small" />}
                        </ListItemIcon>
                        <ListItemText>{f.label}</ListItemText>
                      </MenuItem>
                    )),
                  ])}
                </Menu>
                <Chip
                  size="small"
                  variant={showCompletion ? "filled" : "outlined"}
                  color={showCompletion ? "primary" : "default"}
                  onClick={() => setShowCompletion((v) => !v)}
                  label={showCompletion ? "Hide completion detail" : "Show completion detail"}
                />
              </Stack>
            </Stack>
            <Typography variant="caption" color="text.secondary">
              Ranked by <b>Overall</b> — a single 0–100 measure of how close a profile sits to the
              ideal <b>Speed 100 / Smoothness 100</b> corner (fastest <i>and</i> smoothest). "Best" is
              the confident profile with the highest <b>probability of being the true best</b> — a
              Bayesian comparison that weighs both a high typical Overall and how sure we are of it
              (more iterations → tighter estimate), rather than a worst-case floor. Speed and
              Smoothness are shown alongside. Iterations count every measurement sweep — a
              15‑iteration run carries far more signal than a single‑iteration one. <b>vs typical</b>
              is the time-adjusted edge: median Overall minus the historical norm for the day &amp;
              hour each run landed on — positive means the profile beats its environment, the fair way
              to compare configs sampled at different times; the crown de-confounds a profile that
              scored below its own day/hour norm, so "best" can't just be riding a favorable window.
              Use <b>Columns</b> to add any other metric we collect, then sort by it.
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
                      id="overall"
                      label="Overall"
                      align="right"
                      orderBy={orderBy}
                      order={order}
                      onSort={handleSort}
                    />
                    <SortHeader
                      id="responsiveness"
                      label="Respons."
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
                      id="overall_p25"
                      label="Overall IQR"
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
                    {extraFields.map((f) => (
                      <SortHeader
                        key={f.key}
                        id={f.key}
                        label={f.label}
                        align="right"
                        orderBy={orderBy}
                        order={order}
                        onSort={handleSort}
                      />
                    ))}
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
                  {(pagedProfiles ?? []).map((p) => {
                    const isActive = p.fingerprint === currentFingerprint;
                    return (
                    <TableRow
                      key={p.fingerprint}
                      selected={isActive}
                      sx={isActive ? { "& td": { bgcolor: "action.selected" } } : undefined}
                    >
                      <TableCell sx={{ maxWidth: 360 }}>
                        <Box sx={{ display: "flex", alignItems: "center", gap: 1, flexWrap: "wrap" }}>
                          <Typography variant="body2" sx={{ wordBreak: "break-word" }}>
                            {p.label}
                          </Typography>
                          {isActive && (
                            <Tooltip title="This profile is live on the firewall right now">
                              <Chip size="small" color="info" label="active" />
                            </Tooltip>
                          )}
                          {p.fingerprint === bestFingerprint && (
                            <Tooltip
                              title={
                                `Crowned by probability-of-best${
                                  p.prob_best != null ? ` (${Math.round(p.prob_best * 100)}% most likely the true best)` : ""
                                } — a Bayesian comparison of each profile's Overall that rewards both a high typical score and how sure we are of it (more iterations → tighter estimate), without docking it twice for variance.` +
                                (p.relative_overall_lb != null && p.relative_overall_lb < 0
                                  ? ` Its level is de-confounded down by ${p.relative_overall_lb} for scoring below its day/hour norm (it rode a favorable time window).`
                                  : ``)
                              }
                            >
                              <Chip size="small" color="success" label="best" />
                            </Tooltip>
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
                        {p.overall ?? "—"}
                      </TableCell>
                      <TableCell align="right">{p.scores?.responsiveness ?? "—"}</TableCell>
                      <TableCell align="right">{p.median}</TableCell>
                      <TableCell align="right">{p.speed ? p.speed.median : "—"}</TableCell>
                      <TableCell align="right">
                        <RelativeSopsCell rel={p.relative_overall} confident={p.confident} />
                      </TableCell>
                      <TableCell align="right">
                        {p.overall_p25 != null ? `${p.overall_p25}–${p.overall_p75}` : "—"}
                      </TableCell>
                      <TableCell align="right">
                        {p.min}–{p.max}
                      </TableCell>
                      {extraFields.map((f) => (
                        <TableCell key={f.key} align="right">
                          {fmtNumField(f.get(p), f.unit)}
                        </TableCell>
                      ))}
                      {showCompletion && (
                        <TableCell align="right">
                          {p.completion ? (
                            <Tooltip
                              arrow
                              title={`${completionSummary(p)} — median Completion over ${
                                p.completion.count
                              } run${p.completion.count === 1 ? "" : "s"}${
                                p.completion.confident
                                  ? ""
                                  : ` (need ${minIterations} iterations to confirm)`
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
                                    {p.completion.iterations ?? p.completion.count}/{minIterations}
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
                        <Stack direction="row" spacing={1} justifyContent="flex-end">
                          {!p.confident && (
                            <Tooltip title={`Apply this profile, run the iterations still needed to reach the ${minIterations}-iteration minimum, then restore your current settings. Queues behind any other firewall operation.`}>
                              <span>
                                <Button
                                  size="small"
                                  variant="outlined"
                                  color="secondary"
                                  startIcon={
                                    testPreviewFp === p.fingerprint ? (
                                      <CircularProgress size={14} />
                                    ) : (
                                      <ScienceIcon />
                                    )
                                  }
                                  onClick={() => handleTestClick(p)}
                                  disabled={testPreviewFp != null || testRunning || applying}
                                >
                                  Test to min
                                </Button>
                              </span>
                            </Tooltip>
                          )}
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
                                disabled={previewFp != null || applying || testRunning}
                              >
                                Apply
                              </Button>
                            </span>
                          </Tooltip>
                        </Stack>
                      </TableCell>
                    </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </TableContainer>
            {rowCount > 0 && (
              <TablePagination
                component="div"
                count={rowCount}
                page={page}
                onPageChange={(_e, p) => setPage(p)}
                rowsPerPage={rowsPerPage}
                onRowsPerPageChange={(e) => {
                  setRowsPerPage(parseInt(e.target.value, 10));
                  setPage(0);
                }}
                rowsPerPageOptions={[25, 50, 100]}
              />
            )}
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

      <Dialog open={testConfirm != null} onClose={() => setTestConfirm(null)} maxWidth="sm" fullWidth>
        <DialogTitle>Test this profile up to the minimum</DialogTitle>
        <DialogContent>
          {testConfirm && (
            <>
              <DialogContentText sx={{ mb: 1 }}>
                This will <b>temporarily</b> apply <b>{testConfirm.label}</b> to the firewall, run a
                benchmark with the iterations still needed to reach the {minIterations}-iteration
                minimum, then <b>restore your current settings</b>. The run queues behind any other
                firewall operation, and its measurement is discarded if the settings change mid-run.
              </DialogContentText>
              {testConfirm.changes.length === 0 ? (
                <Alert severity="info" sx={{ mb: 1 }}>
                  The firewall already matches this profile — it'll benchmark in place, then leave
                  settings unchanged.
                </Alert>
              ) : (
                <TableContainer>
                  <Table size="small">
                    <TableHead>
                      <TableRow>
                        <TableCell>Pipe</TableCell>
                        <TableCell>Field</TableCell>
                        <TableCell align="right">From</TableCell>
                        <TableCell align="right">To (during test)</TableCell>
                      </TableRow>
                    </TableHead>
                    <TableBody>
                      {testConfirm.changes.map((c, i) => (
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
              {testConfirm.warnings.length > 0 && (
                <Alert severity="warning" sx={{ mt: 1 }}>
                  {testConfirm.warnings.map((w, i) => (
                    <div key={i}>{w}</div>
                  ))}
                </Alert>
              )}
            </>
          )}
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setTestConfirm(null)}>Cancel</Button>
          <Button
            variant="contained"
            color="secondary"
            startIcon={<ScienceIcon />}
            onClick={handleConfirmTest}
          >
            Run test &amp; restore
          </Button>
        </DialogActions>
      </Dialog>

      <Dialog open={raceOpen} onClose={() => setRaceOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle>Race challengers</DialogTitle>
        <DialogContent>
          <DialogContentText sx={{ mb: 2 }}>
            Adaptively test your limited-data profiles against the current best — one
            iteration at a time, eliminating any whose best case can't overtake the best.
            The firewall is applied and benchmarked for real during the race.
          </DialogContentText>
          <Stack direction="row" spacing={2} alignItems="center" sx={{ mb: 1 }}>
            <Typography variant="body2">Time budget</Typography>
            <Select
              size="small"
              value={raceMinutes}
              onChange={(e) => setRaceMinutes(Number(e.target.value))}
            >
              {[5, 10, 15, 30, 60, 120].map((m) => (
                <MenuItem key={m} value={m}>
                  {m} min
                </MenuItem>
              ))}
            </Select>
          </Stack>
          <FormControlLabel
            control={
              <Checkbox
                checked={raceAutoPromote}
                onChange={(e) => setRaceAutoPromote(e.target.checked)}
              />
            }
            label="Auto-promote the winner (apply it instead of restoring the baseline)"
          />
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setRaceOpen(false)}>Cancel</Button>
          <Button variant="contained" color="secondary" startIcon={<ScienceIcon />} onClick={handleStartRace}>
            Start race
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
