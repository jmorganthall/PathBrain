// Shared TypeScript types mirroring the PathBrain backend API contract.

export interface Health {
  status: string;
  version: string;
}

export interface RunSummary {
  id: number;
  created_at: string;
  started_at?: string | null;
  finished_at?: string | null;
  status: string;
  label?: string | null;
  // Headline axis scores under the current methodology (null until scored/comparable).
  // `overall` is the first-class corner roll-up (the headline figure, replacing SOPS).
  overall?: number | null;
  responsiveness?: number | null;
  speed?: number | null;
  smoothness?: number | null;
  // True when the run has a score but isn't comparable under the current methodology.
  legacy?: boolean;
  iterations: number;
  iterations_completed: number;
  per_iteration_ms?: number | null;
}

export interface RunEstimate {
  per_iteration_ms: number | null;
  based_on_runs: number;
  default_iterations: number;
  max_iterations: number;
}

// Where the window's stall time came from (PRD R7). dominant is the layer to act
// on: "network" is tunable (FQ-CoDel/quantum); "render" is main-thread, not.
export interface StallAttribution {
  network_ms: number;
  render_ms: number;
  unknown_ms: number;
  dominant: "network" | "render" | "mixed" | "unknown";
}

export interface AxisStat {
  median: number;
  p25: number;
  p75: number;
  p95: number;
  min: number;
  max: number;
}

// Methodology-aware rolling window: per-axis distributions under the current
// methodology (no single SOPS), plus the per-metric breakdown + attribution.
export interface RollingScore {
  window_hours: number;
  count: number;
  methodology: string;
  axes: MethodologyAxis[];
  axis_scores: Record<string, AxisStat>;
  subscores: Record<string, number>;
  metric_values: Record<string, number>;
  weights: Record<string, number>;
  attribution?: StallAttribution | null;
}

export interface AxisSeriesPoint {
  run_id: number;
  timestamp: string;
  [axis: string]: number | string | null;
}

export interface AxisSeriesResponse {
  methodology: string;
  axes: MethodologyAxis[];
  points: AxisSeriesPoint[];
}

export interface MonitoringStatus {
  enabled: boolean;
  interval_minutes: number;
  active: boolean;
  last_run_at: string | null;
  next_run_at: string | null;
}

export interface ProfileSpread {
  count: number;
  // Total iterations behind this spread (present on the completion axis).
  iterations?: number;
  confident: boolean;
  median: number;
  p25: number;
  p75: number;
  min: number;
  max: number;
}

export interface SettingsProfile {
  fingerprint: string;
  label: string;
  settings: Array<Record<string, unknown>> | null;
  count: number;
  iterations: number;
  confident: boolean;
  first_seen: string;
  last_seen: string;
  // Top-level distribution is Smoothness (the ranking axis).
  median: number;
  p25: number;
  p75: number;
  min: number;
  max: number;
  // Speed axis distribution (the other headline axis), shown alongside.
  speed: ProfileSpread | null;
  // Completion axis distribution; null until any run in the profile captured its
  // (infra) metrics.
  completion: ProfileSpread | null;
  // Per infra-metric medians, e.g. { dns: { median, count }, tcp: {...} }.
  completion_metrics: Record<string, { median: number; count: number }>;
  // Time-adjusted SOPS: median of (run SOPS − the day×hour historical norm).
  // Positive = this profile performs above the historical average for the times
  // it actually ran. Null until any run has a usable baseline.
  relative_sops: { delta_median: number; p25: number; p75: number; count: number } | null;
  // Median 0–100 score per axis (speed/smoothness/stability/completion).
  scores: Record<string, number>;
  // Median 0–100 *subscore* per scored metric (perception-calibrated grade). Drives the axis
  // scores + the custom-crown lens — NOT the canonical Overall (which is raw-based below).
  crown_scores: Record<string, number>;
  // Each crown metric's raw measurement mapped to its 0–100 percentile (rank) within the
  // field (no methodology grading) — the exact values the Overall corners over. Percentile
  // normalization gives every metric equal spread, so no one metric dominates; the scale
  // moves only when the measurements do, never when a grading threshold changes.
  crown_norm: Record<string, number>;
  // Single "closeness to the ideal Speed=100/Smoothness=100 corner" (higher = better);
  // null until both axes exist. This IS the crown basis: the highest Overall among
  // confident profiles is "best".
  overall: number | null;
  // IQR of the per-run Overall score (its own run-to-run variation). Null until scorable.
  overall_p25: number | null;
  overall_p75: number | null;
  // Time-adjusted ("vs typical") Overall: how much this profile beats its day×hour norm.
  // Informational only — it does not feed the crown. Null until a usable baseline exists.
  relative_overall: { delta_median: number; p25: number; p75: number; count: number } | null;
  // Median of every numeric metric we collect (logical key → value), for the
  // dynamic chart axes + the table column selector.
  metrics: Record<string, number>;
}

