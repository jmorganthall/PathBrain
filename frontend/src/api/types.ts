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
  // `label` is the technical settings summary ("wan: 900Mbit q1514 t5ms") — what the
  // profile IS. `name` is its call sign ("Speedy Sloth") — what to CALL it. Views lead
  // with the name and keep the summary as secondary detail.
  label: string;
  name?: string;
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
  // "% vs SQM off": percent improvement of this profile's Overall over the honest unshaped
  // baseline (the best Overall among measured "SQM off" profiles). Positive = shaping helps;
  // negative = this profile is worse than turning SQM off. Derived from the methodology's
  // Overall, so it re-derives when the methodology changes. Null when no baseline exists yet
  // or the profile has no Overall.
  pct_vs_sqm_off: number | null;
  // True when this profile is itself an "SQM off" baseline (a pipe with shaping disabled).
  is_sqm_off: boolean;
  // Setup-stripped decomposition: the Overall re-cornered over fcp/lcp with each run's own
  // nav dns+tcp+tls subtracted. API-only — no longer rendered anywhere on Settings Impact
  // (the measured-weather `weather_relative` below is the one surfaced "vs weather" reading).
  weather_adjusted_overall: number | null;
  // Time-adjusted ("vs typical") Overall: how much this profile beats its day×hour norm.
  // Informational only — it does not feed the crown; no longer a surfaced column
  // (superseded by weather_relative). Null until a usable baseline exists.
  relative_overall: { delta_median: number; p25: number; p75: number; count: number } | null;
  // "Wins above the weather": per-run Overall vs the median of OTHER profiles' runs in the
  // same measured-weather severity band (conditions from each run's own probe instruments,
  // not the clock). coverage = fraction of runs that had a usable cohort. Flag-and-steer
  // only — never a crown input. Null when no run had a cohort.
  weather_relative: {
    delta_median: number;
    p25: number;
    p75: number;
    count: number;
    coverage: number;
  } | null;
  // Median measured-weather severity (0–100 percentile; higher = harsher conditions) this
  // profile has been sampled under — a sampling-fairness readout.
  weather_severity: number | null;
  // Residual standing far above raw standing → "there may be something here".
  weather_beater: boolean;
  // "Overall (recent)": the crown grade recomputed over only the most recent
  // crown_window_iterations — the drift lens (what the Overall would be on current
  // evidence). The ranked `overall` pools all time. Null when the window is disabled,
  // the methodology isn't weighted, or the window lacks a required crown subscore.
  overall_recent: number | null;
  recent_iterations: number;
  recent_scores: Record<string, number>;
  // Current form vs prior record: recent-window median Overall vs the rest of history,
  // significance-gated both directions. "fading" = pooled Overall propped by a past it no
  // longer delivers; "rising" = pooled Overall understates its present. Null when there
  // isn't enough history to split. Flag-and-steer only — never a crown input.
  form: {
    recent: number;
    prior: number;
    delta: number;
    threshold: number;
    direction: "rising" | "fading" | "steady";
    recent_n: number;
    prior_n: number;
  } | null;
  // Median of every numeric metric we collect (logical key → value), for the
  // dynamic chart axes + the table column selector.
  metrics: Record<string, number>;
  /** Which verdict placed this profile: the ring measured it, or pooled seeded it. */
  verdict_source?: "ring" | "pooled" | "unmeasured";
  /** Position under the primary ordering (1 = best). */
  primary_rank?: number | null;
  /** The ring's fitted strength and how many rounds stand behind it (null if unraced). */
  ring_rating?: number | null;
  ring_rounds?: number | null;
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
  name?: string | null;
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
  name?: string;
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

// One (weather covariate × crown metric) correlation from GET /settings/weather-sensitivity.
// `within_profile_spearman` is the causal signal (profile held fixed); `pooled_spearman` mixes
// in between-profile differences. `clean` covariates are profile-orthogonal (usable to adjust);
// shaped ones are moved by the shaper itself and shown for transparency only.
export interface WeatherSensitivityRow {
  covariate: string;
  covariate_label: string;
  clean: boolean;
  role: string | null;
  metric: string;
  metric_label: string;
  metric_higher_is_better: boolean;
  pooled_spearman: number | null;
  pooled_n: number;
  within_profile_spearman: number | null;
  within_profile_profiles: number;
  metric_direction: "increases" | "decreases" | "none";
  weather_sensitive: boolean;
}

export interface WeatherSensitivity {
  crown_metrics: string[];
  covariates: { key: string; clean: boolean; role: string | null; label: string }[];
  within_profile_min_points: number;
  trend_rho: number;
  runs_analyzed: number;
  profiles_analyzed: number;
  rows: WeatherSensitivityRow[];
}

