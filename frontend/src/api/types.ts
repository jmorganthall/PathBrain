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
}

export interface MonitoringStatus {
  enabled: boolean;
  interval_minutes: number;
  active: boolean;
  last_run_at: string | null;
  next_run_at: string | null;
}

export interface SettingsProfile {
  fingerprint: string;
  label: string;
  settings: Array<Record<string, unknown>> | null;
  count: number;
  confident: boolean;
  first_seen: string;
  last_seen: string;
  median: number;
  p25: number;
  p75: number;
  min: number;
  max: number;
}

export interface SettingsProfilesResponse {
  profiles: SettingsProfile[];
  count: number;
  min_runs: number;
}

export interface ImpactSide {
  label: string;
  fingerprint: string;
  median: number;
  count: number;
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

export interface ExperimentsResponse {
  experiments: unknown[];
  status: string;
  message: string;
}