// A selectable non-metric numeric field (axis scores + run stats) the /api/metrics
// catalog doesn't describe; metric fields get their metadata from the catalog.
export interface ProfileField {
  key: string;
  label: string;
  unit: string;
  higher_is_better: boolean;
  group: string;
}

// One planned/applied firewall field write from "Apply this profile".
export interface ApplyProfileChange {
  pipe_uuid: string;
  param: string;
  value: string | number | boolean;
  label: string;
  field: string;
  field_label: string;
  from: string | number | boolean | null;
  to: string | number | boolean | null;
}

export interface ApplyProfileResult {
  ok?: boolean;
  preview?: boolean;
  fingerprint: string;
  label: string;
  // Present on preview responses: the writes that *would* happen.
  changes?: ApplyProfileChange[];
  // Present on commit responses: the writes that happened.
  applied?: Array<{ label: string; field_label: string; to: string | number | boolean | null }>;
  warnings: string[];
  already_applied: boolean;
  resulting_fingerprint?: string | null;
  // The single-iteration benchmark kicked after applying (when run_benchmark), if any.
  run_id?: number | null;
}

export interface ProfileFieldChange {
  pipe: string;
  field: string;
  field_label: string;
  from_value: string | number | boolean | null;
  to_value: string | number | boolean | null;
  direction: "higher" | "lower" | "changed";
}

export interface ProfileDiffSide {
  fingerprint: string;
  label: string;
  // Field-normalized Overall (the crown corner we rank on), null if no crown data.
  overall: number | null;
  completion: number | null;
  // Time-adjusted Overall (median vs the day×hour norm), null if not computable.
  relative_overall: number | null;
  confident: boolean;
}

export interface ProfileDiff {
  best: ProfileDiffSide;
  comparison: ProfileDiffSide;
  // Overall gap (best − comparison); null when either side lacks a crown Overall.
  delta_abs: number | null;
  delta_pct: number | null;
  // Completion median delta (best − comparison); can move opposite to the Overall.
  completion_delta: number | null;
  // Time-adjusted advantage of best over comparison (their relative_overall gap).
  relative_delta: number | null;
  changes: ProfileFieldChange[];
}

// A pretender to the crown: a limited-data or stale profile whose *optimistic ceiling*
// (the crown corner over each metric's upper estimate — the same number the challenger
// race uses) could still clear the reigning crown's Overall. "Run these and one may
// dethrone the crown."
export interface CrownHeir {
  fingerprint: string;
  label: string;
  // Why it isn't the crown yet: "limited-data" (under the iteration minimum),
  // "stale" (confident but not re-run recently), or "untested" (no ceiling estimate yet).
  reason: "limited-data" | "stale" | "untested";
  // Optimistic ceiling Overall (0–100) and how far it clears the crown (null when either
  // the ceiling or the crown's Overall isn't yet estimable — e.g. bootstrap).
  optimistic: number | null;
  margin: number | null;
  // Current (median) Overall, iterations collected, and iterations still needed to reach
  // confidence — so the card can show "N to go".
  overall: number | null;
  iterations: number;
  iterations_to_min: number;
  confident: boolean;
  last_seen: string;
}

export interface CrownHeirs {
  // Top heirs, ranked by ceiling-above-crown (descending).
  items: CrownHeir[];
  // Every qualifying heir (drives the "N could beat your crown" badge), even beyond `items`.
  total: number;
  // How many `items` are returned (config challenger.heir_count).
  limit: number;
  // The reigning crown's Overall the ceilings are measured against (null in bootstrap).
  crown_overall: number | null;
}

// Effective best/worst threshold (and direction) a metric is *scored* with under the
// current methodology — used to flag a quadrant axis as "saturated" (every profile already
// past 'best', so its raw spread carries no score signal).
export interface MetricThreshold {
  best: number;
  worst: number;
  higher_is_better: boolean;
}