export interface SettingsProfilesResponse {
  profiles: SettingsProfile[];
  count: number;
  min_runs: number;
  // Total iterations a profile needs to be "confident" (the unit of signal).
  min_iterations: number;
  complete_only: boolean;
  // The "SQM off" baseline Overall that "% vs SQM off" is measured against (best Overall
  // among measured SQM-off profiles). Null until a baseline test has run.
  sqm_off_overall: number | null;
  // The POOLED crown: confident and highest all-time Overall. Kept as its own field and
  // deliberately NOT re-pointed at the ring — the duel's matchmaking reads it as the
  // independent opinion, and pointing it at the ring would make the ladder choose who gets
  // checked against the ladder.
  best_fingerprint: string | null;
  // The field's primary ordering — the ring where it has real head-to-head evidence,
  // pooled where it doesn't. `profiles` arrives sorted by it.
  ranking?: "ring" | "pooled";
  primary_best_fingerprint?: string | null;
  primary_best_source?: "ring" | "pooled" | "unmeasured";
  // How many profiles each verdict is placing.
  ring_rated_count?: number;
  seeded_count?: number;
  // The current methodology's crown metric set — the metrics the Overall corners over
  // (fcp/lcp/stall_time under v8). The table pins these as its standings columns so the
  // displayed columns are the ones that actually compute Overall.
  overall_metrics: string[];
  // Fingerprints statistically tied with the crown (co-leaders): the crown's median lead
  // over these is within run-to-run noise, so the UI flags them as a tie rather than
  // implying the crown is decisively better. Excludes the crown itself; empty when the
  // crown stands clearly apart.
  co_leaders: string[];
  // The measured signal-to-noise behind the #1 verdict: the crown's Overall + its standard
  // error, the gap to the runner-up, and the significance threshold (σ · pooled SE of the
  // medians). `clear_lead` is true only when the gap clears that bar. Null when no crown.
  crown_confidence: CrownConfidence | null;
  // The profile the firewall is on right now (best-effort live discovery), so the UI
  // can flag the active row. Null when discovery is unavailable.
  current_fingerprint: string | null;
  // When the "vs weather" residual ranking's top profile differs from the raw crown:
  // the crown may be weather-confounded — race these two. Null when they agree.
  weather_crown_suspect: {
    fingerprint: string;
    label: string;
    delta_median: number;
    coverage: number;
  } | null;
  // The ghost-crown signal: the crown's current form significantly trails its own prior
  // record, so the bar challengers must clear may be stale. Null when steady/rising.
  crown_fading: ({ fingerprint: string; label: string } & NonNullable<SettingsProfile["form"]>) | null;
  // The recent-evidence window the informational `overall_recent` column is computed
  // over (iterations per profile; 0 = column disabled). The verdict pools all time.
  crown_window_iterations: number;
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
  // Which of the two lengths ran: "top_up" (to the confidence minimum) or "exact" (the
  // caller named a count — a re-measurement, which a confident profile can still have).
  mode?: "top_up" | "exact";
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
  // Winner-first context: when set, only the top-N profiles run, ranked by their Overall
  // under `ranked_by` (the prior methodology). Both null for a full, unranked batch.
  top?: number | null;
  ranked_by?: string | null;
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
  // True when a one-click self-update is wired up (Watchtower HTTP API configured); the
  // "Update now" button is shown only then.
  self_update: boolean;
  // Transparency: which upstream the check compares against, and when it last looked — so
  // "up to date" isn't a black box.
  update_repo?: string;
  update_branch?: string;
  checked_at?: string | null;
  error: string | null;
}

export interface UpdateTriggerResult {
  triggered: boolean;
  detail?: string;
  attempt_id?: number | null;
}

// One recorded "Update now" attempt. `outcome` is how the CALL went; `verdict` is whether the
// build actually changed — the only thing that answers "did the update work?", and the reason
// this is persisted rather than logged (a successful update replaces the container's log).
export interface UpdateAttempt {
  id: number;
  created_at: string | null;
  url: string | null;
  token_sent: boolean;
  outcome: "requested" | "accepted" | "dropped" | "rejected" | "unreachable" | "not_configured" | string;
  http_status: number | null;
  response_body: string | null;
  error: string | null;
  elapsed_ms: number | null;
  git_sha_before: string | null;
  git_sha_after: string | null;
  verdict: "pending" | "confirmed" | "no_change" | "failed" | string;
  verdict_at: string | null;
  detail: string | null;
}

export interface UpdateLog {
  attempts: UpdateAttempt[];
  running_sha: string | null;
  verify_after_seconds: number;
}

export interface UpdateConfig {
  configured: boolean;
  url: string | null;
  token_set: boolean;
}

export interface CrownConfidence {
  overall: number;
  overall_se: number;
  runner_up_overall: number | null;
  gap_to_runner_up: number | null;
  noise_threshold: number | null;
  sigma: number;
  clear_lead: boolean;
  co_leader_count: number;
  confident_count: number;
}

export interface DerivationCohort {
  checked: number;
  drifting: number;
  consistent: boolean;
  drift_metrics: string[];
}

export interface CollectionShape {
  runs: number;
  urls: string[];
  loaf_present_frac: number;
  loaf_sources: string[];
  median_resources: Record<string, number>;
}

export interface CollectionComparison {
  urls_added: string[];
  urls_removed: string[];
  loaf_changed: boolean;
  loaf_present: { old: number; new: number };
  resource_shift: Record<string, { old: number; new: number }>;
  changed: boolean;
  oldest: CollectionShape;
  newest: CollectionShape;
}

export interface DerivationAudit {
  fingerprint: string;
  total_runs: number;
  current_derivation: string;
  oldest: DerivationCohort;
  newest: DerivationCohort;
  consistent: boolean;
  stale_history: boolean;
  collection: CollectionComparison;
}

export interface UpdateConnectionTest extends UpdateConfig {
  reachable: boolean;
  status: "ok" | "unreachable" | "not_configured";
  detail: string;
}

