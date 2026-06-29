# CLAUDE.md — PathBrain developer guide

PathBrain is an empirical network-optimization platform that maximizes
*human-perceived responsiveness* — scored as the **Responsiveness / Smoothness /
Speed** axes (+ an **Overall** corner roll-up), not raw ping/throughput. (The
original single *Seat of Pants Score* was split into these axes; SOPS is now
legacy.) The optimizer is classical (deterministic sweep + hysteresis), not
LLM-based. See `README.md` for the product overview.

## Layout

- `backend/pathbrain/` — FastAPI app (the core). Key modules:
  - `plugins/` — benchmark plugins (`icmp/dns/tcp/tls/http/browser`) that are
    **pure sensors: they emit raw observations only** (`PluginResult.raw`) and never
    interpret. `base.py` defines the contract + registry. Add one by subclassing
    `BenchmarkPlugin` and `@register`. (icmp emits per-ping RTT series, http emits
    bytes+timing, browser emits raw nav/paint/CLS/long-task + Resource Timing/LoAF
    entries, and an optional filmstrip.)
  - `interpret/` — **the interpretation layer** (`derive.py`, versioned
    `DERIVATION_VERSION`). Turns raw observations → scoreable metric values:
    `jitter`=stddev(RTTs), `latency`=mean, `transfer`=bytes·8/dl, the **byte-arrival
    smoothness** metrics (`smoothness.py`: longest stall / cadence CoV / byte
    earliness / delivery Gini / perceived time / network-vs-render stall attribution,
    all from Resource Timing + LoAF — no pixels), and the pixel diagnostics (Speed
    Index / paint cadence / CLS from the optional filmstrip); `fcp`/`lcp` are identity
    pass-throughs. This is the **only** place interpretation lives, so a new metric or
    changed formula can be re-derived over history without re-collecting.
  - `providers/` — firewall config discovery + **apply** (`opnsense.py`,
    `mock.py`); pick via `PATHBRAIN_CONFIG_PROVIDER`. OPNsense reads/writes
    fq_codel fields (`fqcodel_quantum/limit/flows`, `codel_target/interval/ecn`);
    `apply()` does `setPipe` + `reconfigure` and is the **only firewall-write path**.
    `discover()` (read) + `apply()` (write) are the one read/write path; a provider's
    `writable_fields()` is the single accessor for *what it can change*.
  - `shaper_fields.py` — **single source of truth for the SQM field model.** Each
    `ShaperField` (key, label, kind, `identity`/`writable`/`sweepable`) is declared once;
    `CANON_FIELDS` (profile identity / fingerprint), `FIELD_LABELS`, `WRITABLE_FIELDS`,
    `NON_WRITABLE_FIELDS`, and `SWEEPABLE_FIELDS` all derive from it, so `settings_profile`,
    the providers, and the sweep/experiment engines share one definition instead of
    re-listing field names. Invariants (writable ⊆ identity; sweepable ⊆ writable; the read
    model `FqCodelConfig` and OPNsense `_PARAM_FIELD` cover the registry) are asserted at
    import **and** in `test_shaper_fields` — the relationships that used to drift in comments
    and produced the "valid but unappliable profile" challenger bug. Adding a shaper field =
    one entry here.
  - `metrics.py` — **single source of truth for metrics.** Each `MetricDef` (key,
    plugin+source_key, axis, default weight/thresholds, label/description/unit/
    direction, `marks_latest`) is defined once; `METRIC_SOURCES`, the config
    weights/thresholds, `LATEST_METRIC_KEYS`, and the `/api/metrics` catalog (which
    the frontend's `MetricCatalogProvider`/`useMetricMeta` consume) are all derived
    from it. Adding a measurement = one entry here (+ the plugin emitting it).
  - `scoring/engine.py` — the generic score **primitive**: `compute_score` takes a
    metric set + weights + thresholds and returns a 0–100 weighted average on a
    perception-calibrated log curve, redistributing missing-metric weight. Axis-
    agnostic; *which* metrics form *which* axis lives in `methodology.py`.
  - `methodology.py` — **the published, versioned rubric** (derivation + axis
    weights/thresholds + the first-class Overall), append-only. `CURRENT_METHODOLOGY` =
    `speed-smoothness-v6`, which scores **three headline axes** (the temporal phases of a
    load; each metric maps to exactly one axis):
    - **Responsiveness** (time-to-first): byte-earliness (30) + FCP (25) + TTFB (15).
    - **Smoothness** (steady fill): longest-stall (40, required) + total-stall (30)
      + cadence (15) + evenness (15).
    - **Speed** (time-to-last + interactive): LCP (40) + INP (40) + render (20) +
      load-event (20).
    Plus secondary **Stability** (CLS) and **Completion** (DNS/TCP/TLS/jitter/loss),
    kept out of the headline since they barely move human feel. The **Overall** is a
    first-class, versioned roll-up defined here (`overall_from_definition` /
    `corner_score`) and persisted to `Score.axis_scores["overall"]` at scoring time — the
    corner over **FCP × total_stall × load_event** (quickest first response × cumulative
    dead-air × page-load time), two built-in standards plus the one bespoke stall signal.
    It's an intersection, so a stall pulls the Overall down via the corner geometry, not a
    hidden weight. v5 introduced the first-class Overall (then fcp/perceived_time/inp) and
    re-anchored the time-to-content `best` thresholds (TTFB 30, FCP 150, byte-earliness
    150, LCP 150ms); **v6** decomposed the crown — `perceived_time` (which baked an
    uncalibrated 4× stall penalty into a duration) is dropped from scoring and kept as a
    display-only diagnostic, replaced by the independent `total_stall` (cumulative time
    behind the load's own median pace; `interpret/smoothness.total_stall`) + the built-in
    `load_event`. `runner.score_metrics_under` scores every axis generically via
    `axis_rubric` + `compute_score`, persisting per-axis results + Overall to
    `Score.axis_scores` (JSON). Predecessors (`speed-smoothness-v1..v5`, earlier rubrics)
    are frozen for old at-measure scores. The crown metric set is read from the current
    methodology's `overall` spec (`overall_metrics`) as the single source of truth, so the
    persisted Overall, the live fallback, and the challenger race never drift.
  - `config_store.py` — DB-backed runtime config + defaults (targets, weights,
    thresholds, `iterations`, `monitoring`, `correlation`, `trends`, `experiment`,
    `rubric_version`).
  - `runner.py` — orchestrates a run across plugins (derives metrics from raw via
    `interpret`, median-aggregated over iterations, per-run SOPS confidence band),
    stores `BenchmarkResult.raw` as the source of truth, captures the firewall
    settings + fingerprint per run, and holds run-lifecycle safety
    (`reconcile_interrupted_runs`, `fail_stale_runs`, `rescore_run` = re-grade cached
    scalars under a new rubric, `rederive_run` = re-run derivation+scoring from raw).
  - `trends.py` — historical baselines by day-of-week × hour-of-day (viewer-local);
    `relative_reading`/`profile_relative` give a time-adjusted "vs typical" delta
    ("wins above replacement"). Powers `/api/trends/*`, the Dashboard delta chip,
    and the Settings-Impact "vs typical" column.
  - `sweep.py` — **Shotgun Sweep**: an on-demand foreground sweep of a grid over the
    registry's `SWEEPABLE_FIELDS` (quantum × target today). Applies each variant for real,
    benchmarks it, **restores the baseline at the end** (`reconcile_interrupted_sweeps`
    restores on startup too). Variant generation, value formatting (`shaper_fields.format_value`
    — int vs `"<n>ms"`), apply, label, and restore all iterate `SWEEPABLE_FIELDS`, so marking
    another field sweepable in the registry extends the engine with no new branch. The Shotgun
    Sweep **UI** is driven the same way: `GET /api/sweep/fields` returns each sweepable field's
    label/unit/default range (from `ShaperField.sweep_default`) and the page renders a control
    + a results column per field — so a new sweepable field needs no frontend edit. Runs in its
    own thread; the scheduler yields while `sweep.active()`.
  - `scheduler.py` — daemon thread: watchdog → (yield while the coordination lock is
    held) → experiment step → monitoring run (serialized so benchmark runs never overlap).
  - `experiment.py` — autonomous window-gated single-parameter shaper sweep
    (writes via `provider.apply()`; disarmed + dry-run by default; restores baseline). The
    swept `param` is validated against `shaper_fields.WRITABLE_FIELDS` at start — an
    experiment on a non-writable field (scheduler/queues) is refused instead of no-op'ing.
  - `coordinator.py` — process-wide lock that serializes any apply-firewall + benchmark
    session (sweep, profile test, experiment, monitoring, manual run): user-triggered
    ones `hold` (queue), periodic ones `try_hold` (defer). Pairs with the read-before/
    read-after fingerprint check in `runner.execute_run` (FAILs a run on mid-run drift).
  - `jobs.py` — in-process background-job registry (progress/status/recent history).
    The heavy score passes (`/api/score/regrade|rescore|rederive`) run as jobs and
    return `202 {job_id}`; `/api/jobs` (`api/routes_jobs.py`) merges them with read-only
    adapters for active runs/sweep/profile-test/experiment so the top-right jobs
    dropdown shows everything. History is in-memory (durable ops live in their DB rows).
  - `profile_test.py` — **Test to minimum**: apply a stored profile, run exactly the
    iterations still needed to reach `correlation.min_iterations`, then **restore the
    baseline** (persisted to a `ProfileTest` row; `reconcile_interrupted_profile_tests`
    restores on startup). `/api/settings/test-profile`.
  - `challenger.py` — **Challenger Race**: the adaptive, multi-profile sibling of
    `profile_test`. A time-boxed loop that runs **one iteration at a time** on whatever the
    field can't currently trust against the winner, re-ranks via `rank_challengers`, and
    **eliminates** any under-minimum profile whose *optimistic* Overall (corner over each
    crown metric's p75 upper estimate; `routes_settings.optimistic_overall`) can no longer
    beat the confident best. Contenders span, in priority order — **defend the crown by
    confronting the biggest known threat first, not by gambling on the unknowns**:
    **(1) under-minimum** profiles that can still beat the bar, **highest optimistic ceiling
    first** (the profile most likely to dethrone the crown is confirmed/refuted first);
    **(2) stale confident** profiles older than `challenger.contender_stale_minutes`
    (default 180), re-measured **ordered by closeness to the winner** (in case anything has
    changed); **(3) no-data** profiles — zero comparable runs under the current methodology
    (`_field` augments the `compute_profiles` field with these from `refresh.list_profiles`;
    the "run anything without data on the latest methodology" case, never eliminated until
    measured) — sampled **last**, once the known threats and nearby incumbents have had the
    window's time. It **bootstraps** with no confident best (bar
    None → race everything lacking data until a winner emerges). It also **refreshes a stale
    incumbent** (`challenger.incumbent_refresh_minutes`, default 60) first so the bar stays
    contemporaneous (`_incumbent_stale`; counted in `incumbent_refreshes`). It only races
    profiles **reachable** from the live environment: `apply()` can write the codel/bandwidth
    params but not `scheduler`/`queues`/`upload_bandwidth` (`settings_profile.NON_WRITABLE_FIELDS`),
    so a profile differing in those is unreproducible — `rank_challengers(reachable_env=…)`
    eliminates it ("unreachable: …") instead of letting `_apply_profile` abort the whole race
    on a fingerprint it can't reach (`_apply_profile` now verifies the *writable* params took,
    not the full fingerprint; `environment_signature` hashes the non-writable fields). At the
    end it **restores the baseline**, or applies the winner when `auto_promote`. Own thread under
    the `coordinator` lock (so the scheduler defers via `coordinator.busy()`); persisted to
    a `ChallengerRace` row; `reconcile_interrupted_challenges` restores on startup.
    `/api/settings/race` (+ `/race/cancel`).
  - `refresh.py` — **Re-run all profiles**: the batch sibling of `profile_test`. For
    each stored profile it applies the settings, benchmarks a **caller-chosen** number of
    iterations, then moves on — **restoring the baseline at the end** (persisted to a
    `ProfileRefresh` row; `reconcile_interrupted_refreshes` restores on startup). One bad
    profile is logged and skipped, not fatal. `refresh.preview` estimates duration
    (median per-iteration time × total iterations + per-profile overhead) so the UI can
    show "N profiles × M ≈ ~T" before committing. Own thread under the `coordinator` lock.
    Use it to collect fresh, comparable data after a methodology change quarantines
    history that can't supply a new crown metric. `/api/settings/refresh`
    (+ `/refresh/preview`, `/refresh/cancel`).
  - `settings_profile.py` — normalize/fingerprint/summarize firewall profiles for
    settings-vs-responsiveness correlation (`/api/settings/*`). Profile confidence is
    gated on **total iterations** (`correlation.min_iterations`, default 15).
    `/api/settings/profiles` ranks profiles by the **Overall**, which since methodology
    `speed-smoothness-v5` is a **first-class, versioned quantity** defined in the
    methodology (`overall_from_definition`) and **persisted** on each `Score`
    (`axis_scores["overall"]`) at scoring time — so grading and crowning never drift.
    Overall = closeness to the (100, 100, 100) corner (`methodology.corner_score`) over the
    crown metric set — the few measurements that directly capture human feel, as
    perception-calibrated 0–100 subscores. The set is read from the methodology's `overall`
    spec (`overall_metrics`; module `CROWN_METRICS`/`CROWN_REQUIRED` are only the pre-v5
    fallback): under v6 that's **FCP × total_stall × load_event** (quickest first response ×
    cumulative dead-air × page-load time — two built-in standards + one bespoke stall
    signal; v5 used fcp/perceived_time/inp). It's an *intersection* (corner, not mean — one
    weak metric can't be averaged away), √k-normalized so corners of different arity share a
    scale.
    `compute_profiles` reads the persisted Overall (falling back to a live `_crown_corner`
    for not-yet-re-graded Scores). A **custom crown** (`crown_metrics=` query param,
    `_apply_custom_crown`) corners over any caller-chosen subset of subscores as an
    exploratory `custom_overall` + `custom_best_fingerprint` — a what-if lens over the same
    persisted building blocks, leaving the canonical Overall untouched. The per-axis scores
    (Responsiveness/Smoothness/Speed; `_CORNER_AXES`) remain as display columns. It also
    aggregates per profile the median of every axis score *and* every metric we collect
    (`metrics.all_metric_sources`) to power the dynamic quadrant + table column selector.
    The crowned **"best"** is the confident profile with the highest **probability of
    being the true best** (`probability_of_best`): a Bayesian/Thompson Monte-Carlo over
    each candidate's Normal posterior on its true Overall (location = median, scale =
    `overall_posterior_scale` SE, tightening with √n), so it weighs *both* a high typical
    Overall and how sure we are — rather than a pessimistic floor that double-penalized
    variance (smoothness already scores consistency). The posterior location is shifted
    *down* by any negative **vs-typical** shortfall (`relative_lower_bound`) so a
    window-rider competes from its de-confounded level. Returns `best_fingerprint` + a
    per-profile `prob_best`.
  - `database.py` — engine/session + additive SQLite `_migrate()` (ALTER for new
    columns; `create_all` for new tables).
  - `api/` — REST routers mounted at `/api`.