// Methodology health for one scored, non-zero-`best` metric: the share of profiles whose
// value already clears 'best' (so the metric scores ~100 and can't rank them). `flagged`
// when that share exceeds 50% — the threshold is too lenient to crown the fastest profile;
// `suggested_best` re-anchors it to the fastest value measured.
export interface MetricSaturation {
  key: string;
  label: string;
  unit: string;
  best: number;
  saturated_fraction: number;
  profiles: number;
  flagged: boolean;
  suggested_best: number | null;
  higher_is_better: boolean;
}

export interface SettingsProfilesResponse {
  profiles: SettingsProfile[];
  count: number;
  min_runs: number;
  // Total iterations a profile needs to be "confident" (the unit of signal).
  min_iterations: number;
  complete_only: boolean;
  // The crowned profile: confident and closest to the top-right (fastest+smoothest)
  // corner. Null until a confident profile with both axes exists.
  best_fingerprint: string | null;
  // The current methodology's crown metric set — the metrics the Overall corners over
  // (fcp/lcp/total_stall under v7). The table pins these as its standings columns so the
  // displayed columns are the ones that actually compute Overall.
  overall_metrics: string[];
  // Fingerprints statistically tied with the crown (co-leaders): the crown's median lead
  // over these is within run-to-run noise, so the UI flags them as a tie rather than
  // implying the crown is decisively better. Excludes the crown itself; empty when the
  // crown stands clearly apart.
  co_leaders: string[];
  // The profile the firewall is on right now (best-effort live discovery), so the UI
  // can flag the active row. Null when discovery is unavailable.
  current_fingerprint: string | null;
  // Selectable non-metric numeric fields for the chart axes + column selector.
  fields: ProfileField[];
  best_diff: ProfileDiff | null;
  // The crown's heirs — limited-data / stale profiles that could still beat it.
  heirs: CrownHeirs;
  // Per-metric effective thresholds (for the saturated-axis warning), keyed by metric key.
  metric_thresholds: Record<string, MetricThreshold>;
  // Methodology health: scored metrics whose 'best' is too lenient to rank profiles
  // (saturating >50%), with a suggested re-anchor.
  saturation: MetricSaturation[];
}

// One "Test this profile up to the minimum" session.
export interface ProfileTest {
  id: number;
  status: "pending" | "running" | "complete" | "failed";
  fingerprint: string;
  label: string | null;
  iterations: number;
  run_id: number | null;
  error: string | null;
  // Live step readout: snapshot → apply → verify → benchmark → restore → done/failed.
  stage: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  // Best-effort label of whatever holds the coordination lock (for queued tests).
  lock_owner: string | null;
}

export interface ProfileTestStart {
  id: number;
  fingerprint: string;
  iterations: number;
  current_iterations: number;
  min_iterations: number;
}

export interface ChallengerRace {
  id: number;
  status: "pending" | "running" | "complete" | "failed" | "cancelled";
  time_budget_s: number;
  auto_promote: boolean;
  iterations_run: number;
  // Iterations spent re-measuring the crowned incumbent so challengers race a
  // contemporaneous bar (counted within iterations_run).
  incumbent_refreshes: number;
  leader_fingerprint: string | null;
  leader_label: string | null;
  winner_fingerprint: string | null;
  promoted: boolean;
  eliminated: Array<{ fingerprint: string; label: string | null; reason: string }>;
  error: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  lock_owner: string | null;
}

export interface RaceStart {
  id: number;
  contenders: number;
  auto_promote: boolean;
}

export interface ProfileRefresh {
  id: number;
  status: "pending" | "running" | "complete" | "failed" | "cancelled";
  profiles_total: number;
  profiles_done: number;
  iterations_run: number;
  current_fingerprint: string | null;
  current_label: string | null;
  error: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  lock_owner: string | null;
}

export interface ProfileRefreshPreview {
  profiles: number;
  iterations: number;
  total_iterations: number;
  per_iteration_ms: number | null;
  estimated_seconds: number | null;
}

export interface VersionInfo {
  version: string;
  git_sha: string | null;
  git_sha_short: string | null;
  update_check: boolean;
  update_available: boolean;
  latest_sha: string | null;
  latest_sha_short: string | null;
  compare_url: string | null;
  error: string | null;
}

export interface ImpactSide {
  label: string;
  fingerprint: string;
  median: number;
  count: number;
  iterations?: number;
}