export interface ImpactSide {
  label: string;
  // The profile's call sign — what the sentence should read as ("Tall Garland"), with the
  // settings summary above kept as the detail for whoever wants the numbers.
  name?: string | null;
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

// "Where's the pause?" diagnostic: the single longest void in one page load, which phase it
// falls in, and whether it's byte-delivery (network) or main-thread (render) bound.
export interface PauseDiagnostic {
  url: string;
  start_ms: number;
  end_ms: number;
  duration_ms: number;
  phase: "pre_fcp" | "fcp_lcp" | "lcp_load" | "post_load";
  attribution: "network" | "render" | "mixed" | "unknown" | null;
  fcp_ms: number | null;
  lcp_ms: number | null;
  load_ms: number | null;
}

// Profile-level roll-up of the "Where's the pause?" diagnostic — the median longest void per URL
// across a profile's runs, with the dominant phase and network/render attribution split.
export interface ProfilePauseUrl {
  url: string;
  runs: number;
  median_void_ms: number;
  phase: "pre_fcp" | "fcp_lcp" | "lcp_load" | "post_load";
  phase_fraction: number;
  attribution: "network" | "render" | "mixed" | "unknown" | null;
  network_fraction: number | null;
  render_fraction: number | null;
}

export interface ProfilePauseRollup {
  fingerprint: string;
  runs: number;
  run_cap: number;
  urls: ProfilePauseUrl[];
}

export interface RunDetail extends RunSummary {
  notes?: string | null;
  error?: string | null;
  settings_fingerprint?: string | null;
  settings?: Array<Record<string, unknown>> | null;
  config_used?: Record<string, unknown> | null;
  results: BenchmarkResult[];
  score: ScoreOut | null;
  pause_diagnostics?: PauseDiagnostic[] | null;
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
  // The current methodology's crown measurements (what we rank on today), so the UI can
  // feature the same day×hour "vs typical" matrix for them without a hardcoded set.
  crown_metrics?: string[];
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

export interface BaselinePipeState {
  uuid: string | null;
  label: string | null;
  enabled: boolean;
}

export interface BaselineTest {
  id: number;
  status: "pending" | "running" | "complete" | "failed" | "cancelled" | null;
  trigger: "manual" | "scheduled" | string;
  iterations: number;
  settle_s: number;
  iterations_run: number;
  runs_created: number;
  run_ids: number[];
  baseline: BaselinePipeState[];
  error: string | null;
  stage: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  lock_owner?: string | null;
}

export interface BaselineConfig {
  enabled: boolean;
  hour: number;
  minute: number;
  iterations: number;
  settle_seconds: number;
  // IANA zone the hour/minute are interpreted in (the browser zone captured on save);
  // "" = container-local fallback.
  timezone: string;
  next_run_at: string | null;
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
  // Which version scores runs "at present", the version this build ships as latest, and the
  // config pin (null when unpinned → follows code_default). current_version < code_default ⇒
  // pinned to an older rubric.
  current_version?: string;
  code_default?: string;
  pinned?: string | null;
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
  kind: string; // regrade | rescore | rederive | run | run_series | sweep | profile_test | experiment
  label: string;
  status: "running" | "succeeded" | "failed";
  current: number | null;
  total: number | null;
  message: string | null;
  // The technical settings summary behind a job's call sign ("Download: 880Mbit q3550 t3
  // i60 ecn | …"). The row leads with the name, which is what identifies a profile at a
  // glance; this is the detail, shown on hover rather than wrapped over three lines.
  detail?: string | null;
  error: string | null;
  href: string | null;
  // When set, this job is a chunk nested under the broader job with this id (the parent line).
  parent_id?: string | null;
  // When set, POST here to cancel this job (a chunk cancels itself; a parent cancels the whole
  // operation). Absent → not cancellable.
  cancel_url?: string | null;
  started_at: string;
  finished_at: string | null;
  // Milliseconds remaining as of this response — deliberately a duration, not a formatted
  // string (which can't be counted down) and not an absolute server timestamp (which would
  // be read against the browser's clock, putting any skew straight into the number). The
  // client anchors it to its own clock on arrival and ticks from there; each poll re-anchors.
  // null when the job genuinely can't be estimated — better than a fabricated countdown.
  eta_ms?: number | null;
  // How the estimate was reached: "scheduled" (a time-boxed job's real deadline),
  // "measured" (units left x measured unit cost), "observed" (this job's own rate so far),
  // "queued" (the job hasn't started — this is how much work it is, not how long is left).
  eta_basis?: "scheduled" | "measured" | "observed" | "queued" | null;
  // True while the job is still waiting on the coordination lock. Its clock hasn't started,
  // so `eta_ms` is a duration of work rather than a remaining time and must NOT be ticked
  // down — a queued job's finish time moves out for as long as it waits.
  queued?: boolean;
  // Expected duration of ONE unit of this job's work — an iteration, a sweep variant, a
  // profile. The same number the ETA is built from, so the bar and the countdown can't
  // form separate opinions about how fast the job is going. It lets the bar advance
  // *within* the unit in progress instead of standing still until the counter ticks;
  // null when the job has no units (a time-boxed window) or none has finished yet.
  unit_ms?: number | null;
  // The full length of a TIME-BOXED job's window (a duel session, a challenger race, "test
  // current for 20 minutes"), null for a job made of countable units. Those jobs have no
  // unit total at all, so the bar had nothing to draw but the indeterminate sweep — which
  // reads the same at minute one of a six-hour duel as at minute three hundred. With the
  // window, `eta_ms` (what's left) over this (what it's left of) is real, exact progress:
  // the share of the agreed window already spent, from the same deadline the countdown
  // beside it is anchored on.
  window_ms?: number | null;
  // How long this job has held the benchmark pipeline without reporting progress (null =
  // it is progressing, or isn't the holder). It replaces the countdown when set: a
  // time-boxed job past its deadline floors at "finishing…", which is precisely the
  // reading a wedged job gives for as long as it stays wedged.
  stalled_ms?: number | null;
}

// The gate every benchmark session queues on. A feed of "waiting" rows says everyone is
// waiting; this says what they are waiting on, and whether it is still alive.
export interface PipelineStatus {
  busy: boolean;
  owner: string | null;
  held_for_s: number | null;
  // Seconds since the holder last showed progress. Not the same question as held_for_s —
  // a duel window is meant to run for hours, so age proves nothing and silence is what
  // distinguishes a session from a wedge.
  stalled_for_s: number | null;
  stale_after_s: number;
  waiting: number;
  evictions: number;
  last_eviction: { owner: string; quiet_s: number; held_s: number; at: number } | null;
}

export interface JobsResponse {
  jobs: Job[];
  running: number;
  pipeline?: PipelineStatus;
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

// One lever's "what the top-Overall profiles share" contrast (top quartile vs the rest).
export interface LeverSignature {
  pipe: string;
  field: string;
  field_label: string;
  pattern: "higher" | "lower" | "sweet_spot" | "none";
  top_value: number;
  top_range: [number, number];
  field_range: [number, number];
  field_median: number;
  shift: number | null;
  concentration: number | null;
  cliffs_delta: number | null;
  top_n: number;
  rest_n: number;
  summary: string;
}

export interface TopProfileSignature {
  available?: boolean;
  reason?: string;
  top_profiles?: number;
  rest_profiles?: number;
  top_fraction?: number;
  levers?: LeverSignature[];
}

// One deterministic "collect more data here" recommendation: a lever with a promising but
// under-sampled signal, and the values to measure next.
export interface CoverageGap {
  pipe: string;
  field: string;
  field_label: string;
  distinct_values: number;
  measured_range: [number, number];
  overall_rho: number | null;
  pattern: "higher" | "lower" | "sweet_spot" | "none" | null;
  sweepable: boolean;
  action: "extend_lower" | "extend_higher" | "resolve";
  suggested_values: number[];
  rationale: string;
  priority: number | null;
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
  // The model's data-collection requests (where it wants more data before trusting a signal).
  data_requests?: Record<string, unknown>[];
  // The deterministic relationships we computed and sent to the model.
  field_sensitivity?: FieldSensitivity[];
  // What the top-Overall profiles share, per lever (catches sweet spots correlations miss).
  top_profile_signature?: TopProfileSignature;
  // Deterministic "collect more data here" recommendations (promising but under-sampled levers).
  coverage_gaps?: CoverageGap[];
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
      top_profile_signature?: TopProfileSignature;
      coverage_gaps?: CoverageGap[];
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
      data_requests?: Record<string, unknown>[];
      usage: Record<string, number>;
    }
  | { type: "error"; error: string };

// ── Crown follower ("Follow best") ─────────────────────────────────────────────────

export interface CrownFollowConfig {
  enabled: boolean;
  interval_minutes: number;
  // The first-class CROWNING POLICY: which verdict the follower acts on.
  // "pooled" = the all-time Overall argmax; "duel" = the duel ladder's fresh champion
  // (pooled fallback when no decisive fresh verdict exists).
  policy: "pooled" | "duel";
}

// The duel ladder's latest fresh champion (crowning candidate under policy "duel").
export interface DuelChampion {
  fingerprint: string;
  label: string | null;
  // Call sign, resolved by fingerprint (absent on payloads built before naming).
  name?: string | null;
  duel_id: number;
  finished_at: string;
  decisive: boolean;
  // From the standings: consecutive sessions ended holding the belt, and whether the fit
  // behind it is still mostly prior.
  consecutive_sessions?: number;
  provisional?: boolean;
}

// The result of one crown-follower check (tracking + optional apply).
export interface CrownCheckResult {
  checked_at: string;
  enabled: boolean;
  crown_fingerprint: string | null;
  crown_label: string | null;
  // The profile's call sign ("Speedy Sloth"), resolved by fingerprint at read time so a
  // rename lands here too. The label beside it is the technical settings summary — every
  // view leads with the name and keeps the summary as the detail.
  crown_name?: string | null;
  crown_changed: boolean;
  // The crowning-policy resolution this check acted on.
  policy?: "pooled" | "duel";
  governing_fingerprint?: string | null;
  governing_label?: string | null;
  governing_name?: string | null;
  governing_source?: "pooled" | "duel";
  governing_detail?: string;
  duel_champion?: DuelChampion | null;
  live_fingerprint: string | null;
  on_crown: boolean | null;
  applied: boolean;
  apply_skipped: string | null;
  error: string | null;
}

// Crown-churn statistics: how often the crowned best profile changes.
export interface CrownFollowStats {
  tracked_since: string | null;
  total_changes: number;
  changes_24h: number;
  changes_7d: number;
  changes_30d: number;
  changes_per_day: number | null;
  distinct_crowns_30d: number;
  last_change_at: string | null;
  current_crown_fingerprint: string | null;
  current_crown_label: string | null;
  current_crown_name?: string | null;
  current_reign_hours: number | null;
  mean_reign_hours: number | null;
  median_reign_hours: number | null;
}

// One crown-ledger row: a crown change, or a follower firewall apply.
export interface CrownEventOut {
  id: number;
  created_at: string | null;
  kind: "change" | "apply" | string;
  fingerprint: string;
  previous_fingerprint: string | null;
  label: string | null;
  previous_label: string | null;
  // Call signs for the same two fingerprints (see CrownCheckResult.crown_name).
  name?: string | null;
  previous_name?: string | null;
  overall: number | null;
  applied: boolean;
  error: string | null;
  detail: string | null;
}

export interface CrownFollowStatus {
  config: CrownFollowConfig;
  // The selectable crowning policies + the duel ladder's latest fresh champion (or null),
  // so the popover can show both verdicts side by side whatever the active policy.
  policies: ("pooled" | "duel")[];
  duel_champion: DuelChampion | null;
  status: { last_check_at: string | null; last_result: CrownCheckResult | null };
  stats: CrownFollowStats;
  events: CrownEventOut[];
}

// ── Both crowns side by side (GET /settings/crowns) ─────────────────────────────────
// The pooled crown answers "best record across all history we've measured"; the duel
// champion answers "who beat whom, same weather, head to head". They can disagree — the
// Dashboard shows both and marks which one automation follows.

export interface PooledCrown {
  fingerprint: string;
  label: string | null;
  name?: string | null;
  overall: number | null;
  since: string | null;
  reign_hours: number | null;
  // The crown's LIVE pooled Overall — the ledger `overall` above was recorded at
  // crowning time and can be days stale; this one shares a vintage with the duel side.
  overall_now?: number | null;
  overall_iterations?: number;
}

export interface DuelCrown {
  fingerprint: string;
  label: string | null;
  name?: string | null;
  duel_id: number;
  finished_at: string | null;
  consecutive_sessions: number;
  decisive: boolean;
  // False once the verdict ages past `freshness_days` — shown, but marked expired.
  fresh: boolean;
  freshness_days: number;
  wins: number;
  losses: number;
  draws: number;
  matchups: number;
  beaten: string[];
  // The champion's LIVE pooled Overall (same scale and vintage as the pooled side's
  // overall_now) — the number that lets the two crowns be compared at all.
  overall_now?: number | null;
  overall_iterations?: number;
}

export interface CrownsOut {
  policy: "pooled" | "duel";
  pooled: PooledCrown | null;
  duel: DuelCrown | null;
  // Champion's live pooled Overall minus the crown's — how far apart the two verdicts
  // sit on the one scale they share. Null until both have a live Overall.
  overall_delta?: number | null;
  governing: { source: "pooled" | "duel"; fingerprint: string | null; detail: string };
  agree: boolean;
  follow_enabled: boolean;
  on_crown: boolean | null;
  checked_at: string | null;
}

// ── The fight card: who fights whom if a duel started now ───────────────────────────

export interface DuelCardEntry {
  position: number;
  fingerprint: string;
  name: string | null;
  label: string | null;
  overall: number | null;
  iterations: number | null;
  confident: boolean | null;
  // Why it's in the queue: a contender near the crown, a limited-data/stale heir, or untested.
  reason: string;
  // What the RING says: its fitted head-to-head rating, the optimistic ceiling the queue is
  // ordered by, and the reason it sits where it does under the "ring" model.
  rating?: number | null;
  ceiling?: number | null;
  ring_why?: string | null;
  // Priority tier: 0 pooled crown, 1 contender, 2 untested. The ladder never gives the ring
  // to a lower tier while a higher one still has someone waiting.
  tier?: number;
  tier_name?: string;
  // Already decided against the champion within the rematch window. NOT a skip — the
  // cooldown only orders within a tier, so this bout still runs, just after its equals.
  on_cooldown: boolean;
}

export interface DuelCard {
  incumbent: {
    fingerprint: string;
    name: string | null;
    label: string | null;
    overall: number | null;
    iterations: number | null;
    // Why this profile defends: the reigning champion carrying its belt in, or the pooled
    // crown standing in because there's no fresh decisive champion.
    why?: string;
    is_duel_champion?: boolean;
  } | null;
  queue: DuelCardEntry[];
  total?: number;
  contenders: "ring" | "leaders" | "heirs";
  top_n: number;
  rematch_hours?: number;
  champion_freshness_days?: number;
  rating_prior_pairs?: number;
  rank_sigma?: number;
  iterations_per_round?: number;
  // The belt-holder's own ring rating — the bar every ceiling is measured against.
  incumbent_rating?: number | null;
  contender_modes?: string[];
  // Set when there's nothing to race (e.g. no confident crown yet).
  reason: string | null;
}

// ── Duel ladder (head-to-head adjudication) ─────────────────────────────────────────

// What the stopping rule actually demands of a bout, computed server-side. A pair cap
// below the rule's reach makes every matchup a draw — invisible from the raw numbers.
export interface DuelDecisionCost {
  // Fastest possible verdict: the fewest consistently one-sided pairs that can decide.
  sweep_pairs: number | null;
  // The same thing in plain language: N wins in a row ends a bout on the spot.
  streak_pairs?: number | null;
  // Pair-wins rule only: the win COUNT needed at the cap (null under the margins rule,
  // which judges by how much each pair was won, not how many).
  wins_needed: number | null;
  win_rate_needed: number | null;
  // Margins rule only: the peek-corrected threshold actually applied, and the correction.
  nominal_alpha?: number;
  peek_penalty?: number;
  // True when the rule can mostly (or never) reach a verdict at these settings.
  restrictive: boolean;
}

export interface DuelPreset {
  key: "snap" | "quick" | "balanced" | "strict";
  label: string;
  alpha: number;
  min_pairs: number;
  max_pairs: number;
  streak_wins: number;
  // Measured consequences, not adjectives — what this choice actually does.
  summary: string;
  detail: string;
}

export interface DuelConfig {
  enabled: boolean;
  hour: number;
  minute: number;
  // The window's finish time, derived server-side from start + duration. The page edits
  // the schedule as a start/finish pair and PUTs both; `duration_minutes` stays the
  // canonical value the engine counts down.
  end_hour: number;
  end_minute: number;
  timezone: string;
  duration_minutes: number;
  min_pairs: number;
  max_pairs: number;
  min_margin: number;
  rematch_hours: number;
  champion_freshness_days: number;
  rank_sigma?: number;
  rating_prior_pairs?: number;
  // Seconds to let the link settle after writing a profile before measuring it. Applied
  // to both sides of every pair, so it never favours a side.
  settle_seconds: number;
  // The evidence bar: the edge worth detecting, and the false-positive rate.
  p1: number;
  alpha: number;
  // How a bout is judged: "margins" (paired signed-rank — uses HOW MUCH each pair was
  // won by) or "pair_wins" (the legacy sign test — counts winners, ignores margins).
  method: "margins" | "pair_wins";
  methods: ("margins" | "pair_wins")[];
  // The single dial that answers "how sure before calling a winner". Hand-editing any
  // derived field reads back as "custom" rather than pretending a preset is active.
  preset: "snap" | "quick" | "balanced" | "strict" | "custom";
  // An explicit "N wins in a row ends it" (0 = derived from the statistical threshold).
  streak_wins: number;
  // Run the ladder perpetually rather than once a night, and the pause between sessions.
  continuous: boolean;
  continuous_gap_minutes: number;
  // Who the champion fights: the profiles nearest the crown, or the exploring heirs order.
  contenders: "ring" | "leaders" | "heirs";
  contender_top_n: number;
  // Which rule names the champion. "lineal" — you take the belt by beating its holder,
  // provided your whole shared record then favours you on BOTH matches and rounds.
  // "rating_floor" — the ring's #1 by proven rating. The standings rank on the floor
  // either way; this only decides who wears the belt.
  crown_rule: "lineal" | "rating_floor";
  crown_rules: string[];
  /** Benchmark iterations per leg of a round — divides the round's noise by sqrt(k). */
  iterations_per_round?: number;
  presets: DuelPreset[];
  decision: DuelDecisionCost;
}

export interface DuelMatchup {
  incumbent: string;
  challenger: string;
  incumbent_label: string;
  challenger_label: string;
  // Call signs recorded at duel time (absent on bouts fought before naming existed).
  incumbent_name?: string | null;
  challenger_name?: string | null;
  // Why this challenger got the ring: "pooled crown", "contender", "untested", possibly
  // with ", re-raced (…)". Absent on bouts fought before matchmaking recorded it.
  challenger_why?: string | null;
  pairs: number;
  wins_incumbent: number;
  wins_challenger: number;
  median_delta: number | null;
  llr_incumbent: number;
  llr_challenger: number;
  /**
   * "aborted" means no result — the ladder could not measure this match. It is NOT a draw
   * (a verdict), and matches written before the distinction carry `verdict: "draw"` with an
   * aborted reason; read it through `matchOutcome`, never off this field directly.
   */
  verdict: "incumbent" | "challenger" | "draw" | "aborted";
  reason: string;
  /** How many rounds were discarded for having no Overall, and the causes. */
  unusable_rounds?: number;
  unusable_why?: Record<string, number> | null;
}

/** GET /duel/health — is the ladder measuring anything? */
export interface DuelHealth {
  matches: number;
  decided: number;
  drawn: number;
  aborted: number;
  aborted_share: number | null;
  unusable_rounds: number;
  diagnosed_matches: number;
  reasons: { reason: string; legs: number }[];
  sessions_analyzed: number;
}

// ── The head-to-head league table (GET /duel/standings) ──────────────────────────────
// A ranking earned in the ring: decided matchups only, nothing pooled or averaged over
// history. Each row is one profile's record across every duel session in the ledger.

export interface DuelStanding {
  rank: number;
  fingerprint: string;
  label: string;
  // Call sign, resolved by fingerprint — so rows recorded before naming read correctly.
  name?: string;
  matchups: number;
  wins: number;
  losses: number;
  draws: number;
  // Match points: win 3 / draw 1. Kept as a readable ledger column — it is NOT the sort
  // any more, because it records how many you beat but not who.
  points: number;
  // Bradley-Terry strength fitted to every pair on the ledger, on the Elo scale (1500 =
  // middle of the field). This is what the standings rank on: beating a strong profile
  // moves it a lot, beating a weak one barely at all, and profiles that never met are
  // still comparable through shared opponents.
  rating: number | null;
  rating_se: number | null;
  // rating − 1 SE: what the record has DEMONSTRATED rather than what it suggests. This is
  // the default order — a five-pair record can fit a high rating, but its error bar is
  // wide, so it has to be measured before it can lead a table of forty-pair records.
  rating_floor: number | null;
  rating_pairs: number | null;
  // Too few pairs for the fit to say much — mostly the prior talking.
  rating_provisional: boolean;
  // The leader's rating does not clearly stand above this one: the gap is inside the
  // ring's own noise (`tie_sigma` x the pooled SE of the two ratings). A flag on a strict
  // order, never a shared rank — sharing one would say two profiles are equal when one of
  // them beat the other, and statistical ties are non-transitive so the table cannot
  // honestly be cut into bands. Same treatment the pooled crown gives `co_leaders`.
  tied_with_leader?: boolean;
  // Extra head-to-head pairs before that gap would clear the bar, if the ratings hold.
  // The actionable half of the flag: "race them N more rounds" beats "these are tied".
  // null when it is not the leader, not tied, or not reachable in a sane number of rounds.
  pairs_to_separate?: number | null;
  // Pairs the fit expected this profile to win against the opponents it actually faced.
  expected_pair_wins: number | null;
  // Share of *decided* matchups won (draws excluded); null with no decisive matchup.
  win_rate: number | null;
  pair_wins: number;
  pair_losses: number;
  pair_win_rate: number | null;
  // Median Overall-point margin signed from THIS profile's point of view (+ = better).
  median_margin: number | null;
  opponents: number;
  beaten: string[];
  lost_to: string[];
  // Sessions this profile ended as the ladder's final incumbent.
  championships: number;
  is_champion: boolean;
  last_dueled_at: string | null;
  last_duel_id: number | null;
  // The POOLED (all-history measured) Overall for this profile, so the ring record and
  // the raw record can be read against each other. Null when it has no comparable runs.
  overall?: number | null;
  pooled_iterations?: number;
}

export interface DuelHeadToHeadCell {
  wins: number;
  losses: number;
  draws: number;
  pairs: number;
  median_margin: number | null;
}

// The champion is NOT row 1, and under the lineal rule it is not meant to be. The table
// ranks on `rating_floor` — what a record has demonstrated across the whole network —
// while the belt records who beat whom. A champion sitting at row 4 is the two verdicts
// disagreeing, which is the reason for running both. Derived by replaying the ledger, not
// read from a stored per-session value.
export interface DuelChampionStanding {
  fingerprint: string;
  label: string | null;
  name?: string | null;
  // The most recent completed session it fought in (null if it has only fought in the
  // session currently running).
  duel_id: number | null;
  finished_at: string | null;
  // How many consecutive completed sessions have ended with this profile on top.
  consecutive_sessions: number;
  // Has it actually beaten (or lost to) anyone, or is its whole record draws?
  decisive: boolean;
  // Its rating still rests on too few pairs / too few opponents to be established.
  provisional?: boolean;
  // Where it sits on the OTHER verdict — the standings' proven-rating order.
  rank?: number | null;
  // Which rule named it: "lineal" (beat the holder) or "rating_floor" (the ring's #1).
  rule?: string | null;
  // Successful title defences since it took the belt, and how many times the belt has
  // changed hands over the whole ledger.
  defences?: number | null;
  title_changes?: number | null;
  title_bouts?: number | null;
  // The profile it took the title from.
  took_it_from?: string | null;
}

export interface DuelStandings {
  champion: DuelChampionStanding | null;
  standings: DuelStanding[];
  // head_to_head[a][b] = a's record against b.
  head_to_head: Record<string, Record<string, DuelHeadToHeadCell>>;
  sessions_analyzed: number;
  matchups_analyzed: number;
  decisive_matchups: number;
  generated_from: number;
  // What the default order means, and the pair count below which a rating is provisional.
  ranked_by?: string;
  // How many standard errors the default order subtracts from the fitted rating.
  rank_sigma?: number;
  // Fingerprints the leader does not clearly stand above. Information about the order,
  // never a change to it.
  co_leaders?: string[];
  // Standard errors of the difference a lead must clear to be called real.
  tie_sigma?: number;
  provisional_pairs?: number;
  rating_pairs_total?: number;
}

/** One bout on a profile's own tape, signed from that profile's side. */
export interface DuelProfileBout {
  duel_id: number;
  finished_at: string | null;
  session_status: string;
  // Which corner it fought from. The verdict doesn't depend on it (the lead alternates
  // within a bout), but "defended" and "challenged" are different stories about a record.
  role: "defended" | "challenged";
  opponent: string;
  opponent_label: string;
  opponent_name?: string | null;
  label: string;
  result: "win" | "loss" | "draw";
  pairs: number;
  pair_wins: number;
  pair_losses: number;
  // Median Overall-point margin from THIS profile's point of view (+ = it was better).
  margin: number | null;
  reason?: string | null;
  method?: string | null;
  p_value?: number | null;
  challenger_why?: string | null;
  lead_alternated?: boolean | null;
}

/** This profile's record against one opponent. */
export interface DuelProfileOpponent {
  fingerprint: string;
  name?: string | null;
  wins: number;
  losses: number;
  draws: number;
  pairs: number;
  median_margin: number | null;
  label?: string | null;
  /** The opponent's pooled Overall, and this profile's Overall minus theirs. */
  overall?: number | null;
  overall_delta?: number | null;
  duel_rank?: number | null;
  /** Did the ring decide anything here, or is the record all draws? */
  decisive: boolean;
}

/**
 * One profile's record in the ring (GET /duel/profile/{fingerprint}).
 *
 * The head-to-head verdict beside the pooled one the profile page already shows — what this
 * profile has BEATEN, not what it averaged. `record` is null for a profile that has never
 * fought, which is the ordinary case, not an error.
 */
export interface DuelProfileLedger {
  fingerprint: string;
  name?: string | null;
  label?: string | null;
  in_ring: boolean;
  record: DuelStanding | null;
  rank_of: number;
  champion: DuelChampion | null;
  is_champion: boolean;
  opponents: DuelProfileOpponent[];
  /** How this profile's ring record stands against the pooled Overall ranking. */
  versus_overall?: {
    beat_higher_overall: number;
    lost_to_lower_overall: number;
    decided_opponents: number;
    undecided_opponents: number;
    overall: number | null;
  };
  bouts: DuelProfileBout[];
  sessions_analyzed: number;
  matchups_analyzed: number;
  ranked_by?: string;
  rank_sigma?: number;
  provisional_pairs?: number;
}

/**
 * The bout in progress, as structured state rather than prose.
 *
 * A scoreline inside a sentence ("pair 4 (2-1)") can't say WHOSE wins those are, by how
 * much, or how near the bout is to a verdict — which is all anyone watching a duel wants.
 * Margins are challenger − incumbent in Overall points, so a positive number always means
 * the challenger is ahead. Null while a session is between bouts or finished.
 */
export interface DuelLive {
  bout: number;
  pairs: number;
  incumbent: { fingerprint: string | null; name: string | null; label: string | null; wins: number };
  challenger: {
    fingerprint: string | null;
    name: string | null;
    label: string | null;
    wins: number;
    why: string;
  };
  leader: "incumbent" | "challenger" | "level";
  median_margin: number | null;
  last_margin: number | null;
  margins: number[];
  min_pairs: number;
  max_pairs: number;
  min_margin: number;
  p_value: number | null;
  alpha: number;
  streak: { length: number; side: "incumbent" | "challenger" | null; needed: number };
}

export interface DuelSession {
  id: number;
  status: "pending" | "running" | "complete" | "failed" | "cancelled" | null;
  stage: string | null;
  live?: DuelLive | null;
  trigger: string;
  duration_s: number;
  matchups: DuelMatchup[];
  iterations_run: number;
  run_ids: number[];
  champion_fingerprint: string | null;
  champion_label: string | null;
  // `matchups` is capped by the API to the most recent few; this is the true count.
  matchups_total?: number;
  error: string | null;
  created_at: string | null;
  started_at: string | null;
  finished_at: string | null;
  lock_owner: string | null;
}


// ── The exploration landscape: the shaper's parameter space, its holes, and what to
//    measure next. Read-only — nothing here applies or runs anything.

export interface ExploreAxis {
  // "<pipe>::<field>" — the Download and Upload legs are separate levers throughout.
  key: string;
  pipe: string;
  field: string;
  field_label: string;
  unit: string | null;
  sweepable: boolean;
  values: number[];
  min: number;
  max: number;
  distinct: number;
  measured: number;
}

export interface ExploreCurvePoint {
  value: number;
  overall: number;
  best: number;
  profiles: number;
}

export interface ExploreCurve {
  key: string;
  pipe: string;
  field: string;
  field_label: string;
  unit: string | null;
  sweepable: boolean;
  curve: ExploreCurvePoint[];
  spearman: number | null;
  best_value: number;
  best_overall: number;
  // The best value tested is the highest or lowest tried — the optimum isn't bracketed.
  best_at_edge: boolean;
  shape: string;
  // Curve points whose OTHER levers are systematically skewed — the point is measuring two
  // levers at once, which is how a marginal curve names a value best that isn't.
  imbalance?: {
    value: number;
    profiles: number;
    other: string;
    other_label: string;
    shift: number;
    detail: string;
  }[];
  confounded?: boolean;
}

// A controlled comparison found inside the observational record: profiles that differ in
// exactly ONE lever, so the difference in their Overall is that lever's effect with no
// confounding at all. This is the strongest evidence the measured field can give.
export interface ExploreMatchedPairs {
  key: string;
  pipe: string;
  field: string;
  field_label: string;
  unit: string | null;
  total_pairs: number;
  transitions: {
    from: number;
    to: number;
    pairs: number;
    median_delta: number;
    worst: number;
    best: number;
    // Every matched pair agreed on the sign — far stronger than a median over a mix.
    consistent: boolean;
  }[];
}

// A lever's curve restricted to profiles that are otherwise like the reference — "what
// happens if I change THIS profile", rather than "how do profiles with this value score".
export interface ExploreConditionedCurve {
  key: string;
  pipe: string;
  field: string;
  field_label: string;
  unit: string | null;
  reference_value: number | null;
  curve: { value: number; overall: number; profiles: number; exact: boolean }[];
}

// A local maximum under one-lever moves: no measured sibling beats it. Several separated
// basins mean the levers are coupled and a one-lever-at-a-time search gets stuck.
export interface ExploreBasin {
  fingerprint: string;
  name: string | null;
  label: string;
  overall: number;
  iterations: number;
  siblings: number;
  levers_from_better: number | null;
  coords: Record<string, number>;
}

export interface ExploreInteraction {
  a: string;
  b: string;
  a_label: string;
  b_label: string;
  a_split: number;
  b_split: number;
  cells: { a_high: boolean; b_high: boolean; overall: number; profiles: number }[];
  contrast: number;
  // Spread between the four corners — the contrast is judged against it, so a pair isn't
  // called "interacting" on rounding noise.
  corner_spread: number;
  interacts: boolean;
  summary: string;
}

export interface ExploreGap {
  key: string;
  pipe: string;
  field: string;
  field_label: string;
  unit: string | null;
  // "gap" = a hole between two tested values (bracketed, one run settles it).
  // "edge" = the best value is the end of the range (not bracketed — look further out).
  kind: "gap" | "edge" | string;
  from: number;
  to: number;
  suggest: number;
  width_fraction: number | null;
  detail: string;
  // The profile that would fill the hole: the best measured one with this single lever
  // moved to `suggest`, priced exactly like a headline candidate so the row can post it.
  // Absent only when no measured profile carries this lever.
  candidate?: ExploreCandidate;
}

export interface ExploreCandidate {
  changes: {
    key: string;
    pipe: string;
    field: string;
    field_label: string;
    unit: string | null;
    from: number;
    to: number;
    why: string;
  }[];
  parent: { fingerprint: string; name: string | null; label: string; overall: number };
  coords: Record<string, number>;
  predicted: number;
  uncertainty: number;
  // What the parent's Overall was worth as a starting point after the winner's-curse
  // shrink (best-of-many-noisy-medians is biased high; the anchor slides toward the
  // settled field's median by what the parent's own error bar can't rule out). A
  // prediction below the parent's headline number is this correction, not a typo.
  anchor?: number;
  anchor_se?: number;
  // predicted - best_overall > the field's noise floor: this candidate proposes a
  // difference the field can actually express. Absent when no floor is computable.
  beats_noise?: boolean;
  // predicted + exploration_weight * uncertainty — "how good could this be?", which is the
  // question worth spending a night on, not "how good do we expect it to be?".
  upside: number;
  beats_best_by: number;
  nearest_measured: number;
  // How the prediction was arrived at, per changed lever: a controlled matched pair beats
  // the marginal curve, and a number from the latter must never read like a measurement.
  evidence: string[];
  // Moves two levers at once — the prediction adds their effects, which the basin
  // structure shows they may not do. Its uncertainty is widened to say so.
  multi_lever: boolean;
  summary: string;
  // Per-pipe writable overrides, ready to POST to /settings/test-settings.
  settings: Record<string, unknown>[];
  // Only on a gap's variant: every candidate parent has already been to this value, so the
  // hole is closed. Flagged rather than dropped — "already measured" is a real answer.
  already_measured?: boolean;
}

export interface ExplorePoint {
  fingerprint: string;
  name: string | null;
  label: string;
  overall: number;
  iterations: number;
  confident: boolean;
  coords: Record<string, number>;
}

// ── The recommendation ledger ─────────────────────────────────────────────────────────
// Explore's output is a prediction, and a prediction nobody scores is a horoscope. The
// claim is written down before the test runs; the verdict is derived from the measured
// field on every read, so a re-grade or fresh runs move it.

export interface ExploreTestRequest {
  settings: unknown;
  label?: string;
  // Omitted → top up to the confidence minimum. A number → run exactly that many.
  iterations?: number;
  // The profile the candidate branches from: the levers are applied to ITS stored settings,
  // not to whatever the firewall is currently on, so the test measures what was proposed.
  parent_fingerprint?: string;
  parent_overall?: number;
  changes?: unknown;
  evidence?: string[];
  multi_lever?: boolean;
  predicted?: number;
  uncertainty?: number;
  upside?: number;
  best_overall?: number | null;
  summary?: string;
}

export interface ExploreTestResult {
  id: number;
  fingerprint: string;
  iterations: number;
  label: string | null;
  existing_iterations?: number;
  recommendation_id: number | null;
  // Set when the measurement is not a faithful reproduction of the proposal.
  note: string | null;
}

// "pending" = nothing measured yet; "incomparable" = proposed under another methodology, so
// the prediction and the measurement aren't on the same scale; otherwise the claim is graded
// against its own stated band.
export type ExploreVerdict =
  | "pending"
  | "incomparable"
  | "unscored"
  | "on_target"
  | "better"
  | "worse"
  // The firewall never held the full proposal (a field it can't write, or a value its
  // selects don't offer, was dropped before benchmarking) — the benchmark measured the
  // closest reachable profile, not the claim, so grading it would charge a plumbing
  // failure to the model's evidence class. Excluded from calibration.
  | "unreachable";

export interface ExploreRecommendation {
  id: number;
  created_at: string | null;
  fingerprint: string;
  name: string | null;
  label: string | null;
  summary: string | null;
  parent: {
    fingerprint: string | null;
    name: string | null;
    overall: number | null;
    overall_now: number | null;
  };
  changes: ExploreCandidate["changes"];
  evidence: string[];
  // Weakest-link: a candidate priced part from a matched pair and part from a confounded
  // curve is scored as confounded.
  evidence_kind: string;
  evidence_label: string;
  multi_lever: boolean;
  predicted: number | null;
  uncertainty: number | null;
  upside: number | null;
  best_overall: number | null;
  methodology_version: string | null;
  iterations_requested: number;
  profile_test_id: number | null;
  note: string | null;
  actual: number | null;
  iterations: number;
  verdict: ExploreVerdict;
  band: number;
  error: number | null;
  provisional: boolean;
  stale_methodology: boolean;
  beat_best: boolean | null;
  beat_parent: boolean | null;
  // One sentence joining the miss to the kind of evidence it was priced from.
  why: string;
  // How many times this same proposal was tested. The rows are collapsed to one per
  // proposal: two attempts resolve to the same profile and the same measurement, so
  // counting them separately would say the model had been checked twice when it was
  // checked once.
  attempts: number;
  attempt_ids: number[];
  first_proposed_at: string | null;
  other_predictions?: number[];
}

export interface ExploreCalibration {
  recorded: number;
  graded: number;
  pending: number;
  incomparable: number;
  // Claims whose test never measured the proposal (see the "unreachable" verdict) —
  // reported so they're visible, never counted in the calibration.
  unreachable?: number;
  on_target: number;
  better: number;
  worse: number;
  mean_error: number | null;
  mean_abs_error: number | null;
  hit_rate: number | null;
  beat_best: number | null;
  beat_best_claimed: number | null;
  by_evidence: {
    kind: string;
    label: string;
    graded: number;
    on_target: number;
    mean_error: number | null;
    mean_abs_error: number | null;
  }[];
}

export interface ExploreLedger {
  recommendations: ExploreRecommendation[];
  // Raw claim count before collapsing duplicates — nothing is hidden, just not double-counted.
  attempts_recorded?: number;
  summary: ExploreCalibration;
  methodology_version: string;
  min_iterations: number;
  quick_iterations: number;
}

export interface ExploreLandscape {
  axes: ExploreAxis[];
  points: ExplorePoint[];
  curves: ExploreCurve[];
  interactions: ExploreInteraction[];
  gaps: ExploreGap[];
  candidates: ExploreCandidate[];
  matched_pairs: ExploreMatchedPairs[];
  conditioned_curves: ExploreConditionedCurve[];
  basins: ExploreBasin[];
  reference: { fingerprint: string; name: string | null; label: string; overall: number } | null;
  condition_max_other_changes?: number;
  best_overall: number | null;
  profiles_modelled: number;
  confident_only: boolean;
  exploration_weight?: number;
  // The Overall gap two settled profiles need before they're distinguishable at all
  // (2σ × pooled SE of two medians — the same machinery as the crown-tie check). Null
  // when the field carries no spread information.
  noise_floor?: number | null;
  // False when candidates exist but none of them predicts an edge over the best measured
  // profile bigger than the noise floor — the honest state of a packed field, where the
  // right next measurement is the coverage gaps, not a refinement.
  candidates_clear_noise?: boolean | null;
  reason: string | null;
}
