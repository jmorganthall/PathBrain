// Small typed API client for the PathBrain backend. Uses plain fetch.
import type {
  BenchmarkConfig,
  ConfigSnapshot,
  DiscoverResponse,
  ExperimentsResponse,
  Health,
  MonitoringStatus,
  PluginInfo,
  ProviderHealth,
  RollingScore,
  RunDetail,
  RunEstimate,
  RunSummary,
  ScoreOut,
  SeriesResponse,
  WeightsResponse,
} from "./types";

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

  // Results
  latestResult: () => request<RunDetail>("/results/latest"),
  result: (id: number) => request<RunDetail>(`/results/${id}`),

  // History
  history: (limit = 50) => request<RunSummary[]>(`/history?limit=${limit}`),
  historySeries: (limit = 100) =>
    request<SeriesResponse>(`/history/series?limit=${limit}`),

  // Score
  weights: () => request<WeightsResponse>("/score/weights"),
  scorePreview: (body: Record<string, Record<string, number>>) =>
    request<ScoreOut>("/score/preview", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  score: (id: number) => request<ScoreOut>(`/score/${id}`),
  rollingScore: (hours = 24) => request<RollingScore>(`/score/rolling?hours=${hours}`),

  // Monitoring
  monitoring: () => request<MonitoringStatus>("/monitoring"),

  // Config
  config: () => request<BenchmarkConfig>("/config"),
  updateConfig: (partial: Record<string, unknown>) =>
    request<BenchmarkConfig>("/config", {
      method: "PUT",
      body: JSON.stringify(partial),
    }),
  resetConfig: () => request<BenchmarkConfig>("/config/reset", { method: "POST" }),
  providerHealth: () => request<ProviderHealth>("/config/provider"),
  discover: () => request<DiscoverResponse>("/config/discover", { method: "POST" }),
  snapshots: () => request<ConfigSnapshot[]>("/config/snapshots"),

  // Plugins & experiments
  plugins: () => request<PluginInfo[]>("/plugins"),
  experiments: () => request<ExperimentsResponse>("/experiments"),
};