export interface SettingsDiagnostics {
  total_completed: number;
  stamped: number;
  unstamped: number;
  distinct_profiles: number;
  with_latest_metrics: number;
  legacy_metrics: number;
  recent: Array<{
    id: number;
    created_at: string;
    label?: string | null;
    fingerprint: string | null;
  }>;
}

export interface SettingsImpact {
  changed: boolean;
  threshold_pct: number;
  min_runs?: number;
  min_iterations?: number;
  enough_data?: boolean;
  changed_at?: string;
  delta_abs?: number;
  delta_pct?: number | null;
  significant?: boolean;
  before?: ImpactSide;
  after?: ImpactSide;
}

export interface ScoreOut {
  sops: number;
  sops_stdev?: number | null;
  sops_min?: number | null;
  sops_max?: number | null;
  subscores: Record<string, number>;
  weights_used: Record<string, number>;
  metric_values: Record<string, number>;
  // True when this score predates the current rubric's metrics (legacy).
  legacy?: boolean;

  // Completion axis (pure-infra timing) — separate from SOPS. null when the run
  // captured none of its metrics.
  completion?: number | null;
  completion_stdev?: number | null;
  completion_min?: number | null;
  completion_max?: number | null;
  completion_subscores?: Record<string, number> | null;
  completion_weights_used?: Record<string, number> | null;
  completion_metric_values?: Record<string, number> | null;
}

export interface BenchmarkResult {
  id: number;
  plugin: string;
  success: boolean;
  error?: string | null;
  duration_ms?: number | null;
  metrics: Record<string, number | null>;
  details?: Record<string, unknown> | null;
}

export interface RunBaseline {
  run_id: number;
  // "best_profile" = averaged over the profile with the highest median SOPS;
  // "all" = averaged over the most recent completed runs (fallback).
  scope: "best_profile" | "all";
  profile_fingerprint: string | null;
  profile_label: string | null;
  profile_median_sops: number | null;
  // True when the viewed run already belongs to the best-scoring profile.
  is_best_profile: boolean;
  run_count: number;
  // plugin name -> { metric_key: mean_value }
  metrics: Record<string, Record<string, number>>;
}

export interface RunDetail extends RunSummary {
  notes?: string | null;
  error?: string | null;
  settings_fingerprint?: string | null;
  settings?: Array<Record<string, unknown>> | null;
  config_used?: Record<string, unknown> | null;
  results: BenchmarkResult[];
  score: ScoreOut | null;
}

export interface SeriesPoint {
  run_id: number;
  timestamp: string;
  label?: string | null;
  overall?: number | null;
  responsiveness?: number | null;
  speed: number | null;
  smoothness: number | null;
  stability?: number | null;
  completion?: number | null;
  dns_ms: number | null;
  tcp_ms: number | null;
  tls_ms: number | null;
  ttfb_ms: number | null;
  jitter_ms: number | null;
  packet_loss_pct: number | null;
}

export interface SeriesResponse {
  points: SeriesPoint[];
}

export interface TestApplyStep {
  step: string;
  ok: boolean;
  detail: string;
}

// ── Shotgun Sweep ────────────────────────────────────────────────────────────
export interface SweepParamRange {
  enabled: boolean;
  min: number;
  max: number;
  step: number;
}

// A shaper field the sweep can vary (from /sweep/fields, driven by the shaper-field
// registry) — its label, unit, and a sensible starting range for the UI control.
export interface SweepField {
  key: string;
  label: string;
  unit: string | null;
  default: SweepParamRange;
}

export interface SweepPipe {
  uuid: string;
  label: string;
  direction?: string | null;
}

// A range per swept field (keyed by field key, e.g. "quantum"/"target") plus the pipes
// to vary. The field set is dynamic — whatever the registry marks sweepable.
export interface SweepSpec {
  [field: string]: SweepParamRange | SweepPipe[] | undefined;
  // Pipes to sweep; the parameter grid runs on each (one pipe varied at a time).
  // Omitted/empty = the single default pipe.
  pipes?: SweepPipe[];
}

export interface SweepResult {
  index: number;
  pipe_uuid?: string | null;
  pipe_label?: string | null;
  run_id: number | null;
  sops: number | null;
  created_at: string | null;
  relative: TrendRelative | null;
  // Each swept field's value for this variant (quantum/target + any future field).
  [field: string]: number | string | null | TrendRelative | undefined;
}

