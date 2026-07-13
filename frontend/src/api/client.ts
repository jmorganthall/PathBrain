// Small typed API client for the PathBrain backend. Uses plain fetch.
import type {
  AxisSeriesResponse,
  BaselineConfig,
  BaselineTest,
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
  CurrentTest,
  RunDetail,
  RunEstimate,
  RunSummary,
  ScoreOut,
  SeriesResponse,
  SettingsDiagnostics,
  ApplyProfileResult,
  MethodologiesResponse,
  MethodologyDetail,
  JobStart,
  JobsResponse,
  RunScoresResponse,
  ChallengerRace,
  AiConfig,
  AiModel,
  AiStreamEvent,
  AiSuggestResult,
  DataDump,
  OptimizerExport,
  ProfilePauseRollup,
  DerivationAudit,
  ProfileTest,
  ProfileTestStart,
  ProfileRefresh,
  ProfileRefreshPreview,
  RaceStart,
  SettingsImpact,
  VersionInfo,
  UpdateTriggerResult,
  UpdateConfig,
  UpdateConnectionTest,
  SettingsProfilesResponse,
  Sweep,
  SweepField,
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

// Fired whenever a call starts a background job (test to minimum, run, race, sweep, …) so the
// jobs badge can refresh immediately instead of waiting out its idle poll interval.
export const JOBS_REFRESH_EVENT = "pathbrain:jobs-refresh";
export const notifyJobsChanged = () => {
  if (typeof window !== "undefined") window.dispatchEvent(new Event(JOBS_REFRESH_EVENT));
};

// Wrap a job-starting request so a successful start nudges the jobs badge to poll now.
function startingJob<T>(p: Promise<T>): Promise<T> {
  return p.then((r) => {
    notifyJobsChanged();
    return r;
  });
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
    this.name = "ApiError";
  }
}