- `frontend/` — React + TS + Vite + MUI dashboard (dark mode). Pages: Dashboard,
  History, Trends, Compare, Settings Impact (**paginated** sortable table — 25/page —
  with standard **Overall / Responsiveness / Smoothness / Speed** columns + an optional
  column selector; a **dynamic** any-metric quadrant where X/Y pick the axes, a **Shade**
  picker encodes a third field as dot **opacity** (brighter = better; `ProfileQuadrant`),
  and the crowned profile is ringed — the quadrant now warns when an axis is **saturated**
  (every profile already past the methodology's `best` threshold, so the raw spread carries
  no score signal, e.g. fcp/load_event on a fast link), using the effective thresholds in
  the profiles response's `metric_thresholds`; a page-level **methodology saturation check**
  (`saturation` in the response, `_saturation_report`) flags any scored, non-zero-`best`
  metric that saturates >50% of profiles — too lenient to crown the fastest — and suggests
  re-anchoring `best` to the fastest value measured (`best`=0 metrics like total_stall are a
  physical floor and never flagged); plus a **"Heirs to the crown"** card — the
  limited-data / stale profiles whose *optimistic ceiling* (`optimistic_overall`, the same
  number the race uses) could still beat the crown, ordered to **mirror the race's sampling
  priority** (biggest known threat first → nearby stale incumbents → untested last), so the
  top heir is the first profile a race would actually run, with a
  count badge on **"Race challengers"** ("N could beat your crown"; response field `heirs`).
  Heirs are filtered to profiles **reachable** from the live environment (same
  `environment_signature` check as the race), so the card never lists a profile the race
  would refuse to apply;
  plus "Test to minimum" and **"Race challengers"**),
  Experiments, Shotgun Sweep, Config, Methodology, Plugins, Data Dump, Run Detail. A
  top-right **jobs dropdown** (`JobStatus`) shows every running/recent background job
  (re-grade, sweep, run, profile test, challenger race, …).
