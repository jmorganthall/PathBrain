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
  sops?: number | null;
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

export interface RollingScore {
  window_hours: number;
  count: number;
  median: number | null;
  p25: number | null;
  p75: number | null;
  min: number | null;
  max: number | null;
  subscores: Record<string, number>;
  metric_values: Record<string, number>;
  weights: Record<string, number>;
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
  median: number;
  p25: number;
  p75: number;
  min: number;
  max: number;
  // Perceptual axis (Responsiveness Score) distribution; null until any run in
  // the profile captured paint metrics.
  responsiveness: ProfileSpread | null;
  // Per paint-metric medians, e.g. { fcp: { median, count }, lcp: {...} }.
  perceptual_metrics: Record<string, { median: number; count: number }>;
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
  median: number;
  responsiveness: number | null;
  confident: boolean;
}

export interface ProfileDiff {
  best: ProfileDiffSide;
  comparison: ProfileDiffSide;
  delta_abs: number;
  delta_pct: number | null;
  // Responsiveness median delta (best − comparison); can move opposite to SOPS.
  responsiveness_delta: number | null;
  changes: ProfileFieldChange[];
}

export interface SettingsProfilesResponse {
  profiles: SettingsProfile[];
  count: number;
  min_runs: number;
  best_diff: ProfileDiff | null;
}

export interface ImpactSide {
  label: string;
  fingerprint: string;
  median: number;
  count: number;
}

export interface SettingsDiagnostics {
  total_completed: number;
  stamped: number;
  unstamped: number;
  distinct_profiles: number;
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

  // Perceptual axis (Responsiveness Score) — separate from SOPS. null when the
  // run captured no paint metrics.
  responsiveness?: number | null;
  responsiveness_stdev?: number | null;
  responsiveness_min?: number | null;
  responsiveness_max?: number | null;
  perceptual_subscores?: Record<string, number> | null;
  perceptual_weights_used?: Record<string, number> | null;
  perceptual_metric_values?: Record<string, number> | null;
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
  sops: number | null;
  sops_min?: number | null;
  sops_max?: number | null;
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
  experiment: ExperimentConfig;
  rubric_version: string;
  weights: Record<string, number>;
  thresholds: Record<string, Threshold>;
  [key: string]: unknown;
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