async function request<T>(
  path: string,
  init?: RequestInit,
  opts?: { timeoutMs?: number },
): Promise<T> {
  // Optional client-side timeout so a slow/oversized request fails with a useful message
  // instead of the browser's opaque "Load failed" after some intermediary drops it.
  const controller = opts?.timeoutMs ? new AbortController() : null;
  const timer = controller
    ? setTimeout(() => controller.abort(), opts!.timeoutMs)
    : null;
  let res: Response;
  try {
    res = await fetch(`${BASE}${path}`, {
      headers: { "Content-Type": "application/json" },
      signal: controller?.signal,
      ...init,
    });
  } catch (e) {
    // fetch() rejects (TypeError "Load failed" / "Failed to fetch") only when NO HTTP response
    // arrived: an abort/timeout, a dropped connection, or an oversized request. Translate it
    // into something actionable rather than surfacing the raw browser text.
    if (controller?.signal.aborted) {
      throw new ApiError(
        0,
        `Request timed out after ${Math.round((opts!.timeoutMs || 0) / 1000)}s. ` +
          "The payload may be too large — try fewer profiles or runs per profile.",
      );
    }
    throw new ApiError(
      0,
      "Couldn't reach the server (connection dropped or request too large). " +
        "If you raised the profile / runs-per-profile count, lower it and retry.",
    );
  } finally {
    if (timer) clearTimeout(timer);
  }
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
    startingJob(request<RunDetail>("/run", { method: "POST", body: JSON.stringify(body) })),
  runEstimate: () => request<RunEstimate>("/runs/estimate"),
  cancelRun: (id: number) => request<RunDetail>(`/runs/${id}/cancel`, { method: "POST" }),

  // "Test current for X minutes": time-boxed collection on the live profile.
  currentTestStart: (minutes: number) =>
    startingJob(
      request<CurrentTest>("/current/test", { method: "POST", body: JSON.stringify({ minutes }) }),
    ),
  currentTestStatus: () => request<CurrentTest>("/current/test"),
  currentTestCancel: () =>
    request<{ cancelled: boolean; status: string | null }>("/current/test/cancel", { method: "POST" }),

  // Baseline (SQM off) test: disable shaping on every pipe, settle, benchmark, restore.
  baselineConfig: () => request<BaselineConfig>("/baseline/config"),
  baselineConfigSave: (body: Partial<Omit<BaselineConfig, "next_run_at">>) =>
    request<BaselineConfig>("/baseline/config", { method: "PUT", body: JSON.stringify(body) }),
  baselineTestStart: (body: { iterations?: number; settle_seconds?: number }) =>
    startingJob(
      request<BaselineTest>("/baseline/test", { method: "POST", body: JSON.stringify(body) }),
    ),
  baselineTestStatus: () => request<BaselineTest>("/baseline/test"),
  baselineTestCancel: () =>
    request<{ cancelled: boolean; status: string | null }>("/baseline/test/cancel", { method: "POST" }),

  // Results
  latestResult: () => request<RunDetail>("/results/latest"),
  result: (id: number) => request<RunDetail>(`/results/${id}`),
  resultBaseline: (id: number) => request<RunBaseline>(`/results/${id}/baseline`),

  // History
  history: (limit = 50, offset = 0, fingerprint?: string) =>
    request<RunSummary[]>(
      `/history?limit=${limit}&offset=${offset}` +
        (fingerprint ? `&fingerprint=${encodeURIComponent(fingerprint)}` : "")
    ),
  historyCount: (fingerprint?: string) =>
    request<{ count: number }>(
      "/history/count" + (fingerprint ? `?fingerprint=${encodeURIComponent(fingerprint)}` : "")
    ),
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
  // Profile-level roll-up of the "Where's the pause?" diagnostic across the profile's runs.
  profilePauses: (fingerprint: string) =>
    request<ProfilePauseRollup>(`/results/profile/${encodeURIComponent(fingerprint)}/pauses`),
  // Read-only data-integrity audit: re-derive a profile's oldest + newest runs from raw and
  // report whether the stored metrics reproduce (are old and new runs like-for-like?).
  verifyProfileDerivation: (fingerprint: string) =>
    request<DerivationAudit>(
      `/settings/profiles/${encodeURIComponent(fingerprint)}/verify-derivation`,
    ),
  regradeHistory: () => startingJob(request<JobStart>("/score/regrade", { method: "POST" })),
  // Fork the current methodology, re-anchor one metric's `best`. Re-grades onto it when
  // `regrade` is true; when false, publishes only (batch several re-anchors, then re-grade once).
  reanchorMetric: (metricKey: string, best: number, regrade = true) =>
    request<{ version: string; job_id: string | null; regrade_deferred?: boolean }>(
      "/methodologies/reanchor",
      {
        method: "POST",
        body: JSON.stringify({ metric_key: metricKey, best, regrade }),
      },
    ),
  // Pick which published methodology scores runs "at present" (the config pin). Pass null to clear
  // the pin and follow the shipped latest. Re-grades history under the choice (background job).
  setCurrentMethodology: (version: string | null) =>
    startingJob(
      request<{ version: string; job_id: string | null }>("/methodologies/set-current", {
        method: "POST",
        body: JSON.stringify({ version }),
      }),
    ),

  // Background jobs feed (powers the top-right "running jobs" dropdown)
  jobs: () => request<JobsResponse>("/jobs"),

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
  // Recompute every run's settings fingerprint from its own captured settings — collapses all
  // "SQM off" runs into one profile (keeps the data, just re-keys the grouping).
  settingsRefingerprint: () =>
    request<{ scanned: number; rekeyed: number }>("/settings/refingerprint", { method: "POST" }),
  settingsDiagnostics: () => request<SettingsDiagnostics>("/settings/diagnostics"),
  // Write a stored profile to the firewall. preview=true returns the planned
  // field changes without writing, so the UI can confirm an exact diff first.
  // runBenchmark (default true) kicks a single-iteration benchmark on the applied profile.
  applyProfile: (fingerprint: string, preview = false, runBenchmark = true) =>
    request<ApplyProfileResult>("/settings/apply-profile", {
      method: "POST",
      body: JSON.stringify({ fingerprint, preview, run_benchmark: runBenchmark }),
    }),
  // Top a "limited data" profile up to the confidence minimum: applies it, runs the
  // iterations still needed, then restores the prior settings.
  testProfile: (fingerprint: string) =>
    startingJob(
      request<ProfileTestStart>("/settings/test-profile", {
        method: "POST",
        body: JSON.stringify({ fingerprint }),
      }),
    ),
  profileTestCurrent: () =>
    request<{ test: ProfileTest | null }>("/settings/test-profile/current"),

  // Challenger race: adaptively test promising limited-data profiles one iteration at
  // a time within a time budget, eliminating any that can't overtake the best.
  startRace: (timeBudgetMinutes: number, autoPromote: boolean) =>
    startingJob(
      request<RaceStart>("/settings/race", {
        method: "POST",
        body: JSON.stringify({ time_budget_minutes: timeBudgetMinutes, auto_promote: autoPromote }),
      }),
    ),
  raceCurrent: () => request<{ race: ChallengerRace | null }>("/settings/race"),
  cancelRace: () => request<{ cancelled: boolean }>("/settings/race/cancel", { method: "POST" }),

  // Re-run stored profiles: apply each, run a chosen number of iterations, restore the
  // baseline at the end. `refreshPreview` estimates time before committing. `top` (optional)
  // re-runs only the top-N profiles, winner-first by their Overall under the prior methodology.
  refreshPreview: (iterations: number, top?: number) =>
    request<ProfileRefreshPreview>(
      `/settings/refresh/preview?iterations=${iterations}` + (top ? `&top=${top}` : ""),
    ),
  startRefresh: (iterations: number, top?: number) =>
    startingJob(
      request<{ id: number; iterations: number; top: number | null }>("/settings/refresh", {
        method: "POST",
        body: JSON.stringify(top ? { iterations, top } : { iterations }),
      }),
    ),
  refreshCurrent: () => request<{ refresh: ProfileRefresh | null }>("/settings/refresh"),
  cancelRefresh: () => request<{ cancelled: boolean }>("/settings/refresh/cancel", { method: "POST" }),

  // Build identity + best-effort "newer build available to pull" check.
  version: () => request<VersionInfo>("/version"),
  // Force a fresh upstream check now, bypassing the 1-hour cache (the "Check now" button).
  refreshVersion: () => request<VersionInfo>("/version/refresh", { method: "POST" }),

  // One-click self-update: ask Watchtower to pull the newer image and recreate this container.
  triggerUpdate: () => request<UpdateTriggerResult>("/update/trigger", { method: "POST" }),
  // Watchtower integration: config state (no network) + a reachability test (no update triggered).
  selfUpdateConfig: () => request<UpdateConfig>("/update/config"),
  testUpdateConnection: () =>
    request<UpdateConnectionTest>("/update/test", { method: "POST" }),

  // Config
  config: () => request<BenchmarkConfig>("/config"),
  updateConfig: (partial: Record<string, unknown>) =>
    request<BenchmarkConfig>("/config", {
      method: "PUT",
      body: JSON.stringify(partial),
    }),
  resetConfig: () => request<BenchmarkConfig>("/config/reset", { method: "POST" }),
  adoptRubric: () => request<BenchmarkConfig>("/config/adopt-rubric", { method: "POST" }),
  rescoreHistory: () => startingJob(request<JobStart>("/score/rescore", { method: "POST" })),
  rederiveHistory: () => startingJob(request<JobStart>("/score/rederive", { method: "POST" })),
  providerHealth: () => request<ProviderHealth>("/config/provider"),
  discover: () => request<DiscoverResponse>("/config/discover", { method: "POST" }),
  testApply: () => request<TestApplyResult>("/config/test-apply", { method: "POST" }),

  // Shotgun Sweep
  sweepPipes: () => request<{ pipes: SweepPipe[] }>("/sweep/pipes"),
  sweepFields: () => request<{ fields: SweepField[] }>("/sweep/fields"),
  sweepPreview: (body: { spec: SweepSpec; iterations: number; dwell_minutes: number }) =>
    request<SweepPreview>("/sweep/preview", { method: "POST", body: JSON.stringify(body) }),
  startSweep: (body: {
    spec: SweepSpec;
    iterations: number;
    dwell_minutes: number;
    dry_run: boolean;
    pipe_uuid?: string | null;
  }) =>
    startingJob(
      request<Sweep>(`/sweep?tz_offset=${tzOffsetMinutes()}`, {
        method: "POST",
        body: JSON.stringify(body),
      }),
    ),
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

  // Consolidated raw export of the last N runs.
  dataDump: (limit: number) => request<DataDump>(`/history/dump?limit=${limit}`),
  optimizerExport: (runsPerProfile: number, profileLimit?: number) =>
    request<OptimizerExport>(
      `/settings/export/optimizer?runs_per_profile=${runsPerProfile}` +
        (profileLimit ? `&profile_limit=${profileLimit}` : ""),
      undefined,
      { timeoutMs: 120_000 },
    ),

  // AI (OpenRouter)
  aiConfig: () => request<AiConfig>("/ai/config"),
  aiSaveConfig: (body: { api_key?: string; model?: string; prompt?: string }) =>
    request<AiConfig>("/ai/config", { method: "PUT", body: JSON.stringify(body) }),
  aiClearKey: () => request<AiConfig>("/ai/config/key", { method: "DELETE" }),
  aiModels: () => request<{ models: AiModel[] }>("/ai/models"),
  aiSuggest: (body: {
    model?: string;
    prompt?: string;
    runs_per_profile?: number;
    profile_limit?: number | null;
  }) =>
    request<AiSuggestResult>(
      "/ai/suggest",
      { method: "POST", body: JSON.stringify(body) },
      // The server blocks on OpenRouter (its own 180s cap); allow headroom past that so a
      // slow model returns rather than the browser aborting with an opaque error.
      { timeoutMs: 240_000 },
    ),
  // Streaming variant: the model's reasoning + answer arrive token-by-token as Server-Sent
  // Events, so a long request never times out and the UI shows progress live. `onEvent` fires
  // for each event; the promise resolves when the stream closes. Pass a signal to cancel.
  aiSuggestStream: async (
    body: { model?: string; prompt?: string; runs_per_profile?: number; profile_limit?: number | null },
    onEvent: (evt: AiStreamEvent) => void,
    signal?: AbortSignal,
  ): Promise<void> => {
    let res: Response;
    try {
      res = await fetch(`${BASE}/ai/suggest/stream`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal,
      });
    } catch (e) {
      if (signal?.aborted) return;
      throw new ApiError(0, "Couldn't reach the server to start the stream.");
    }
    if (!res.ok || !res.body) {
      let detail = res.statusText;
      try {
        const b = await res.json();
        if (b && typeof b.detail === "string") detail = b.detail;
      } catch {
        /* ignore */
      }
      throw new ApiError(res.status, detail);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      // SSE events are separated by a blank line.
      let sep: number;
      while ((sep = buf.indexOf("\n\n")) !== -1) {
        const block = buf.slice(0, sep);
        buf = buf.slice(sep + 2);
        for (const line of block.split("\n")) {
          const t = line.replace(/^\s+/, "");
          if (!t.startsWith("data:")) continue;
          const data = t.slice(5).trim();
          if (!data) continue;
          try {
            onEvent(JSON.parse(data) as AiStreamEvent);
          } catch {
            /* skip a partial/garbled line */
          }
        }
      }
    }
  },
  // Apply arbitrary settings (e.g. an AI suggestion) to the firewall PERMANENTLY. preview=true
  // returns the exact planned writes (for the confirm dialog); commit writes + optional benchmark.
  applySettings: (body: {
    settings: unknown;
    label?: string;
    preview?: boolean;
    run_benchmark?: boolean;
  }) =>
    startingJob(
      request<ApplyProfileResult>("/settings/apply-settings", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    ),
  // Apply arbitrary settings (e.g. an AI suggestion) onto the live profile and test to minimum.
  testSettings: (body: { settings: unknown; label?: string }) =>
    startingJob(
      request<{ id: number; fingerprint: string; iterations: number; label: string | null }>(
        "/settings/test-settings",
        { method: "POST", body: JSON.stringify(body) },
      ),
    ),

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