- `Dockerfile` (Playwright base image) / `docker-compose.yml` +
  `docker-compose.ghcr.yml` — single-container deploy (API serves UI). CI publishes
  `ghcr.io/jmorganthall/pathbrain:latest` via `.github/workflows/docker-publish.yml`,
  stamping the build commit (`--build-arg GIT_SHA=$github.sha` → `PATHBRAIN_GIT_SHA`).
- **Version awareness** (`updates.py`, `GET /api/version`): a cached, best-effort
  compare of this build's `git_sha` against the latest commit on `update_repo`'s
  default branch (GitHub API; on by default, `PATHBRAIN_UPDATE_CHECK=false` to disable).
  The top-bar `UpdateChip` shows "Update available" (→ the GitHub compare) when the
  branch has moved past the running build — i.e. a newer `:latest` image is pullable.

## Commands

```bash
# Backend tests (from backend/)
cd backend && pip install -r requirements-dev.txt && python -m pytest

# Run backend (dev)
cd backend && uvicorn pathbrain.main:app --reload --port 8000

# Frontend (dev, proxies /api -> :8000)
cd frontend && npm install && npm run dev

# Frontend build (must pass before commit)
cd frontend && npm run build

# Full stack via Docker
docker compose up --build   # -> http://localhost:8000
```