export interface Sweep {
  id: number;
  status: "pending" | "running" | "complete" | "cancelled" | "failed";
  dry_run: boolean;
  iterations: number;
  dwell_s: number;
  pipe_uuid: string | null;
  total_variants: number;
  completed_variants: number;
  baseline: Record<string, number | string | null> | null;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  active: boolean;
  results: SweepResult[];
}

export interface SweepPreview {
  variants: { quantum?: number; target?: string }[];
  total_variants: number;
  eta_ms: number | null;
  per_iteration_ms: number | null;
  cap: number;
}

export interface TestApplyResult {
  provider: string;
  pipe_uuid: string | null;
  pipe_label: string | null;
  param: string;
  original: number;
  test_value: number;
  changed: boolean;
  restored: boolean;
  ok: boolean;
  error: string | null;
  steps: TestApplyStep[];
}

// ── Historical trends (day-of-week × hour-of-day baselines) ──────────────────
export interface TrendCell {
  weekday: number; // 0 = Mon … 6 = Sun
  hour: number; // 0–23, viewer-local
  median: number;
  p25: number;
  p75: number;
  count: number;
}

export interface TrendHourCell {
  hour: number;
  median: number;
  p25: number;
  p75: number;
  count: number;
}

export interface TrendWeekdayCell {
  weekday: number;
  median: number;
  p25: number;
  p75: number;
  count: number;
}

export interface TrendHeatmapResponse {
  metric: string;
  label: string;
  unit: string;
  higher_is_better: boolean;
  total: number;
  window_days: number;
  cells: TrendCell[];
  by_hour: TrendHourCell[];
  by_weekday: TrendWeekdayCell[];
}

export interface TrendRelative {
  metric: string;
  label: string;
  unit: string;
  higher_is_better: boolean;
  current: number | null;
  baseline: number;
  p25: number;
  p75: number;
  count: number;
  baseline_source: "exact" | "hour" | "weekday" | "global";
  delta: number | null;
  delta_pct: number | null;
  z: number | null;
  percentile: number | null;
  better: boolean | null;
  band: "typical" | "mild" | "strong" | "unknown";
}

export interface TrendRelativeResponse {
  weekday: number;
  hour: number;
  window_hours: number;
  window_days: number;
  min_samples: number;
  metrics: Record<string, TrendRelative>;
}

export interface Threshold {
  best: number;
  worst: number;
}

export interface WeightsResponse {
  weights: Record<string, number>;
  thresholds: Record<string, Threshold>;
}

export interface FqCodelPipe {
  download_bandwidth: string | null;
  upload_bandwidth: string | null;
  quantum: number | null;
  limit: number | null;
  target: string | null;
  interval: string | null;
  ecn: boolean | null;
  flows: number | null;
  queues: number | null;
  scheduler: string | null;
  extra: Record<string, unknown>;
}

export interface DnsProvider {
  name: string;
  server: string;
}

export interface HostPort {
  host: string;
  port: number;
}

export interface BrowserConfig {
  urls: string[];
  timeout_s: number;
  wait_until: string;
  headless: boolean;
  screenshot: boolean;
  har: boolean;
  http3: boolean;
  force_quic_origins: string[];
}

export interface BenchmarkConfig {
  icmp: { targets: string[]; count: number; interval_s: number; timeout_s: number };
  dns: { providers: DnsProvider[]; hostnames: string[]; timeout_s: number };
  tcp: { targets: HostPort[]; timeout_s: number };
  tls: { targets: HostPort[]; timeout_s: number };
  http: { urls: string[]; timeout_s: number };
  browser: BrowserConfig;
  iterations: number;
  monitoring: { enabled: boolean; interval_minutes: number };
  // Settings-vs-responsiveness correlation. `min_iterations` is the maturity/confidence
  // threshold: total iterations a profile needs before it's trusted / crownable.
  correlation: {
    min_iterations: number;
    min_runs?: number;
    significant_change_pct?: number;
    crown_tie_min_margin?: number;
    crown_tie_iqr_fraction?: number;
  };
  experiment: ExperimentConfig;
  rubric_version: string;
  weights: Record<string, number>;
  thresholds: Record<string, Threshold>;
  [key: string]: unknown;
}

