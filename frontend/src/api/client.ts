// Small typed API client for the PathBrain backend. Uses plain fetch.
import type {
  AxisSeriesResponse,
  BenchmarkConfig,
  ConfigSnapshot,
  DiscoverResponse,
  ExperimentDetail,
  ExperimentsResponse,
  Health,
  MetricsCatalog,
  MonitoringStatus,
  PluginInfo,
  ProviderHealth,
  RollingScore,
  RunBaseline,
  RunDetail,
  RunEstimate,
  RunSummary,
  ScoreOut,
  SeriesResponse,
  SettingsDiagnostics,
  ApplyProfileResult,
  MethodologiesResponse,
  MethodologyDetail,
  RegradeSummary,
  RunScoresResponse,
  SettingsImpact,
  SettingsProfilesResponse,
  Sweep,
  SweepPipe,
  SweepPreview,
  SweepSpec,
  TestApplyResult,
  TrendHeatmapResponse,
  TrendRelativeResponse,
  WeightsResponse,
} from "./types";

// Minutes to add to UTC to reach the viewer's local time. getTimezoneOffset()
// returns local-behind-UTC minutes, so negate it.
export const tzOffsetMinutes = () => -new Date().getTimezoneOffset();

const BASE = "/api";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      /* ignore parse errors */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export const api = {
  health: () => request<Health>("/health"),

  // Runs
  triggerRun: (body: { label?: string; notes?: string; iterations?: number }) =>
    request<RunDetail>("/run", { method: "POST", body: JSON.stringify(body) }),
  runEstimate: () => request<RunEstimate>("/runs/estimate"),
  cancelRun: (id: number) => request<RunDetail>(`/runs/${id}/cancel`, { method: "POST" }),

  // Results
  latestResult: () => request<RunDetail>("/results/latest"),
  result: (id: number) => request<RunDetail>(`/results/${id}`),
  resultBaseline: (id: number) => request<RunBaseline>(`/results/${id}/baseline`),

  // History
  history: (limit = 50, offset = 0) =>
    request<RunSummary[]>(`/history?limit=${limit}&offset=${offset}`),
  historyCount: () => request<{ count: number }>("/history/count"),
  historySeries: (limit = 100, includeLegacy = false) =>
    request<SeriesResponse>(`/history/series?limit=${limit}&include_legacy=${includeLegacy}`),

  // Score
  weights: () => request<WeightsResponse>("/score/weights"),
  scorePreview: (body: Record<string, Record<string, number>>) =>
    request<ScoreOut>("/score/preview", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  score: (id: number) => request<ScoreOut>(`/score/${id}`),
  rollingScore: (hours = 24, fingerprint?: string) =>
    request<RollingScore>(
      `/score/rolling?hours=${hours}` + (fingerprint ? `&fingerprint=${encodeURIComponent(fingerprint)}` : "")
    ),
  axisSeries: (limit = 100, fingerprint?: string) =>
    request<AxisSeriesResponse>(
      `/score/axis-series?limit=${limit}` + (fingerprint ? `&fingerprint=${encodeURIComponent(fingerprint)}` : "")
    ),
  // Methodology layer (versioned interpretation)
  methodologies: () => request<MethodologiesResponse>("/methodologies"),
  methodologyCurrent: () => request<MethodologyDetail>("/methodologies/current"),
  methodology: (version: string) =>
    request<MethodologyDetail>(`/methodologies/${encodeURIComponent(version)}`),
  runScores: (id: number) => request<RunScoresResponse>(`/score/${id}/methodologies`),
  regradeHistory: () => request<RegradeSummary>("/score/regrade", { method: "POST" }),

  // Monitoring
  monitoring: () => request<MonitoringStatus>("/monitoring"),

  // Settings correlation
  settingsProfiles: (completeOnly = true) =>
    request<SettingsProfilesResponse>(
      `/settings/profiles?complete_only=${completeOnly}&tz_offset=${tzOffsetMinutes()}`
    ),
  settingsImpact: (completeOnly = true) =>
    request<SettingsImpact>(`/settings/impact?complete_only=${completeOnly}`),
  settingsBackfill: () =>
    request<{ updated: number; fingerprint: string }>("/settings/backfill", { method: "POST" }),
  settingsDiagnostics: () => request<SettingsDiagnostics>("/settings/diagnostics"),
  // Write a stored profile to the firewall. preview=true returns the planned
  // field changes without writing, so the UI can confirm an exact diff first.
  applyProfile: (fingerprint: string, preview = false) =>
    request<ApplyProfileResult>("/settings/apply-profile", {
      method: "POST",
      body: JSON.stringify({ fingerprint, preview }),
    }),

  // Config
  config: () => request<BenchmarkConfig>("/config"),
  updateConfig: (partial: Record<string, unknown>) =>
    request<BenchmarkConfig>("/config", {
      method: "PUT",
      body: JSON.stringify(partial),
    }),
  resetConfig: () => request<BenchmarkConfig>("/config/reset", { method: "POST" }),
  adoptRubric: () => request<BenchmarkConfig>("/config/adopt-rubric", { method: "POST" }),
  rescoreHistory: () =>
    request<{ rescored: number; rubric_version: string }>("/score/rescore", { method: "POST" }),
  rederiveHistory: () =>
    request<{ rederived: number; derivation_version: string }>("/score/rederive", { method: "POST" }),
  providerHealth: () => request<ProviderHealth>("/config/provider"),
  discover: () => request<DiscoverResponse>("/config/discover", { method: "POST" }),
  testApply: () => request<TestApplyResult>("/config/test-apply", { method: "POST" }),

  // Shotgun Sweep
  sweepPipes: () => request<{ pipes: SweepPipe[] }>("/sweep/pipes"),
  sweepPreview: (body: { spec: SweepSpec; iterations: number; dwell_minutes: number }) =>
    request<SweepPreview>("/sweep/preview", { method: "POST", body: JSON.stringify(body) }),
  startSweep: (body: {
    spec: SweepSpec;
    iterations: number;
    dwell_minutes: number;
    dry_run: boolean;
    pipe_uuid?: string | null;
  }) =>
    request<Sweep>(`/sweep?tz_offset=${tzOffsetMinutes()}`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  sweepCurrent: () =>
    request<{ sweep: Sweep | null }>(`/sweep/current?tz_offset=${tzOffsetMinutes()}`),
  cancelSweep: (id: number) =>
    request<{ cancelling: boolean }>(`/sweep/${id}/cancel`, { method: "POST" }),
  applySweepBest: (id: number) =>
    request<{ ok: boolean; applied: Record<string, unknown>; run_id: number | null; sops: number | null }>(
      `/sweep/${id}/apply-best`,
      { method: "POST" }
    ),
  snapshots: () => request<ConfigSnapshot[]>("/config/snapshots"),

  // Plugins
  plugins: () => request<PluginInfo[]>("/plugins"),

  // Metric registry (single source of truth for metric metadata)
  metrics: () => request<MetricsCatalog>("/metrics"),

  // Historical trends (day-of-week × hour-of-day baselines + relative reading)
  trendsHeatmap: (metric: string, days?: number) =>
    request<TrendHeatmapResponse>(
      `/trends/heatmap?metric=${encodeURIComponent(metric)}&tz_offset=${tzOffsetMinutes()}` +
        (days != null ? `&days=${days}` : "")
    ),
  trendsRelative: (windowHours?: number, days?: number) =>
    request<TrendRelativeResponse>(
      `/trends/relative?tz_offset=${tzOffsetMinutes()}` +
        (windowHours != null ? `&window_hours=${windowHours}` : "") +
        (days != null ? `&days=${days}` : "")
    ),

  // Experiment engine
  experiments: () => request<ExperimentsResponse>("/experiments"),
  experiment: (id: number) => request<ExperimentDetail>(`/experiments/${id}`),
  abortExperiment: () => request<{ aborted: boolean }>("/experiments/abort", { method: "POST" }),
};