## Conventions

- Plugins must never raise for *measurement* failures — return a `PluginResult`
  with `success=False` and an `error`. Use the `timed()` helper. Plugins emit
  **raw observations only** (`raw=…`); the `interpret` layer derives metrics — keep
  statistics/aggregation out of the probe.
- All runtime config (targets/weights/thresholds) is DB-backed and editable via
  `/api/config`; infra config (DB URL, OPNsense creds) is env-only (`config.py`).
- Lower-is-better for all current axis metrics; thresholds define best/worst and
  are interpolated on a perception-calibrated log curve (Weber–Fechner). The
  rubric (axes+weights+thresholds) is bundled into a versioned **methodology**.
  **Re-grade paths:** `POST /api/score/regrade` re-scores every run from raw under
  the current methodology, writing new `Score` rows (use this after publishing a new
  methodology — e.g. the v4 axis split); `POST /api/score/rescore` / `rederive` are
  the legacy in-place paths over cached scalars / raw.
  **GUI re-anchor (`POST /api/methodologies/reanchor`):** forks the *current* methodology's
  frozen definition, overrides one scored metric's `best`, writes it as a **new** version
  (axes + Overall spec carried over unchanged — append-only, not an edit), points
  `config.methodology_version` at it, and kicks the re-grade — the one-click "apply" behind
  the Settings-Impact saturation alert (Settings → `?reanchor=<metric>&best=<n>` → Methodology
  page proposal panel). Lets a threshold be re-anchored from the UI without a code edit, while
  every published version stays a frozen DB snapshot.