// A "test the current settings for X minutes" session — a time-boxed data-collection loop
// on the live profile (no firewall write). Chunked into <=5-iteration runs so partial
// completion keeps its data.
export interface CurrentTest {
  id: number;
  status: "pending" | "running" | "complete" | "failed" | "cancelled" | null;
  label: string | null;
  duration_s: number;
  iterations_run: number;
  runs_created: number;
  run_ids: number[];
  error: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  lock_owner?: string | null;
}

export interface ProviderHealth {
  provider: string;
  ok: boolean;
  [key: string]: unknown;
}

export interface DiscoverResponse {
  provider: string;
  pipes: FqCodelPipe[];
  snapshot_id: number | null;
}

export interface ConfigSnapshot {
  id: number;
  created_at: string;
  provider: string;
  label?: string | null;
  data: Record<string, unknown>;
}

export interface PluginInfo {
  name: string;
  description: string;
}

// One entry from the backend metric registry — the single source for display
// metadata (label/description/unit/direction), axis membership and rubric.
export interface MetricCatalogEntry {
  key: string;
  source_key: string;
  plugin: string;
  label: string;
  description: string;
  unit: string;
  axis: "sops" | "completion" | null;
  weight: number;
  best: number | null;
  worst: number | null;
  higher_is_better: boolean;
  // Chronological/logical display rank (lower = earlier in a page load).
  order: number;
}

export interface MetricsCatalog {
  metrics: MetricCatalogEntry[];
}

export interface ExperimentWindow {
  days: number[];
  start_hour: number;
  end_hour: number;
}

export interface ExperimentConfig {
  enabled: boolean;
  dry_run: boolean;
  auto_promote: boolean;
  window: ExperimentWindow;
  pipe_uuid: string;
  param: string;
  candidates: Array<number | string>;
  dwell_minutes: number;
  min_trials_per_value: number;
  improve_pct: number;
}

export interface ExperimentResult {
  medians: Record<string, number>;
  baseline_value: string;
  baseline_median: number | null;
  winner: string | null;
  winner_median: number | null;
  action: string;
  final_value: string;
}

export interface ExperimentSummary {
  id: number;
  created_at: string;
  finished_at: string | null;
  status: string;
  param: string;
  candidates: Array<number | string>;
  dry_run: boolean;
  baseline_value: string | null;
  trial_count: number;
  result: ExperimentResult | null;
}

export interface ExperimentTrial {
  id: number;
  created_at: string;
  value: string;
  sops: number | null;
  run_id: number | null;
  applied: boolean;
}

export interface ExperimentDetail extends ExperimentSummary {
  trials: ExperimentTrial[];
}

export interface ExperimentStatusInfo {
  enabled: boolean;
  dry_run: boolean;
  auto_promote: boolean;
  in_window: boolean;
  window: ExperimentWindow;
  param: string;
  candidates: Array<number | string>;
  active_experiment_id: number | null;
}

export interface ExperimentsResponse {
  status: ExperimentStatusInfo;
  experiments: ExperimentSummary[];
}

// ── Methodology layer (versioned interpretation) ──
export interface MethodologyAxis {
  key: string;
  label: string;
  role: string;
}

export interface MethodologyMetric {
  key: string;
  axis: string | null;
  plugin: string;
  source_key: string;
  label: string;
  description: string;
  unit: string;
  weight: number;
  best: number | null;
  worst: number | null;
  higher_is_better: boolean;
  required: boolean;
  order: number;
}

export interface MethodologyDefinition {
  axes: MethodologyAxis[];
  metrics: MethodologyMetric[];
}

export interface MethodologySummary {
  version: string;
  rubric_version: string;
  derivation_version: string;
  created_at: string | null;
  notes: string | null;
  is_current: boolean;
  axes: MethodologyAxis[];
  metric_count: number;
  scored_metric_count: number;
  required_metrics: string[];
}

export interface MethodologyDetail extends MethodologySummary {
  definition: MethodologyDefinition;
}

export interface MethodologiesResponse {
  methodologies: MethodologySummary[];
  count: number;
}

export type Comparability = "exact" | "partial" | "incomparable";

export interface RunScore {
  run_id: number;
  methodology_version: string;
  is_at_measure: boolean;
  comparability: Comparability;
  missing_metrics: string[];
  axis_scores: Record<string, number>;
  subscores: Record<string, number>;
  weights_used: Record<string, number>;
  metric_values: Record<string, number>;
  bands: Record<string, { stdev?: number; min?: number; max?: number }>;
  computed_at: string | null;
}