- **Publishing a new methodology — required follow-through.** Bumping
  `CURRENT_METHODOLOGY` is not done until both of these happen, or history shows stale
  scores and the default UI stops reflecting the rubric:
  1. **Re-grade history.** New/changed metrics derive from the **already-captured raw**
     (Resource Timing etc.), so a re-grade re-scores every run with the new
     metrics — **no re-collection / re-run needed**. Trigger it via the **Methodology
     page → "Re-grade history under current"** button (or `POST /api/score/regrade`).
     Only pre-raw-collection legacy runs (no raw) can't be re-derived — they stay
     quarantined as legacy. There is deliberately no "physically re-run every profile"
     batch; re-grading from raw is the supported way to bring history onto a new rubric.
  2. **Update the quadrant defaults.** The Settings-Impact quadrant should open on the
     metrics that drive the current Overall. Set the default axis keys in
     `frontend/src/pages/Settings.tsx` (`xKey`/`yKey`/`sizeKey`) to the new crown set —
     the methodology's `overall` spec metrics (`methodology.overall_metrics`), one per
     X / Y / Shade slot — so the default view demonstrates how Overall is scored. (v6:
     `fcp` × `load_event` × `total_stall`.)
- A run repeats the suite `iterations` times; each headline axis is the **median**
  over iterations, with a confidence band. The Dashboard shows a windowed
  **rolling** score (`/api/score/rolling`, 24h median + IQR) plus a **"vs typical"**
  delta vs the day/hour historical baseline (`trends.py`).
- **Per-plugin iteration caps (perf).** A plugin's config section may set `iterations`
  to run it fewer than the suite's `iterations` — the heavy **browser** defaults to
  `browser.iterations` (2) while the cheap network probes run the full count. The headline
  metric medians use every captured sample (`_median_values` skip-missing, so a capped
  plugin stays unbiased); only the legacy SOPS confidence band is restricted to full-suite
  rounds. Plugins get a `teardown()` lifecycle hook the runner calls after the loop, so the
  browser **reuses one Chromium across a run's iterations** (cold-start once, not per
  iteration) and closes it there. The browser's **screenshot/HAR are off by default**
  (artifacts-only, no scored metric), its `networkidle` settle has its own short cap
  (`networkidle_timeout_s`, 5s) instead of the 30s nav timeout, and the default
  ICMP/DNS/TCP/TLS/HTTP target lists are trimmed — all to cut wall-clock without changing
  what's scored.
- **Comparability is tied to crownability.** `methodology.comparability()` flags a run
  `incomparable` when its raw can't supply a `required` metric **or any of the current
  methodology's crown metrics** (`overall.required`, via `overall_metrics`) — so a run
  that can't produce the headline Overall (e.g. a pre-v6 run with no `total_stall`) is
  quarantined, never silently scored without the metrics that define the score. A re-grade
  reports the `exact`/`partial`/`incomparable` split (surfaced in the job summary). This
  auto-adapts to every future methodology, so adding a crown metric can't silently leave
  stale-but-valid-looking scores. (`marks_latest`/`has_latest_metrics` is the separate,
  static at-measure legacy marker — still `longest_stall`.)
- **Current vs. legacy scoring (no dual-score machinery).** A run scored before
  the current rubric (no longest-stall / byte-arrival metrics —
  `metrics.has_latest_metrics`, keyed off `marks_latest`, now `longest_stall`) isn't
  comparable, so it's **quarantined**, not
  reconciled: Dashboard rolling + History trend exclude legacy; the History list
  hides it behind a "Show legacy" toggle; Run Detail/Compare flag it
  (`ScoreOut.legacy`/`RunSummary.legacy`); Settings Impact aggregates `complete_only`
  (default true). Legacy runs are kept for their *settings* history, not their score.
  Responsiveness/Smoothness/Speed (+ the Overall roll-up) are the ranked headlines;
  Stability and Completion are opt-in diagnostics.
- Each run captures the live firewall settings + a stable **fingerprint** at start
  (best-effort). Runs group into **profiles**; `/api/settings/impact` flags a
  change significant only with ≥ `correlation.min_runs` per side. `/api/settings/
  backfill` stamps current settings onto unstamped historical runs.
- **Run lifecycle safety:** `reconcile_interrupted_runs()` (startup) + scheduler
  watchdog `fail_stale_runs()` (`monitoring.run_timeout_minutes`, default 30) +
  manual `POST /api/runs/{id}/cancel` resolve orphaned/hung runs. These mark the
  DB row FAILED; a live benchmark thread can't be force-killed mid-call.
- Timestamps are stored UTC (naive in SQLite); the frontend (`parseApiDate`)
  treats them as UTC so they render in the viewer's local zone. Experiment-window
  hours use the container `TZ`.
- Every action should be logged (`logging_config.get_logger`).

## Phase map

- **Phase 1 (done):** benchmark engine (ICMP/DNS/TCP/TLS/HTTP), SOPS scoring,
  history, config discovery (OPNsense/mock), REST API, dashboard.
- **Phase 2 (done):** Playwright browser engine — `benchmark_browser` emits raw
  nav timings, **paint events** (`fcp`/`lcp`/`inp`), **Resource Timing + LoAF** (for
  smoothness), and an **optional filmstrip** (CDP screencast, gated by
  `browser.filmstrip`, off by default — it only feeds the pixel Speed Index/cadence
  diagnostics); captures screenshot/HAR to the artifact dir, served at `/artifacts`.