export interface RunScoresResponse {
  run_id: number;
  at_measure_version: string | null;
  scores: RunScore[];
}

export interface RegradeSummary {
  methodology: string;
  total: number;
  scored: number;
  exact: number;
  partial: number;
  incomparable: number;
  skipped: number;
}

// Returned by the heavy async endpoints (regrade/rescore/rederive): they kick off a
// background job and hand back its id; progress is tracked in the jobs feed.
export interface JobStart {
  job_id: string;
}

// One entry in the universal "running jobs" feed (GET /api/jobs).
export interface Job {
  id: string;
  kind: string; // regrade | rescore | rederive | run | sweep | profile_test | experiment
  label: string;
  status: "running" | "succeeded" | "failed";
  current: number | null;
  total: number | null;
  message: string | null;
  error: string | null;
  href: string | null;
  started_at: string;
  finished_at: string | null;
}

export interface JobsResponse {
  jobs: Job[];
  running: number;
}

// Consolidated raw export (GET /history/dump). The shape is intentionally loose —
// it's a debugging/analysis payload rendered as raw JSON, not a typed view model.
export interface DataDumpRun {
  id: number;
  created_at: string | null;
  status: string;
  label: string | null;
  iterations: number;
  settings_fingerprint: string | null;
  score: Record<string, unknown> | null;
  results: Array<Record<string, unknown>>;
  [key: string]: unknown;
}

export interface DataDump {
  generated_at: string;
  count: number;
  limit: number;
  runs: DataDumpRun[];
}

// Profile-centric AI export: each profile's tunable settings → runs → raw scoring metrics,
// plus the methodology objective and the shaper field model. Purpose-built to feed an LLM
// that suggests new (untested) profiles. Typed loosely — the payload is deeply nested and
// consumed as raw JSON (view / copy / download).
export interface OptimizerExport {
  generated_at: string;
  profile_count: number;
  runs_per_profile_limit: number;
  [key: string]: unknown;
}

// AI (OpenRouter) settings, as returned to the UI — the API key is masked to a hint.
export interface AiConfig {
  configured: boolean;
  key_hint: string;
  model: string;
  prompt: string;
  default_prompt: string;
}

export interface AiModel {
  id: string;
  name: string;
  context_length: number | null;
  prompt_price: string | null;
}

// One proposed profile from the model: a settings object (only the fields it's changing) +
// a rationale. `settings` shape is model-authored, so it's loosely typed.
export interface AiSuggestion {
  settings?: Record<string, unknown>;
  rationale?: string;
  [key: string]: unknown;
}

// One deterministic settings→outcome relationship computed server-side (Spearman ρ over the
// exported profiles): a writable field on a pipe vs a crown metric.
export interface FieldSensitivity {
  pipe: string;
  field: string;
  field_label: string;
  metric: string;
  metric_label: string;
  spearman: number | null;
  n: number;
  distinct_values: number;
  metric_direction: "increases" | "decreases" | "none";
  effect: "improves" | "worsens" | "none";
  summary: string;
}

// The model's own interpreted relationship (its read of the levers), separate from the
// deterministic map above.
export interface AiRelationship {
  pipe?: string;
  field?: string;
  metric?: string;
  direction?: "inverse" | "linear" | "none" | string;
  confidence?: string;
  evidence?: string;
  [key: string]: unknown;
}

export interface AiSuggestResult {
  model: string;
  raw: string;
  suggestions: AiSuggestion[];
  // The model's interpreted settings→metric relationships (may be empty if it omitted them).
  relationships?: AiRelationship[];
  // The deterministic relationships we computed and sent to the model.
  field_sensitivity?: FieldSensitivity[];
  usage: Record<string, number>;
  profiles_sent: number | null;
  // Size of the JSON payload sent to the model, so the UI can show how big the request was.
  payload_bytes?: number | null;
}

// One Server-Sent Event from the streaming suggest endpoint (/ai/suggest/stream).
export type AiStreamEvent =
  | {
      type: "meta";
      profiles_sent: number | null;
      payload_bytes: number;
      model: string;
      field_sensitivity?: FieldSensitivity[];
    }
  | { type: "reasoning"; delta: string }
  | { type: "content"; delta: string }
  | {
      type: "done";
      model: string;
      raw: string;
      reasoning: string;
      suggestions: AiSuggestion[];
      relationships?: AiRelationship[];
      usage: Record<string, number>;
    }
  | { type: "error"; error: string };