- **Phase 3 (done):** continuous monitoring (`scheduler.py`) + rolling score;
  settings-vs-responsiveness correlation (`settings_profile.py`, `/api/settings/*`);
  perception-calibrated rubric (Weber–Fechner) with versioned re-scoring; and the
  **experiment engine** (`experiment.py`): window-gated single-parameter sweep
  that writes to the firewall via `provider.apply()`, disarmed + dry-run by
  default, restoring the pre-window baseline at window close.
- **Phase 4 (done):** **historical trends + relative SOPS** (`trends.py`,
  `/api/trends/*`) and time-adjusted Settings-Impact ("vs typical"); **raw-only
  collection + a re-runnable interpretation layer** (`interpret/derive.py`,
  `BenchmarkResult.raw`, `/api/score/rederive`); **trajectory-aware scoring**
  (Speed Index / paint cadence / CLS from the filmstrip; rubric `perceptual-v3`,
  Pillow dep); a reversible **config write-test** (`POST /api/config/test-apply`);
  and the **Shotgun Sweep** (`sweep.py`, `/api/sweep/*`) — an on-demand grid sweep
  that applies each variant, benchmarks it, ranks by SOPS + "vs typical", and
  restores the baseline.
- **Phase 5 (done):** **perceived load-smoothness instrument** — byte-arrival
  smoothness metrics from Resource Timing + LoAF (`interpret/smoothness.py`), with
  network-vs-render stall attribution and protocol mix. Promoted into SOPS (rubric
  `perceptual-v4`): byte earliness / longest stall / perceived time replace the
  pixel Speed Index / paint cadence (now opt-in diagnostics). Per-run records +
  two-config comparison at `/api/smoothness/*` (keyed on `settings_fingerprint`);
  an offline **calibration harness** (`calibration/`) fits the perceived-time
  weight ratio to subjective 1–10 ratings.
- **Phase 6 (done):** **three-axis headline** (methodology `speed-smoothness-v4`):
  split the blended Speed into **Responsiveness** (time-to-first) + a redefined
  **Speed** (time-to-last + interactive), with a derived **Overall** corner roll-up;
  Settings Impact gained the dynamic any-metric quadrant (opacity-shaded third axis) +
  a paginated, column-selectable table; and the **Challenger Race** (`challenger.py`) —
  an adaptive, time-boxed elimination race that promotes limited-data profiles toward
  confidence one iteration at a time.
- **Next:** speed test / bufferbloat (latency-under-load), multi-parameter Bayesian
  search + interleaved A/B with effect-size/CI + hysteresis, routing intelligence /
  SD-WAN.

⚠️ Firewall **writes** go only through `provider.apply()`. Seven callers use it, all
snapshot/restore or are reversible: the experiment engine (disarmed + dry-run by
default), the Shotgun Sweep (restores baseline at end + on startup), config
test-apply (+1 then revert), sweep apply-best (explicit, supervised), the
profile test (`profile_test.py`: apply → benchmark → restore, baseline persisted +
reconciled on startup), the **challenger race** (`challenger.py`: time-boxed
apply → 1 iteration → re-rank, restoring the baseline at the end — or applying the
winner when `auto_promote` — baseline persisted + reconciled on startup), and the
**profile refresh** (`refresh.py`: for each stored profile apply → benchmark N
iterations → next, restoring the baseline at the end — baseline persisted + reconciled
on startup). Keep new write paths to `provider.apply()` and always snapshot/restore.

⚠️ Any **apply-firewall + benchmark** session must hold the `coordinator.py` lock so
two never overlap (user-triggered ones — sweep, profile test, challenger race, profile
refresh, manual `/api/run` — `hold` and queue; periodic ones — monitoring, experiment —
`try_hold` and defer).
`runner.execute_run` independently re-reads the firewall fingerprint **after** the
run and FAILs it on drift (the read-before/read-after integrity check), so "what we
tested" always matches "what we thought". A profile is **confident** once its runs
total ≥ `correlation.min_iterations` (default 15) — iterations, not run count, are
the unit of signal.

The browser engine imports Playwright lazily, so the plugin registry still loads
where Playwright/Chromium isn't installed (it returns `success=False` and the
browser metrics' weight is redistributed). The byte-arrival smoothness metrics need
only Resource Timing (always present); the opt-in filmstrip/Speed Index degrade
gracefully without CDP screencast or Pillow. Chromium is installed in the Docker image.
