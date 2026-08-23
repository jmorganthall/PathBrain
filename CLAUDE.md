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
    smoothness** metrics (`smoothness.py`: longest stall / **stall energy** (`√Σgap²`, the
    crown's smoothness leg) / stall time / cadence CoV / byte earliness / delivery Gini /
    perceived time / jank fraction / network-vs-render stall attribution, all from Resource
    Timing + LoAF — no pixels; the whole instrument is **bounded to the page load**,
    `resources_within_load`, so a late background fetch can't inflate the stall metrics),
    the **navigation waterfall** (`waterfall.py`: the load's independent phases —
    `nav_dns`/`nav_tcp`/`nav_tls`/`nav_request`/`nav_response`/`nav_render` — from Navigation
    Timing marks, so DNS/TCP/TLS setup can be split out from render), and the pixel diagnostics
    (Speed Index / paint cadence / CLS from the optional filmstrip); `fcp`/`lcp` are identity
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
    from it. Adding a measurement = one entry here (+ the plugin emitting it). It also holds
    the **metric ledger** (`METRIC_ROLES`): every metric is bucketed into exactly one role —
    **W** weather instrument (probe sockets: dns/tcp/tls/latency/jitter/loss/download), **N**
    navigation network phase (the `nav_*` waterfall), **C** client CPU (render/inp/cls,
    shaping-immune), **S** byte-arrival shape statistic (stall_energy/longest_stall/…), **O**
    opaque milestone sum (fcp/lcp/load_event — span multiple buckets). `RANKABLE_ROLES = {N, S}`
    is the coarse gate (`rank_eligible`/`ineligible_scored`) that keeps weather + opaque metrics
    out of *automatic* headline/axis ranking; the crown may still explicitly name an `O` metric
    (v10 corners over FCP/LCP by design). Completeness is asserted at import — adding a metric
    forces a role choice.
  - `scoring/engine.py` — the generic score **primitive**: `compute_score` takes a
    metric set + weights + thresholds and returns a 0–100 weighted average on a
    perception-calibrated log curve, redistributing missing-metric weight. Axis-
    agnostic; *which* metrics form *which* axis lives in `methodology.py`.
  - `methodology.py` — **the published, versioned rubric** (derivation + axis
    weights/thresholds + the first-class Overall), append-only. `CURRENT_METHODOLOGY` =
    `speed-smoothness-v13`, which scores **three headline axes** (the temporal phases of a
    load; each metric maps to exactly one axis):
    - **Responsiveness** (time-to-first): byte-earliness (30) + FCP (25) + TTFB (15).
    - **Smoothness** (steady fill): longest-stall (40, required) + network-stall-all (30)
      + cadence (15) + evenness (15).
    - **Speed** (time-to-last + interactive): LCP (40) + INP (40) + render (20) +
      load-event (20).
    Plus secondary **Stability** (CLS) and **Completion** (DNS/TCP/TLS/jitter/loss),
    kept out of the headline since they barely move human feel. The **Overall** is a
    first-class, versioned roll-up defined here (`overall_from_definition` /
    `corner_score`) and persisted to `Score.axis_scores["overall"]` at scoring time — the
    corner over **FCP × LCP × network_stall_all** (quickest first response × perceptual "main
    content visible" × floor-free network-attributed dead-air): the three things that separate
    profiles — shows initial progress fastest × loads fastest × spends least time stalled on the
    network. FCP and LCP are *native* browser paint timestamps; `network_stall_all`
    (`interpret/smoothness`, `stall_attribution_times(..., min_stall_ms=0)`) is the summed duration
    of every network-attributed inter-resource gap with **no minimum-gap floor** — so it counts the
    sub-perceptible RTT/handoff gaps a page load on fiber is made of (the resource waterfall is
    gated by round trips — DNS/TCP/TLS/request + ACK pacing — not bandwidth), which fq_codel's
    fairness/AQM actually moves. Render-covered time is excluded (via LoAF/long-task overlap), so it
    isolates the shapeable share. It is **deliberately below human perception** — the objective is
    to *crown the best profile* by measured network dead-air, not to gate on human-noticeable
    hitches (**v13**). This replaced `worst_void_fraction` (the FCP→load "pregnant pause" fraction,
    v11/v12), which read **0 for every profile on a fast link** because its 200ms perceptible-stall
    floor discarded exactly these sub-perceptible handoff gaps — an inert crown leg. `load_event`
    stays a scored Speed metric but is no longer a crown metric. v5 introduced the first-class Overall (then fcp/perceived_time/inp) and
    re-anchored the time-to-content `best` thresholds (TTFB 30, FCP 150, byte-earliness
    150, LCP 150ms); **v6** decomposed the crown — `perceived_time` (which baked an
    uncalibrated 4× stall penalty into a duration) is dropped from scoring and kept as a
    display-only diagnostic, replaced by the independent `total_stall` (cumulative time
    behind the load's own median pace; `interpret/smoothness.total_stall`) + the built-in
    `load_event`. **v7** swaps the crown's completion leg `load_event → lcp` — the *technical*
    page-load (all resources fetched) for the *perceptual* "main content visible" milestone —
    so the crown corners over three independent dimensions instead of two correlated paint
    milestones + completion (identical axes/thresholds to v6; only the `overall` spec moved).
    **v8** swaps Smoothness's scored stall metric + the crown's stall leg `total_stall → stall_time`:
    the *relative* `total_stall` (excess of each completion gap over the run's **own median** pace —
    an average baked into the metric, so a profile's stall standing is a comparison of
    deviations-from-own-baseline) is replaced by the *absolute* `stall_time` (summed duration of
    every gap over a fixed 200ms perceptible-stall threshold; `interpret/smoothness.stall_time`).
    Like FCP/LCP measure an actual timestamp, `stall_time` is an **actual per-run measurement**
    against a fixed yardstick — the same for every run — so Settings-Impact compares profiles on
    real measured dead-air instead of averages-of-averages. `total_stall` stays a display-only
    diagnostic. derive-v5 adds `stall_time_ms` (purely additive), so history re-grades straight
    from raw — every run with resource-timing raw gains an actual `stall_time`.
    **v9** (short-lived) reworked the crown to rank only *rank-eligible* ledger roles (see the
    `metrics.py` W/N/C/S/O ledger below), swapping the crown legs to `nav_response`/
    `byte_earliness`/`jank_fraction` — chosen for shaper-movability. **v10** reverts that: it
    returns the crown to **FCP × LCP × stall_energy** (the first-principles felt outcome —
    what the human experiences, not what the shaper can move) and takes `stall_energy` as the
    Smoothness scored-stall metric. The crown deliberately corners over FCP/LCP even though
    they're ledger role `O` (opaque milestones): the coarse rank-eligibility gate keeps
    weather/opaque metrics out of *automatic* headline inclusion, but the crown explicitly names
    its metrics — the finer positive selection is the crown's job. **v11** refines the crown's
    smoothness leg `stall_energy → worst_void_fraction`: `stall_energy` was absolute ms (√Σgap²
    over the whole in-load fill), which spanned past LCP (punishing a post-content tail the user
    never felt) and, being absolute, *correlated* with LCP — double-counting a slow load's freeze
    on both the LCP and smoothness legs. `worst_void_fraction` is the *scale-free* longest void
    **within the FCP→LCP window** as a fraction of it, so it measures only the evenness of the
    journey to main content, decoupled from the LCP endpoint — a fast-but-lurching load now scores
    badly on smoothness even with a good LCP (`stall_energy` → display-only, exactly as
    `stall_time` replaced `total_stall`). `derive-v11` adds `worst_void_fraction`
    (purely additive → history re-grades straight from raw). **v12** widens that leg's window
    `FCP→LCP → FCP→loadEventEnd` (same crown metric, `derive-v12`): on a fast link FCP→LCP is
    near-instant, so the felt pause is in the *post-LCP settle* the LCP window read as 0 (an inert
    leg). Widening only reverts the *window* decision — the metric stays a scale-free *fraction*, so
    unlike v10's absolute `stall_energy` it still doesn't correlate with the load duration or
    double-count the freeze. v12 also re-anchors two saturated `best` thresholds to the fastest
    measured value (DNS 1.0 → 0.8ms; page-load 800 → 556.2ms) — secondary axis metrics, so this
    sharpens their subscores + clears the saturation warnings without moving the Overall.
    `runner.score_metrics_under` scores every axis generically via
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
    ("wins above replacement"). Powers `/api/trends/*` and the Dashboard delta chip;
    `relative_overall` stays in the profiles payload (feeds `best_diff`) but is no longer a
    surfaced column. The per-cell fallback-ladder resolution is memoized (`_BaselineResolver`,
    ≤7×24 cells) — the per-run ladder scan + median used to make Settings-Impact quadratic in
    history. (The old ±2h `rolling_baseline_deltas`/`profile_weather_relative` reading was
    **deleted**: the firewall sits on one profile for hours, so a time window rarely held a
    real cohort — time was only a proxy for conditions.)
  - `weather.py` — **measured-weather severity + cohort residuals ("wins above the
    weather")**, the one canonical "vs weather" reading. Weather is defined by **each run's
    own clean covariate readings** (probe DNS/TCP/TLS/latency + nav setup phases —
    `routes_settings._weather_covariates`, `clean` = profile-orthogonal; shaped signals like
    download/`nav_response` are never used), not by the clock: `run_severities` ranks each
    covariate against its all-history distribution and takes the median percentile (0–100
    severity, "DNS at 4 ms when median is 1 ms" = high); `cohort_residuals` bands runs into
    severity quintiles and compares each run's Overall against **other profiles' runs in the
    same band** (own runs excluded; `WEATHER_COHORT_MIN` others required — no cohort, no
    claim). Surfaced per profile as `weather_relative` ({delta_median,p25,p75,count,coverage},
    the **"vs weather"** column), `weather_severity` (median conditions sampled under — a
    sampling-fairness readout), and the **`weather_beater`** flag (residual standing ≫ raw
    standing = "delivered average outcomes where the field delivered below-average — race
    it"); response-level **`weather_crown_suspect`** (residual ranking's top ≠ raw crown →
    "crown may be weather-confounded" alert). Strictly **flag-and-steer: never a crown
    input** — a suspect triggers a race (contemporaneous head-to-head raw data), never a
    re-rank. Empirically gated by `GET /settings/weather-sensitivity` (per clean-covariate ×
    crown-metric Spearman ρ, pooled + **within-profile**, rendered as the Settings-Impact
    "Weather sensitivity" card). The metric-based **"Weather-adj"** reading
    (`weather_adjusted_overall`, setup-stripped fcp/lcp) remains in the API payload but is
    **no longer rendered** — `weather_relative` is the one surfaced "vs weather" reading.
  - `sweep.py` — **Shotgun Sweep**: an on-demand foreground sweep of a grid over the
    registry's `SWEEPABLE_FIELDS` (quantum × target today). Applies each variant for real,
    benchmarks it, **restores the baseline at the end** (`reconcile_interrupted_sweeps`
    restores on startup too). Variant generation, value formatting (`shaper_fields.format_value`
    — the bare-number **wire** value; `format_display` adds the unit for labels only), apply,
    label, and restore all iterate `SWEEPABLE_FIELDS`, so marking
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
    **Every job carries an ETA** (`routes_jobs._eta_ms`, exposed as `eta_ms` + `eta_basis` on
    every entry): "3/40" is what a progress bar shows and not what anyone is asking — the
    question is whether to wait or walk away. Three answers, best first, and the display names
    which was used because they aren't equally trustworthy: **`scheduled`** (a time-boxed job —
    duel window, challenger race, "test current for X" — whose finish is a *deadline*, not an
    estimate), **`measured`** (units left × the per-iteration cost measured over recent runs),
    **`observed`** (the job's own rate: elapsed ÷ done × remaining — the universal fallback, e.g.
    a re-grade counting rows). `None` when genuinely unknowable; a fabricated countdown is worse
    than none, since it's the one number a user plans around. The value is a **duration**, not a
    formatted string (uncountable) and not an absolute server timestamp (which the browser would
    read against *its* clock, folding in any skew): the client anchors it to its own clock on
    arrival and **ticks it down every second** (`JobStatus.Countdown`), re-anchoring on each
    poll, so the readout keeps falling truthfully between polls instead of freezing and jumping.
    It floors at "finishing…" rather than counting into the past. The profile test also gained
    real progress — its completed iterations were only ever in the stage sentence, so its bar was
    indeterminate; they're now summed from its chunks (`job_group`), like the manual-run series.
  - `profile_test.py` — **Test to minimum**: apply a stored profile, run exactly the
    iterations still needed to reach `correlation.min_iterations`, then **restore the
    baseline** (persisted to a `ProfileTest` row; `reconcile_interrupted_profile_tests`
    restores on startup). `/api/settings/test-profile`. The post-apply verify checks the
    firewall reached the target **semantically** — `plan_apply(target, discover())` must have
    no remaining *writable* diffs — **not** by exact fingerprint hash, which is format-sensitive
    and used to false-negative on an externally-supplied target (an AI suggestion), failing
    before any benchmark ran; a genuinely unaccepted field is reported per-field ("did not
    accept quantum …"). Field comparison is **numeric** (`settings_profile._field_equal` via
    `_to_number`), so a duration expressed as `"5ms"`, `"5"`, or `5` all compare equal — the
    firewall echoes CoDel `target`/`interval` back as the **bare option key** (`"5"`, not
    `"5ms"`). Correspondingly the value **written** is always the bare number
    (`_wire_value`/`shaper_fields.format_value`) — writing `"5ms"` to an option-keyed select
    silently doesn't take (the real "apply didn't happen" bug); `"ms"` is display-only
    (`format_display`). Each step is written to `ProfileTest.stage` (snapshot → apply → verify →
    benchmark → restore → done/failed) for a live UI readout.
  - `current_test.py` — **Test current for X minutes**: a time-boxed data-collection loop on
    whatever profile the firewall is **already** on. Unlike the other engines it **never writes
    the firewall** (it measures the live profile as-is), so there's no baseline to snapshot or
    restore — `reconcile_interrupted_current_tests` just closes out an orphaned `CurrentTest`
    row. It benchmarks in short chunks (`runner.CHUNK_ITERATIONS` = 5 iterations each) under the
    coordinator lock until the deadline (or cancel), so each chunk's data is persisted the moment
    it finishes. `/api/current/test` (+ `/current/test/cancel`); the Dashboard drives it. Manual
    runs over 5 iterations chunk the same way (`routes_run._locked_execute_series`): a big request
    fans out into a series of ≤5-iteration runs so an interruption keeps every completed chunk
    (`runner.MAX_ITERATIONS` raised to 500; `run_chunk` is the shared build block).
  - `baseline_test.py` — **Test baseline behavior (SQM off)**: measure the *unshaped* link to
    see what the shaper is actually buying. Snapshots each pipe's on/off state, **disables SQM on
    every pipe** (`provider.set_pipe_enabled`, the pipe on/off toggle — a firewall write *separate*
    from the shaper-field `apply()` model, since `enabled` isn't a profile-identity field), waits a
    configurable **settle** interval, benchmarks a configurable number of iterations (chunked like
    `current_test` so partial data persists), then **restores each pipe's prior state** — always,
    in a `finally` (persisted to a `BaselineTest` row; `reconcile_interrupted_baseline_tests`
    re-enables SQM on startup). Runs on demand **or** on a nightly schedule (`config.baseline_test`:
    `enabled`/`hour`/`minute`/`iterations`/`settle_seconds`, gated in `scheduler.py` by local
    container-TZ time). **All SQM-off runs collapse into one profile**: when any pipe is off the
    shaper params don't apply (the link is unshaped regardless of the values the firewall still
    echoes), so `settings_profile.fingerprint` returns the single canonical `SQM_OFF_FINGERPRINT`
    for *any* disabled-pipe config — the baseline test's runs all aggregate into one "SQM off"
    profile instead of splintering per inert field value. Normal all-enabled profiles hash
    byte-for-byte as before (no history re-key); only SQM-off runs change key. Existing SQM-off
    history is merged by re-keying from each run's own stored settings via
    `POST /api/settings/refingerprint` (the **"Merge SQM-off profiles"** button on Settings Impact).
    `_is_sqm_off` reads the stored `settings` (not the fingerprint), so the "% vs SQM off" baseline
    is unaffected by the collapse. Own thread under the `coordinator` lock. `/api/baseline/*` + the
    **Baseline (SQM off)** tab.
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
    not the full fingerprint; `environment_signature` hashes the non-writable fields).
    Eliminations are tagged **structural vs provisional** (`rank_challengers` sets `structural`):
    only *structural* ones (unreachable — the live environment can't change mid-race) are
    **persisted** across loops; *provisional* ones (optimistic-ceiling < bar / incomplete corner
    coverage) are **re-evaluated every loop**, because the crown/optimistic space is a
    **field-relative percentile rank** that re-normalizes as iterations accrue — so a contender
    ruled out early can re-qualify once the field shifts, instead of being frozen out by a
    transient verdict (`_drive` persists only the structural set). At the
    end it **restores the baseline**, or applies the winner when `auto_promote`. Own thread under
    the `coordinator` lock (so the scheduler defers via `coordinator.busy()`); persisted to
    a `ChallengerRace` row; `reconcile_interrupted_challenges` restores on startup.
    `/api/settings/race` (+ `/race/cancel`).
  - `crown_follower.py` — **Follow best**: keep the firewall's SQM settings on the crowned
    best profile (`compute_profiles` → `best_fingerprint`) as the crown changes.
    **Event-driven, not polled**: the runner queues `notify_run_complete` as each run finishes
    (pure memory); the next scheduler tick runs the **quick filter** (`_needs_full_check`) —
    under the v15 `weighted` crown profile Overalls are independent, so it recomputes *only
    the completed run's profile* (`_profile_overall`, one indexed query) against the cached
    crown/runner-up, and the full `compute_profiles` verdict runs **only when the crown could
    actually have moved** (corner/percentile methodologies are field-relative → always
    escalate; ties escalate). Quiet ticks are a pure in-memory test (zero I/O — enshrined by
    `test_step_quiet_tick_does_no_io`). A slow **backstop** full check
    (`config.crown_follow.interval_minutes`, default 360) plus `poke()` hooks on
    re-grade completion (`score_history_under_current`) and refingerprint catch re-rankings
    that happen without a run completing. Each full check
    does two things. **(1) Track** — when the crown differs from the last recorded one, write a
    `CrownEvent` ledger row; the ledger powers the **crown-churn stats** (`stats`: changes per
    24h/7d/30d, median/current reign, changes/day — "how often does the best profile change?").
    Tracking is read-only and always on, so the stat accrues *before* the user arms following —
    exactly the number needed to judge whether auto-follow would thrash. **(2) Follow** (only
    when `crown_follow.enabled`) — if the firewall isn't semantically on the crown (`plan_apply`
    finds writable diffs; never fingerprint-hash comparison, except for the param-inert SQM-off
    case), apply the crown's writable fields via `provider.apply()` under
    `coordinator.try_hold` (a busy pipeline defers to the next interval). A one-way write like
    "Apply this profile" — being on the crown *is* the steady state, so there's no baseline to
    restore and nothing to reconcile on startup. Never auto-applied: the collapsed **"SQM off"**
    profile (disabling shaping is the baseline test's supervised job; likewise it won't write
    while SQM is currently off) and profiles **unreachable** from the live environment (the
    `environment_signature` guard the race uses). Deliberately a **mirror with no hysteresis**
    (the crown itself has none); the churn ledger + `co_leaders`/`crown_confidence` are what
    tell the user whether the verdict is stable enough to hand over the keys.
    `/api/settings/crown-follow` (GET status+stats+ledger, POST config, POST `/sync` = check
    now); driven by the top-bar **"Follow best" switch** (`FollowBest.tsx`) with a status/churn
    popover.
  - `duel.py` — **Duel ladder**: interleaved head-to-head adjudication, the controlled-trial
    complement to the observational pooled crown. **Counterbalanced** alternation — one iteration
    a side, with the lead alternating every pair (ABBA) — between an incumbent (the pooled crown
    at window start) and a ladder of challengers
    (heirs priority order, reachability-filtered), so each adjacent pair shares its weather
    **by construction** — a thin new variant can be adjudicated against a 3000-iteration crown
    in one night because it races the crown-five-minutes-ago, never its history. Each matchup
    runs a **sequential stopping rule** (Wald SPRT on the pair-win rate, `duel.p1`/`alpha`,
    min/max pairs) plus a practical-significance floor (`duel.min_margin` Overall points —
    statistically real but negligible edges are recorded as draws), so the window never burns
    all night on a settled question: the ring's #1 defends the next bout (see **The ring's #1
    defends** below), and decided pairs get a `duel.rematch_days` cooldown. Two-ledger discipline: duel
    *runs* flow into the pooled record like any runs; duel *verdicts* live beside it as the
    head-to-head ledger (`Duel.matchups`, surfaced on the **Dueling Champions** tab) and never
    enter the pooled score. The duel **never writes a winner to the firewall** — it restores
    the pre-duel baseline (`reconcile_interrupted_duels` on startup); acting on verdicts is
    the crowning policy's job. Runs nightly (`config.duel`: enabled/hour/minute/timezone/
    duration, gated in `scheduler.py` like the baseline test) or on demand (`/api/duel/*`).
    The window is **set as a start/finish clock pair**, not a minute count: `GET/PUT
    /duel/config` carries derived `end_hour`/`end_minute`, and a PUT with an end time derives
    `duration_minutes` (`_window_minutes`, wrapping past midnight — 22:15→01:45 = 210 min;
    a zero-length window is refused). `duration_minutes` stays the canonical value the engine
    counts down, so a window over 24h is still expressible. The on-demand button is the same
    idea client-side: the page's "duel now until" clock becomes minutes-from-now at press.
    **Bouts are adjudicated on the paired MARGINS, not the pair-win count** (`duel.method`,
    default `"margins"`). The original rule was a Wald SPRT on which side won each pair — a
    **sign test**, which discards *by how much* and so throws away most of the evidence a duel
    produces: measured against a true 1.0-point edge with ~1.5-point run noise, a 15-pair bout
    called a winner just **28%** of the time ("profiles are winning but aren't presented as
    winners"). `PairedEvidence` runs a one-sided **Wilcoxon signed-rank** test on the
    challenger-minus-incumbent Overall margins (`wilcoxon_p`: exact below 25 pairs — the regime
    duels actually run in, where the normal approximation is needlessly conservative —
    tie-corrected normal beyond), which lifts the same bout to **~60-70%** and calls a 2-point
    edge essentially always. Peeking after every pair inflates false positives (an uncorrected
    5% test fires on ~26% of true ties over a 40-pair bout), so the threshold is divided by a
    Pocock-style `peek_penalty` **fitted by simulation** (≈`3.32·ln(peeks) − 2.97`: alpha/3 at 6
    peeks, /5 at 11, /9 at 36) which holds the realized false-verdict rate at ~alpha. The
    practical floor (`min_margin`) still gates every verdict — significance and *worth acting on*
    are separate questions — and the SPRT walk is retained purely as the **futility** detector
    (its "pair wins ~50/50" exit still ends settled ties early). The legacy sign test stays
    available as `method="pair_wins"` for comparison. Crucially the margins rule has **no cap at
    which a verdict becomes unreachable** — more pairs only ever help.
    **"If it wins back to back, it wins"** is the rule, at the length that isn't luck
    (`duel.streak_to_decide`): an unbroken run of n pairs has p = 1/2ⁿ, so a bout ends the
    moment the run clears the threshold — 6 straight under *quick*, 8 under *balanced*, 12
    under *strict*. The length is not arbitrary and short streaks are worthless: between two
    **identical** profiles a 30-pair bout throws up a 3-in-a-row 99.7% of the time, 5-in-a-row
    62%, 8-in-a-row 9%. A *pure* streak rule would also be glacial (a profile winning 70% of
    pairs needs ~54 pairs on average to string 8 together), which is why the paired test backs
    it: a clean run wins instantly, and a profile that goes 12–3 without ever stringing 8
    together still gets called. The margin floor (`min_margin`) now defaults to **0** — a
    consistent win counts however small, matching the pooled crown, which likewise has no floor
    ("the profile that wins wins"); raising it is opt-in, in the advanced panel.
    An explicit **"N wins in a row ends it"** rule (`duel.streak_wins`, the **snap** preset's
    3) overrides the derived streak *and* `min_pairs` — a field that says "3 in a row wins"
    has to mean 3. It is a deliberate trade on a ladder that keeps running: measured against a
    true 1-point edge, 3-in-a-row names the better profile **~91%** of the time and the worse
    one ~7%; between genuinely equal profiles it is a coin toss, which costs nothing because
    either answer is right, and the next bout re-runs it. Read the **standings**, not one bout.
    The ladder can also **run continuously** (`duel.continuous`, gap `continuous_gap_minutes`)
    rather than once a night — the scheduler starts a session whenever `coordinator.busy()` is
    clear and the gap has elapsed, so monitoring and manual runs still get the pipeline, and
    every session still restores the baseline. And it races **leaders, not randoms**:
    `build_queue(contenders="leaders", top_n=…)` ranks the **whole field** by Overall
    (confident profiles first — two runs and a lucky score is noise, not a contender), puts
    the pooled crown first whenever it isn't the one defending, and lets the heirs tail follow
    so unknowns still get measured. Reachability is tested **per profile against the live
    environment** (`_reachable`). It briefly wasn't: reachability was inherited from the heirs
    pass, which silently restricted the "leaders" pool to the heirs themselves — and heirs are
    *by definition* the under-sampled/stale profiles, capped at `challenger.heir_count` (5) —
    so the mode built to avoid filler raced nothing but filler, and with an empty heirs list
    produced no queue at all (`test_leaders_are_drawn_from_the_field_not_from_the_heirs_list`).
    `_recently_decided` likewise scans duels **by `finished_at` within the cooldown**, not
    "the last 20 sessions" (a continuous ladder finishes several a day, so a row cap covered
    ~3 days of a 7-day cooldown).
    **Counterbalancing + settle, so a pair measures the profiles and not the schedule.** The
    incumbent used to run first in *every* pair, which makes "went first" and "is the incumbent"
    the same variable: any position-in-pair effect (state the previous run left behind, a
    still-warm cache, the shaper freshly reconfigured) lands on the same side every time and is
    indistinguishable from a real difference. The lead now alternates each pair — ABBA, recorded
    as `lead_alternated` on the matchup so the counterbalance is auditable from the ledger — and
    the margin stays challenger − incumbent, so the verdict doesn't care who ran first
    (`test_pairs_alternate_which_profile_runs_first`). Each leg also waits `duel.settle_seconds`
    (default 3, GUI-editable in the advanced panel) after `_apply_profile` before measuring: every
    leg is preceded by a `setPipe` + reconfigure that rebuilds the queues, and the baseline test
    has always settled before believing a measurement. It is symmetric across both sides, so it
    never biased a verdict — it just put reconfiguration noise into every pair, and noise costs
    pairs. Mocked-engine tests set it to 0 and score each leg **by the profile applied**, not by
    its position (`_score_by_profile`) — a fake keyed on run order would bake in exactly the
    confound the alternation removes.
    **The operating model: always be running the bout most likely to unseat the belt**
    (`contender_order`, `ledger_ratings`, `duel.contenders = "ring"`, the default). The queue
    used to be ordered by the **pooled** Overall, which made the ladder circular: the duel
    exists to be the independent check on the pooled verdict, and the pooled verdict decided
    who got checked. A profile the ring had *proven* strong stayed buried if its pooled score
    was mid-table, and a pooled-flattered profile got first billing every session after losing
    five bouts running — measured end-to-end, the old order raced the two profiles the ring had
    already beaten and put the one that beat the belt **last**. Challengers are now ranked by
    the **ring's own findings**: each one's **optimistic ceiling** on the fitted head-to-head
    rating (`rating + CEILING_SIGMA·rating_se`), which does the right thing three ways at once —
    a strong established contender ranks high on its rating, an unknown ranks high on its wide
    error bar (it might be anything, so go and look), and a well-measured weak profile ranks low
    and stays out of the way, because the ring has answered that question and re-asking finds
    nothing. Four tiers, and the ring is never given to a lower one while a higher still has
    someone: `CROWN_TIER` (the pooled crown — the two verdicts disagreeing is the most
    informative bout there is) < `CONTENDER_TIER` (ceiling clears the belt-holder's rating) <
    `UNTESTED_TIER` (never been in the ring, so anything is possible; ordered among themselves
    by pooled Overall, the **only** job pooled keeps) < `OUTCLASSED_TIER` (the ring says they
    can't reach — raced *last*, not never, the same discipline the cooldown follows). A profile
    with no pooled score at all is still raceable: requiring one would put the pooled verdict
    back in charge of who gets checked. `ledger_ratings` fits the same Bradley–Terry model the
    standings rank on, straight from the matchup ledger without the pooled join, so matchmaking
    can consult it every session cheaply; `_pair_record` is the one accumulator both share.
    `contenders="leaders"` keeps the former pooled ordering for comparison and `"heirs"` the
    oldest exploring order.
    **A thin profile earns the ring by what it could still do, and an unexamined claim on the
    crown is raced IMMEDIATELY** (`contender_order`'s `LIVE_THREAT_TIER`). The pooled Overall is
    the winner *on paper*; the ring is the real-world back-to-back result. Paper decides who gets
    to make a **claim** — for a profile with no ring record, that claim is its **pooled optimistic
    ceiling** (`optimistic`, the same p75 number the heirs card and the challenger race use, so
    all three agree on what counts as a threat) measured against the pooled crown's Overall — and
    the ring decides whether the claim survives contact. A five-iteration profile whose ceiling
    reaches the crown is a **live threat** and runs at `LIVE_THREAT_TIER` (1), **ahead of the
    rated contenders** (2): a rated contender has been examined and its ceiling is a statement
    about beating the *belt-holder*, while this is an unexamined claim on the *crown itself* —
    that it may already be the best thing measured and nobody has checked. Racing it answers the
    claim head-to-head **and matures it**, since a bout's paired runs enter the pooled record like
    any others, so the same hour buys the verdict and the evidence; waiting is what costs, because
    the claim is only interesting while unresolved. Among themselves live threats are ordered by
    ceiling, biggest first. A profile whose own runs fall short even optimistically drops to
    `UNTESTED_TIER` — **given up on, never excluded**, since five iterations is a weak "no" and
    the ring hasn't actually asked. The promotion applies **only where the ring has no opinion**:
    the moment it *has* a rating, paper stops being consulted entirely
    (`CONTENDER_TIER`/`OUTCLASSED_TIER` read the fitted rating, so a noisy pooled number can't
    re-litigate a question the ring already answered). That is the whole point of running two
    verdicts. This is what makes the
    explore→duel relationship real — a proposal is measured briefly for an *initial placement*,
    and the ring then spends its time maturing whatever could displace the best profile found so
    far, which is also why it isn't racing #432 against #567. It changes **matchmaking only**:
    duel verdicts still never enter the pooled score, and the crown is untouched.
    **The rematch cooldown ORDERS, it never excludes** (`contender_tiers` / `next_matchup`).
    Queued profiles carry a priority **tier** — `CROWN_TIER` (the pooled crown) <
    `CONTENDER_TIER` (confident, scored) < `FILLER_TIER` (thin/untested) — and the ring is
    **never given to a lower tier while a higher one still has someone waiting**. Within a
    tier a matchup not fought inside the window goes first; otherwise the best of that tier
    is **re-raced**. This is the fix for the repeatedly-reported *"random duels not involving
    the crown"*: the cooldown used to set a recently-fought contender aside and fall through
    to the next queue entry, which is self-defeating on a ladder that runs continuously —
    the top of the queue is what gets fought first, so it is also what goes on cooldown
    first. Within a day or two the crown and every leader were cooled and the only entries
    still un-fought were the ones nobody had ever raced, so a mode built to race the leaders
    raced nothing but filler, and the pooled crown — first in the queue *by design* — was
    the first profile pushed out of the ring (reproduced end-to-end: the old loop's order was
    `untested → untested → crown → leader → leader`, the new one
    `crown → leader → leader → untested → untested`;
    `test_the_cooldown_reorders_contenders_it_does_not_hand_the_ring_to_filler`). `build_queue`
    returns the queue already **tier-sorted** (stable, so intra-tier order is untouched), and
    `fight_card` reports each entry's `tier`/`tier_name` so the preview shows the real running
    order — a projection rather than a schedule, since the engine re-decides both sides
    before every bout. Because the defender is re-read each time, the two names in the ring
    by bout six can be neither the profile that walked in with the belt nor the crown — which
    reads as "randoms" unless the chain is visible, so each bout records **`challenger_why`**
    (its tier, and whether it was re-raced), the live stage line reads *"Bout 3 · X (belt)
    defends vs Y (pooled crown) — pair 4"*, and the page lists **"this session so far"**:
    every bout and who took
    the belt. An empty queue
    reports *why* (`_no_contenders_reason`): nothing scored under the current methodology, or
    the live environment matches no stored profile. `contenders="heirs"` keeps the exploring
    order.
    **One dial, not six fields** (`duel.PRESETS` / `preset_for` / `preset_config`): "when is
    someone the winner?" is a single question, and it had been spread across six interacting
    numeric fields nobody could reason about together. `GET/PUT /duel/config` carries a
    `preset` — **quick** / **balanced** (default) / **strict** — that writes `alpha` +
    `min_pairs` + `max_pairs` in one move, and each preset is labelled with its **measured**
    behavior (wrong-verdict rate, how often it spots a 1-point edge, typical pairs to decide;
    `test_preset_behaviour_matches_its_promise` re-checks the ordering). A preset deliberately
    never touches the practical margin or the schedule — different questions — and hand-editing
    any derived field reads back as `"custom"` rather than pretending a preset is active. The
    page shows the three presets plus the single "ignore differences smaller than X points"
    field; the raw knobs live behind **Show advanced settings**.
    `duel.sprt_requirements` answers the question the raw settings hide — **what it actually
    takes to win a bout**. Each pair won moves the walk by `ln(p1/0.5)` and each pair lost by
    `ln((1-p1)/0.5)`, a *bigger* step, so the pair cap and the evidence bar interact: at
    p1=0.70/alpha=0.05 with `max_pairs=15` a winner needs **13 of 15 (87%)**, and a profile
    genuinely taking 80% of its pairs is recorded as a draw forever (the real "why is everything
    a draw?" report — a 12–3 bout reaches LLR 2.505 against a 2.944 boundary). `GET /duel/config`
    returns `decision` (`sweep_pairs` / `wins_needed` / `win_rate_needed` / `restrictive`) and the
    page states it outright, warning when the cap demands a near-sweep. `p1`/`alpha` are editable
    from the same card ("Edge to detect" / "Error rate").
    **The ring's #1 defends, and the matchup is re-decided before EVERY bout**
    (`ring_leader` / `select_incumbent` / `next_challenger`). This is the operating model in
    one line: *always be running the bout most likely to unseat the best profile we have.*
    Two earlier rules got in the way of it. The belt went to **whoever survived the previous
    session** (`latest_champion`), which is not the same profile as the best one; and the
    running order was a **queue built once at session start**, walked to the end. Together
    they produce the repeatedly-reported symptom: a mid-table profile wins one bout, inherits
    the belt, and the ladder spends the night defending *it* against opponents chosen for a
    defender who had already left the ring — bouts between #83 and #128 that say nothing
    about whether anything beats the leader.
    Now the ledger is refit before every bout (`ledger_ratings`; `_ledger_sessions` reads the
    **running** duel, so a challenger that just won is re-rated before the next matchup is
    picked), the defender is the ring's **#1 by `rating_floor`** — deliberately the same
    number the standings rank on, so the belt and the top row can never name different
    profiles — and the challenger is whoever `contender_order` puts first *against that
    defender's* rating. `select_incumbent` falls back to the pooled crown only when the ring
    has nothing to say (empty ledger, or no rated profile the live environment can be set
    to), and says which in `incumbent.why`; `fight_card` calls the same helper, so the
    preview can't name a different defender than the one who walks out.
    A consequence worth stating plainly: **the winner does not automatically stay on.**
    Beating the leader promotes you when it lifts your floor above its — the same bar the
    standings apply — so a thin challenger that wins one bout keeps a wide error bar and the
    leader defends again, against the next-best threat; the winner's rating rose, so it comes
    back around quickly and a second win usually settles it. `Duel.champion_fingerprint` is
    written **every bout** (the belt changes hands mid-session, and on a continuous ladder a
    badge that waits for session end reads hours stale) and at session end it is the ring's
    #1 **unfiltered by reachability** — the champion is a statement about the ledger, and the
    standings it must agree with aren't filtered either; an unreachable champion simply never
    defends, and the crown follower already refuses to apply one. Still display only:
    `latest_champion` filters to COMPLETE sessions, so the crowning policy never acts on a
    provisional holder (and can't, mid-duel: the duel owns the firewall while its window is
    open). Two exclusions, deliberately different strengths: a pair already fought **in this
    session** is a hard skip — with one exception: **unfinished business gets its rematch.**
    A challenger that *won* its bout but didn't take the belt (its floor hasn't cleared the
    leader's) has raised the most informative question on the ledger — its record says it is
    the better profile and the standings still say the other one is — so that pair is
    re-opened, **once** per session, and its freshly-raised rating puts it near the top of
    the order on its own. Without it the profile with the single strongest claim to the belt
    was set aside for the rest of the session while the leader defended against fresher,
    weaker challengers (the *"two profiles beat this one but it keeps the belt"* report).
    Gated on the defender actually being ledger-derived, since on the pooled fallback "the
    belt didn't move" says nothing about the head-to-head record. Meanwhile the **rematch
    cooldown only orders within a tier** — the fix from the
    "random duels" report, since the leader and its nearest rivals are the first matchups to
    cool precisely because they are fought first. One interaction is named rather than
    hidden: a bout can be a **draw** on the practical margin floor (`min_margin`, default 0)
    while the *rating* — fitted to pairs, magnitude-blind by design — still moves the belt,
    because "is this difference worth acting on?" and "which profile is stronger?" are
    different questions.
    **The bout in progress is structured state, not a sentence** (`_live_scoreboard`,
    persisted to `Duel.live` after every pair and cleared at session end). A scoreline inside
    the stage line — *"pair 4 (2-1)"* — can't say **whose** wins those are, by how much, or how
    near a verdict is, which is the whole of what a live duel readout is for. The payload
    carries both sides with their own tally, who leads (`level` when tied, never an implied
    lead), the **median margin signed from the challenger's side** (positive = challenger
    ahead; this, not the pair count, is what the verdict is decided on), the per-pair margin
    series, progress against `min_pairs`/`max_pairs`, the current streak against the streak
    that would end it, and the running p-value against its peek-corrected threshold. The
    Dueling Champions page renders it as a scoreboard (`BoutScoreboard`) — two tallies, a
    split bar, and a per-pair margin strip so a steady lead and one lucky pair don't look
    alike — falling back to the stage sentence between bouts, when there is no score to show.
    `duel.fight_card` (`GET /api/duel/card`) answers "are we just racing randoms?" with a
    list rather than a paragraph: the champion plus the ordered queue a duel started *now*
    would work through, each entry carrying its Overall, iterations, why it's in the queue
    (contender / limited-data / stale / untested) and whether it's on rematch cooldown. It is
    built by **the same `build_queue` the engine runs**, so the page can't promise one order
    and the duel fight another; it costs a `compute_profiles` pass, so the **Who's fighting**
    card fetches it on demand rather than on page load. The page header also states what
    pressing "Duel now" will do — until when, how long, who defends against how many, and what
    ends a bout — since a button whose behavior lives in three other fields isn't a control,
    it's a guess.
    **The standings rank on a fitted STRENGTH, not on match points** (`rating.py`,
    `fit_bradley_terry`). Points (3/1/0) record *how many* you beat but not *who*: beating the
    champion and beating a profile nobody has measured were both worth three, the belt-holder
    farmed points simply by defending (the winner stays on, so it fights more than anyone), and
    two profiles that never met could not be compared at all — so a profile could beat the #1
    and still sit at #4, the reported symptom. A **Bradley–Terry** fit over the ledger
    (`P(i beats j) = γᵢ/(γᵢ+γⱼ)`, Zermelo/Ford MM iterations, reported on the Elo scale with a
    Fisher-information error bar) fixes all three from the same data: beating a strong profile
    moves you a lot and beating a weak one barely at all, losing to the best costs little, and
    profiles that never met are comparable **through shared opponents**. It is a *fit over the
    whole ledger*, not a running Elo — deterministic and order-independent, so the table
    re-derives from the record exactly like every other score in PathBrain. The unit of evidence
    is the **pair**, not the bout (`wins_incumbent`/`wins_challenger`), so a hard-fought 12–8
    outweighs a 3–0 snap and a *drawn* bout still informs the rating instead of being discarded.
    A **prior** of `DEFAULT_PRIOR_PAIRS` virtual pairs against a phantom average opponent keeps
    unbeaten records finite and shrinks thin ones toward the field; 4 is calibrated against both
    failure modes at once and `test_the_prior_is_calibrated_between_the_two_failure_modes` pins
    them — at 2 a 3–0 sweep over the *weakest* profile outrates a veteran that beat the whole
    field, and too heavy a prior stops a win over the *strongest* moving anyone, which is the
    entire point. A rating is flagged **provisional** (still shown and ranked, marked `?`) until
    it rests on ≥`PROVISIONAL_PAIRS` pairs **and** ≥2 opponents — one opponent is a single edge
    restated, not a position in the network.
    **The standings rank on the conservative FLOOR, not the fitted rating** (`RANK_SIGMA`,
    `rating_floor` = `rating − 1·rating_se`). The reported symptom was *"why is the top profile
    ranked #1 with only one opponent?"* — and it genuinely was: a 4–1 record against a single
    opponent fitted 55 Elo points above a 29–15 record against seven, on error bars of ±123 and
    ±53. The point estimate was higher; the gap was a third of the pooled bar, so the ordering
    was noise printed as a leaderboard. Ranking on what a record has *demonstrated* rather than
    what it *suggests* fixes it without discarding anything: the thin record keeps its high
    rating (and its row), and the wide bar that comes with it is what costs it the top spot
    until it has been measured. Note the **symmetry with matchmaking**, which ranks on the
    optimistic **ceiling** (`CEILING_SIGMA`): optimism decides who to *race*, because a profile
    that might be good is worth measuring; pessimism decides who is *best*, because a claim to
    be best has to be earned — same machinery, opposite directions. It is deliberately **not** a
    significance gate: two well-measured profiles a hair apart both have narrow bars, so the
    better one still ranks first ("best by a statistically insignificant fraction is still
    best"); the floor only overturns an order that rests on a bar wider than the gap.
    **The reigning champion IS row 1** (`ledger_leader`). The badge used to read a stored
    `Duel.champion_fingerprint` written at session end — the profile that *survived that
    session* — while the standings are fitted live over the whole ledger, so the two were
    computed by different logic and inevitably disagreed: every row written before the belt
    became the ring's #1 recorded a survivor, and a bout in a **running** session moved the
    table without touching any stored row. Both `standings()["champion"]` and
    `latest_champion` now derive from the same fit (`ledger_leader` = highest `rating_floor`),
    so "reigning champion", "row 1", and "who defends" cannot name different profiles.
    `latest_champion` — which the crowning policy acts on — keeps its guards, translated onto
    the ledger: `decisive` (a record of nothing but draws demonstrates nothing) and a
    freshness window over the champion's own **most recent completed** bout, so it either
    agrees with row 1 or returns None (fall back to pooled) — never a third answer. A
    mid-session leader is therefore shown immediately but is not yet actionable.
    `duel.standings` (`GET /api/duel/standings`) aggregates the ledger into the **head-to-head
    league table** that page ranks on — per profile the **rating** (+ `rating_se` /
    `rating_pairs` / `rating_provisional` / `expected_pair_wins`), W–L–D, match points (win 3 /
    draw 1),
    decisive-win + pair-win rate, median Overall-point margin *signed from that profile's own
    side*, opponents beaten/lost to, title count — plus the reigning champion (with reign
    length) and the `head_to_head[a][b]` matrix. It is **pure ledger**: only decided matchups,
    nothing pooled, nothing averaged over history, so it never touches the crown. Rank order:
    **`rating_floor`** → pairs behind it → points (`ranked_by: "rating_floor"` + `rank_sigma` in
    the payload); the floor is printed as its own sortable **"Proven"** column beside the rating
    rather than left implied in a sort order, and the points / win-rate / pair-rate columns stay
    as the readable ledger and remain sortable. Each row also carries the
    profile's **pooled Overall** (`_pooled_overalls` → `crown_follower._profile_overall`, one
    indexed query per profile rather than a full `compute_profiles` pass) so the ring record
    and the raw measured record sit on one line — a profile winning its bouts while mid-table
    on Overall is exactly what running two verdicts is for. The table is **sortable on every
    column** client-side (`compareStandings`, nulls last in both directions), and the ring
    standing is the **default order**, named outright as the **"Duel rank"** column (it was
    an unlabelled `#`). Beside the pooled Overall sits an **"Overall rank"** column plus a
    ▲▼ gap chip on the rank itself, so the two verdicts are one glance apart: "duel rank 1,
    ▲5" is a profile that beats everyone in the ring while sitting 6th on the raw measured
    record — the disagreement running two verdicts exists to surface.
    `GET /api/settings/crowns` (`routes_settings.crowns`) serves **both verdicts side by
    side** for the Dashboard's **"The two crowns"** card (`TwoCrowns.tsx`): the pooled crown
    (trophy) and the duel champion (belt/medal), each marked *following* or *for reference*
    per the crowning policy, plus an `agree` flag when they name the same profile. It is
    deliberately **cheap** — the pooled crown is read from the crown-churn ledger
    (`crown_follower.current_crown`, one indexed row, since tracking is always on) rather
    than recomputing `compute_profiles` on every dashboard load, and the duel side reads the
    matchup ledger; neither triggers a scoring pass. A duel verdict aged past
    `duel.rematch_days` is still shown, labelled expired, instead of vanishing.
  - `explore.py` — **the exploration landscape**: the one engine that asks what we *haven't*
    tried. Every other engine judges profiles that already exist (Settings Impact ranks them,
    the duel adjudicates them, the race promotes the under-sampled) — none answers "what's
    untested that might beat all of this?", which with ~150 profiles varying several levers at
    once is not answerable by eye. Read-only; it proposes, and the existing
    `POST /settings/test-settings` path measures. Four outputs from one `compute_profiles` pass
    (`GET /api/explore/landscape`, on demand — it costs that pass, same bargain as the fight card):
    **(1) Axes** — every writable lever kept **per pipe** (`"<pipe>::<field>"`; the Download and
    Upload legs are separate knobs and don't behave the same), dropping any lever that never
    varies — a constant isn't a lever. **(2) Response curves** — median Overall at each measured
    value, plus Spearman ρ and a one-word `shape`. The curve is the point: a lever with ρ≈0 can
    still have an obvious peak in the middle, which no single coefficient can express. An
    **interior peak beats the correlation** when labelling the shape — a lever whose far end is
    disastrous carries a strong monotonic ρ even when its best value is mid-range, and reading
    that as "lower is better" would send you to the low end the curve says is worse
    (`test_an_interior_peak_beats_a_strong_correlation`). **(3) Gaps** — a **`gap`** is a blank
    between two tested values (bracketed, one run settles it); an **`edge`** is the best value
    being the end of the range (not bracketed at all — the optimum may lie beyond). **(4)
    Interactions** — per axis pair, split each at its median and read the 2×2 contrast
    `(HH−LH)−(HL−LL)`: does the better half of one lever depend on which half of the other you're
    in? That's the "does the best download quantum depend on the upload quantum?" question, which
    no per-lever view can answer. A pair is only reported when the contrast clears **both**
    `INTERACTION_MIN_CONTRAST` (1.0 pt) **and** `INTERACTION_MIN_SHARE` (15%) of the spread
    between its four corners — otherwise every pair "interacts" on rounding noise, which is worse
    than saying nothing.
    **Confounding is modelled, not ignored.** A response curve is `median(Overall | lever=v)`
    — it *marginalizes over whatever the other levers happened to be* at that value, so it
    answers "how do profiles with this value score?" when the question being asked is "what
    happens if I change **this** profile's value?". In a hand-built field those have different
    answers, and the curve confidently reports the wrong one (the reported case: the marginal
    curve named an upload quantum best that was worse than its neighbour in every controlled
    comparison, because it co-occurred with the strong download family). Four responses, in
    descending order of trust:
    **(1) Matched pairs** (`matched_pairs`) — profiles differing in **exactly one** lever, found
    by `_writable_signature` over *all* writable fields (not just the varying ones, so two
    profiles can't be called siblings over a field the axis list dropped). Everything else is
    identical by construction, so the Overall gap is that lever's effect with zero confounding:
    a controlled experiment already sitting in the observational record. Reported per
    *transition* (`7313 → 7000: −1.4 over 3 pairs`), since that's the actionable unit, sorted by
    **evidence strength** (agreeing pairs first) rather than effect size — a dramatic
    single-pair number is an anecdote and sorting on it buries the findings.
    **(2) Conditioned curves** (`conditioned_curves`) — each lever restricted to profiles that
    differ from a `reference` (default: the best measured) in the plotted lever plus at most
    `CONDITION_MAX_OTHER_CHANGES` others. Rendered as a dashed overlay on the same axes as the
    marginal curve, because the disagreement *is* the finding.
    **(3) An imbalance diagnostic** (`_imbalance`) — per curve point, which *other* lever is
    systematically skewed there and by how much (`IMBALANCE_THRESHOLD`, in normalized units).
    It doesn't fix the curve; it names which points not to believe and why, on the chart.
    **(4) Basins** (`basins`) — local maxima under **one-lever moves**: profiles no measured
    sibling beats. Nearness is counted in *levers changed*, never Euclidean distance —
    on a coarse grid two values of a 3-value lever are half its range apart, so a distance
    radius measures how many values a lever happened to be given rather than how alike two
    profiles are, and "no one-lever change beats this" is exactly what makes something a
    local optimum for a coordinate-wise search. Several basins ≥2 levers apart is the
    demonstration that the levers are **coupled**, that no single change crosses between
    them, and that a marginal curve averaging across them describes none of them.
    **Candidates are priced from the best evidence available**, in that same order:
    a matched pair for the exact move → the *parent's own* conditioned neighbourhood → the
    marginal curve, **shrunk by `CONFOUNDED_SHRINK`** when that curve is flagged confounded
    (believing a confounded average whole is precisely how it becomes a confident prediction).
    Each candidate carries its `evidence` provenance so a controlled number never reads like an
    extrapolated one. A **transplant** move was added for the coupled case — give a profile a
    value that scores well *elsewhere* but that it has never run; the value is old, the
    combination is new, and it is the only candidate kind a matched pair can price, since the
    others propose values nobody has measured. Multi-lever candidates set `multi_lever` and get
    a widened band (`MULTI_LEVER_UNCERTAINTY`) that is **subtracted from the score, not added**:
    a UCB rewards uncertainty, so inflating the band without discounting it would push exactly
    the candidates we trust least to the top. Predictions and upsides are clamped to the
    Overall's 0–100 scale.
    **Candidates** are the headline: existing profiles with one (or two) levers moved to a value
    nobody has tried, each stating *why* that value is interesting — fills the widest untested
    gap, refines between the best measured value and its neighbour, or steps past an edge.
    **"Nobody has tried" is answered from two sources, not from the modelling pool**
    (`already_tried` / `_claimed_coords`). The pool is *confident profiles only* — a lucky
    Overall on two iterations is noise, and noise in the model comes back out as a
    confident-sounding prediction — but "is this measured well enough to model?" and "has anyone
    run this?" are different questions, and answering the second from the first re-proposes
    everything below the confidence bar. With **"Test now" at 5 iterations against a 15-iteration
    bar that is every quick test**: the page kept offering a profile whose own measurement was
    sitting on the ledger right below it, with a prediction that ignored it (the reported *"still
    suggesting a profile that already exists and has already been tested"*). So the dedup set is
    **(1)** the coordinates of *every* profile with settings on record — scored or not, confident
    or not — and **(2)** every proposal a benchmark has already been spent on, reconstructed from
    the ledger (`explore_tracker.claimed_moves`: a claim's parent coordinates with its moves
    applied). (2) is not redundant: when the firewall can't be driven exactly to a proposal it
    settles on a neighbour and the runs are filed under *that* profile's coordinates, so the
    proposed point never enters the field and a measured-profiles check alone would re-offer it
    forever. The ledger read is best-effort — bookkeeping must never blank the landscape.
    **A proposed value is run through `coerce_value` first**, so the number on screen is the
    number that will run: a lever is only as fine-grained as the firewall's own control, and
    CoDel target/interval are selects keyed by a bare integer — proposing `6.5ms` proposed
    something that cannot exist, the apply quantized it to `6`, and the ledger then graded
    "3ms → 6.5ms" against a profile running 6ms. They
    are ranked by an **upper confidence bound** (`predicted + EXPLORATION_WEIGHT · uncertainty`),
    because the question is "where might we beat everything measured?", not "where would we score
    respectably?" — different questions with different answers, and only the first one explores.
    `predicted` is **anchored on the parent** (its measured Overall, adjusted by what the response
    curve says about moving that lever) rather than a global regression: a smoothed global average
    can never exceed the best value it was fitted on, so it drags every candidate branched from
    the winner back toward the field mean and reports the leader's own neighbourhood — the one
    place worth looking hardest — as unpromising. Past the tested range the curve is **clamped,
    not extrapolated**: continuing a trend past the last measurement is exactly the confident
    guess that wastes a night, and "we don't know out there" belongs in the uncertainty term
    (which is what makes the candidate attractive at all). `uncertainty` is the field's own spread
    over `√(1 + effective neighbours)` under a Gaussian kernel in normalized lever space, so open
    space carries the full spread. Deliberately **deterministic and dependency-free** (no numpy,
    no fitted black box) — every number re-derives from the profile table and explains in a
    sentence, the same standard the crown and the duel ratings are held to. Thin profiles are
    **never excluded — weighted** (`evidence_weight`, `THIN_WEIGHT_FLOOR`): a five-iteration
    reading is thin but it is the only reading anyone has of that point, and excluding it meant a
    quick test taught the model nothing until it crossed the confidence bar (50 iterations on a
    real link — most of the way to never). `confident_only` now scales a profile's contribution
    by how much measurement stands behind it (linear to the bar, then flat, floored at 0.15)
    instead of gating the pool: `_weighted_median` aggregates the curves (degenerating *exactly*
    to the plain median at equal weights, so nothing moves until the evidence actually differs)
    and a thin neighbour pins a candidate's uncertainty down less. Three protections stop
    inclusion making the page worse, because a lucky two-run 99 must inform without steering:
    it is weighted down; it cannot become a **parent** (candidates branch from *settled* ground —
    the right response to a promising thin profile is to mature it in the ring, not to extend it
    before anyone knows its number is real — with thin parents used only when fewer than
    `MIN_POINTS` settled profiles exist); and it cannot set the bar (`_best_established` keeps
    "best measured" on the best *confident* profile, the same one the crown names). **The seam it leaves is the overnight module**: the
    engine is a pure function returning runnable per-pipe overrides, so a scheduler can alternate
    *explore* (measure these candidates) with *adjudicate* (duel the survivors) and the field
    grows toward the optimum instead of only being re-ranked.
    **Measuring a candidate is two lengths, not one** (`POST /api/explore/test`): **"Test now"**
    runs `explore_tracker.QUICK_ITERATIONS` (5 — one `runner.CHUNK_ITERATIONS` block, so it
    persists as a single chunk), enough to see whether a recommendation went anywhere and cheap
    enough to try several in an evening; omitting `iterations` keeps the old **"Test to minimum"**
    top-up for a candidate worth settling. Both go through the one shared
    `routes_settings.start_settings_test` (validate reachable → apply → benchmark → restore under
    the coordinator lock), so a caller can't invent a second set of reachability rules. The
    candidate is materialized on the **parent's stored settings** (`explore.full_overrides`), not
    on whatever the firewall is currently on: `_apply_writable_overrides` overlays what it's given
    onto *live*, so sending only the moved lever measures "the firewall as it stands, with this
    lever moved" — the proposed profile only when the firewall happens to already be on the parent.
    And a profile test always restores the baseline, so *live* is the baseline profile at the
    start of every test — the proposal is reproduced only when the parent happens to *be* the
    baseline, and every candidate branched from another parent measured something nobody
    proposed. **The existing integrity checks cannot catch this** and were never meant to:
    `profile_test` verifies the firewall reached `target`, but `target` is *defined* as
    live-plus-diff so the verify is true by construction; `runner.execute_run`'s
    read-before/read-after compares the live firewall **against itself**. Both answer "is the
    measurement internally consistent?" — it is, a real stable profile was applied, measured
    and attributed to its own fingerprint — not "is the applied profile the one that was
    proposed?". When the parent's settings can't be found the levers fall back to live and the
    recommendation is stamped with a `note` saying so.
    **A predicted fingerprint is not an identity — never correlate on one.** `fingerprint(target)`
    hashes the profile we *intend* to reach; a run is filed under `fingerprint(normalize(discover()))`,
    read off the firewall while it measures. Two mechanisms make those differ, and **nothing in the
    pipeline notices** (the apply succeeds, the post-apply verify re-plans and again sees no
    *changes*, and the read-before/read-after check sees no drift because the firewall genuinely
    never moved): **(1) an unappliable field** — `plan_apply` skips a pipe with no live match or no
    uuid and records only a *warning*, which every caller discards, so the firewall settles on a
    different profile; `settings_profile.unwritable_diffs` reports exactly those dropped
    differences (sharing `_match_live_pipes` with `plan_apply`, so the planner and the reachability
    check can't answer "which live pipe is this?" two ways), and `start_settings_test` **reverts**
    them so the fingerprint it returns is reachable, listing what it dropped in `warnings`.
    **(2) a field restated in another notation** — `_apply_writable_overrides` ran `coerce_value`
    over every supplied field ("5ms" → 5), which for an *unchanged* field is purely textual: no
    write is planned, the firewall keeps reporting "5ms", and the target hashes a profile that will
    never exist. It now skips any override already `_field_equal` to live, keeping the firewall's
    own representation. This was the dominant cause — because `full_overrides` supplies the parent's
    **whole** writable set, *every* explore test hashed an invented profile, so the runs were filed
    correctly and the recommendation pointed at a fingerprint with no history at all.
  - `explore_tracker.py` — **the recommendation ledger: was the data right?** Explore's output is
    a *prediction*, and a prediction nobody scores is a horoscope — it costs the same night of
    benchmarking either way. So the **claim is stored before the measurement exists**
    (`models.ExploreRecommendation`, written by `POST /explore/test`): what was proposed (parent,
    levers moved, settings applied), what the model asserted (`predicted` ± `uncertainty`, the
    `upside` it was ranked on, the `best_overall` it claimed to beat), and — the part that makes
    the ledger worth keeping — **what the prediction rested on** (`evidence`: a controlled matched
    pair, the parent's own neighbourhood, or a marginal curve already flagged confounded). The
    **verdict is derived, never stored** — recomputed from the measured field on every read
    (`recommendations()`, one indexed query for the rows + one batched `crown_follower.profile_overalls`,
    deliberately *not* a `compute_profiles` pass, so the page loads it immediately), exactly like
    every other score here: a re-grade or fresh runs move it instead of leaving a frozen answer.
    A claim is graded against **its own stated band** (`max(uncertainty, MIN_BAND)` — the model may
    be wrong by exactly as much as it said it might be; the floor stops a ±0.1 claim manufacturing
    a miss out of run-to-run noise) → `on_target` / `better` / `worse`, plus `pending` (nothing
    measured yet — never counted as a success) and **`incomparable`** (proposed under another
    methodology: a prediction is a number on one rubric's scale, so grading it under a different one
    compares two yardsticks — the same discipline the run-comparability gate applies). A thin
    measurement is graded but flagged `provisional`. Every row carries a **`why`** sentence joining
    the miss to the evidence class it was priced from, and the `summary` aggregates the same split
    **by evidence class** — which is how "that curve was confounded" turns from a caveat into a
    measured fact about the model's own failure mode (the claim `CONFOUNDED_SHRINK` was written on
    a hunch). The bucket is **weakest-link** (`evidence_kind`): a two-lever candidate priced one leg
    from a matched pair and the other from a confounded curve is only as trustworthy as the
    confounded leg. `GET /api/explore/recommendations`; rendered as the Explore page's
    **"Was the data right?"** card.
    **The ledger never trusts the recorded fingerprint** (`_measured_fingerprint`/`_reconcile`):
    every chunk of a profile test carries `job_group = "profile_test-<id>"`, and each run's
    `settings_fingerprint` is the very column `compute_profiles` groups profiles on — so resolving
    a recommendation's profile *through the runs it produced* makes the correlation right **by
    construction**, whatever the firewall did with the target. A row the benchmark contradicts is
    re-pointed at the profile that actually ran and stamped with a note saying so (persisted in its
    **own** `session_scope`, like `updates.verify_pending_updates` and `profile_names` — the request
    session comes from the read-only `get_session` dependency and closes without committing). This
    is the backstop behind the two source fixes above, and what rescues data already collected
    against an invented fingerprint.
    **One proposal is one row and one data point** (`_claim_key`/`_collapse`): testing the same
    candidate twice writes two claims — right as a record of what was run, wrong as calibration,
    since both resolve to the same profile and the same measurement, so counting them separately
    says the model was checked twice when it was checked once. Rows are collapsed by
    *(parent, levers moved, methodology, resolved profile)*, keeping the newest claim as the
    representative and carrying `attempts`/`attempt_ids`/`first_proposed_at` (the response also
    returns `attempts_recorded`, so nothing is hidden). The resolved profile is deliberately part
    of the identity: two attempts that landed on **different** profiles measured different things
    and stay separate — a disagreement worth seeing.
  - `crowning.py` — **the first-class CROWNING POLICY**: the single resolver for "which
    verdict governs what automation applies". `crown_follow.policy` = **"pooled"** (the
    all-time Overall argmax) or **"duel"** (the duel ladder's latest fresh decisive champion,
    `duel.latest_champion`, with pooled fallback). One policy, one write path: engines
    (race/duel) only measure and adjudicate; `crowning.resolve` selects; the **crown
    follower** is the only component that writes the firewall. The pooled crown *statistic*
    is always computed, tracked (churn ledger) and displayed regardless of policy. The GUI
    control is the top-bar **Follow best popover** ("Crowning policy" chips + both verdicts
    side by side); the API surface rides `GET/POST /settings/crown-follow` (`policy`,
    `policies`, `duel_champion`).
  - `refresh.py` — **Re-run profiles**: the batch sibling of `profile_test`. For
    each stored profile it applies the settings, benchmarks a **caller-chosen** number of
    iterations, then moves on — **restoring the baseline at the end** (persisted to a
    `ProfileRefresh` row; `reconcile_interrupted_refreshes` restores on startup). One bad
    profile is logged and skipped, not fatal. `refresh.preview` estimates duration
    (median per-iteration time × total iterations + per-profile overhead) so the UI can
    show "N profiles × M ≈ ~T" before committing. Own thread under the `coordinator` lock.
    Use it to collect fresh, comparable data after a methodology change quarantines
    history that can't supply a new crown metric. A **winner-first top-N** mode
    (`ranked_profiles`/`_select`; `start`/`preview` take `top`+`rank_by`) re-runs only the best
    performers first — ranked by their persisted Overall under the prior methodology (the
    `rank_by` version, defaulting to the most-recent non-current methodology) — so the profiles
    that were winning get fresh data before an arbitrary sweep of everything. `/api/settings/refresh`
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
    fallback): under **v13** that's **FCP × LCP × network_stall_all** (quickest first response ×
    perceptual "main content visible" × floor-free network-attributed dead-air, the SQM-movable
    resource-handoff gaps — render excluded, no perceptible floor, deliberately sub-perceptible to
    rank the *best* profile; v11/v12 used fcp/lcp/worst_void_fraction (0 for every profile on a fast
    link — inert), v10 fcp/lcp/stall_energy, v9 nav_response/byte_earliness/jank_fraction, v8
    fcp/lcp/stall_time, v7 fcp/lcp/total_stall, v6 fcp/total_stall/load_event, v5
    fcp/perceived_time/inp). It's an
    *intersection* (corner, not mean — one weak metric can't be averaged away), √k-normalized
    so corners of different arity share a scale.
    A profile's Overall is the **corner over its field-percentile-normalized raw crown
    measurements**, NOT the methodology grade (`compute_profiles` normalize pass,
    `_normalized_crown`). For each crown metric it takes the profile's **median raw value** (e.g.
    FCP in ms) and maps it to its **percentile within the field's distribution**
    (`_percentile_norm` / `_crown_field_values` — mid-rank empirical CDF, direction-aware), then
    corners those. **Percentile (rank) normalization gives every metric equal, uniform spread, so
    no single metric can dominate the corner** — the failure mode of a min/max rescale, where one
    fast/slow outlier compresses FCP/LCP and `stall_energy` (spread more evenly) steamrolls them.
    The scale is the measurements' *ranking*, so **re-grading a metric can't move the crown** —
    only re-measuring can (trade-off: it's magnitude-blind — a 1 ms edge and a 200 ms edge both
    mean "one rank better"). It stays **monotonic in the crown-metric
    columns** (which show each metric's normalized-raw standing, `crown_norm`): a profile faster
    on every crown metric necessarily has a higher Overall, so grading never overturns a raw
    dominance and the standings always explain the ranking. The **Overall IQR**
    (`overall_p25/p75`) is the corner over each metric's normalized p25/p75 raw quartile, so it
    brackets the point Overall; the **optimistic ceiling** (`optimistic`, drives heirs + the
    race) is the corner over each metric's best-case normalized raw (good-side quartile, or
    median + a small margin for a thin sample). The graded per-metric subscores
    (`crown_scores`) still power the axis scores + the custom-crown lens, and the per-Score
    `axis_scores["overall"]` (a per-run graded corner) stays for the "vs typical" baseline.
    Because the crown reads raw, the field-normalized Overall is **field-relative** (adding a
    profile re-normalizes the scale). No re-grade is needed — the raw values are already
    persisted; only the
    cross-run aggregation changed. A **custom crown** (`crown_metrics=` query param,
    `_apply_custom_crown`) corners over any caller-chosen subset of subscores as an
    exploratory `custom_overall` + `custom_best_fingerprint` — a what-if lens over the same
    persisted building blocks, leaving the canonical Overall untouched. The per-axis scores
    (Responsiveness/Smoothness/Speed; `_CORNER_AXES`) remain as display columns. It also
    aggregates per profile the median of every axis score *and* every metric we collect
    (`metrics.all_metric_sources`) to power the dynamic quadrant + table column selector.
    The crowned **"best"** is the **confident** profile (total iterations ≥
    `correlation.min_iterations`) with the **highest Overall** (the field-normalized raw corner
    above) — full stop, the profile that wins wins, even by an infinitesimal margin (`_select_crown`).
    The verdict is a deterministic argmax of that Overall (exact-tie break: more iterations,
    then most-recently-seen); there is **no hysteresis/stickiness and no steadiness override**.
    The Overall IQR
    (`overall_p25/p75`) does **not** decide the crown; it only *labels* a photo finish:
    `_clearly_better`/`co_leaders` flag every confident profile statistically
    indistinguishable from the crown. A profile clears the bar only when its median lead
    exceeds BOTH an absolute floor `correlation.crown_tie_min_margin` **and**
    `correlation.crown_tie_sigma` × the pooled **standard error of the medians**
    (`√(SE_a²+SE_b²)`, `SE = IQR/√n`; `_overall_se`). Because the SE shrinks as runs accrue,
    the bar **tightens with sample size** — so collecting data can *break* a tie two
    heavily-sampled profiles would otherwise be stuck in (the old `crown_tie_iqr_fraction`
    scaled the *raw* IQR, which ignored n, so more runs never separated anything). Returned
    **purely as information** so the UI can show a "tied" chip without changing who's crowned;
    the response also carries `crown_confidence` (the crown's Overall ± SE, the gap to the
    runner-up, the σ·pooled-SE significance threshold, and whether the lead clears it) so the
    Profile-Detail Standings card shows the measured signal-vs-noise, not an adjective. No
    posterior, no variance penalty, and no time window enters the verdict — it pools ALL
    history (weather averages out over the pool; a brief experiment ranking on a rolling
    last-100 window thrashed, because ~100 iterations span only a few days ≈ one weather
    regime per profile, so windowed rankings compared different profiles' different weather
    head-on). The window survives as the **"Overall (recent)" drift-lens column**
    (`overall_recent` + `recent_iterations`/`recent_scores`, sized by
    `correlation.crown_window_iterations`, default 100; 0 disables): the same crown grade
    recomputed over only each profile's most recent iterations — read it against the
    all-time Overall to see drift, against "vs weather" to judge conditions. Returns
    `best_fingerprint` (+ `co_leaders` + `crown_confidence`). Each profile also carries a
    **current-form check**
    (`routes_settings._profile_form`, both directions): its last-`FORM_RECENT_RUNS` median
    Overall vs its prior record, significance-gated by the same σ·pooled-IQR/√n machinery —
    **"fading"** (pooled Overall propped by a past it no longer delivers) / **"rising"**
    (pooled Overall understates its present), rendered as row chips; response-level
    **`crown_fading`** raises the "the crown's bar may be a ghost" alert when the crown
    itself fades. Flag-and-steer only — the recourse is re-measurement (race / re-run
    top-N), never re-weighting. The
    challenger race reads `compute_profiles` and its bar is `best_fingerprint`'s Overall.
    **Finding challengers that could overtake the crown is a separate,
    smarter job** — the **Heirs to the crown** card + the challenger race rank under-sampled
    / stale profiles by their *optimistic ceiling* (`optimistic_overall`, the crown corner
    over each crown metric's p75 upper estimate) against the crown's Overall, to decide where
    to spend iterations to confirm or deny an heir. The **vs-typical** (`relative_overall`)
    delta is kept as an informational column (and a hook for smarter heir-hunting), not a
    crown input.
  - `profile_names.py` — **call signs for profiles** ("Speedy Sloth", not `q1514 t5ms`). A
    fingerprint identifies a profile and `summarize()` describes it, but neither is scannable:
    150 profiles differing in one number are a wall of near-identical strings, and a duel
    between two of them is unnarratable. Names are **deterministically derived** from the
    fingerprint (blake2b seed → ~500 adjectives × ~500 nouns, alliterative *by preference*),
    **persisted** in `ProfileName`, and **unique by construction** (a taken name is probed past;
    the candidate stream ends in a fingerprint-suffixed name that cannot collide). Deliberately
    not AI-generated: a name must be stable (never re-rolled behind the user), offline (no key,
    no per-profile call), and unique — which a sampling model can't guarantee. Assignment always
    opens its **own** `session_scope`, because request sessions come from the read-only
    `get_session` dependency that closes without committing (a name written there would evaporate
    and the next request would re-derive it against a different taken-set). `names_for` is the
    bulk accessor every list view uses (one query, not N). SQM-off gets the fixed name **"No
    Shaper"** — the control group is the one profile a whimsical name would obscure. Users can
    override any name via `PUT /api/settings/profiles/{fp}/name` (the pencil on Profile Detail);
    uniqueness is enforced. Every view leads with `name` and keeps the technical `label` beside
    it; duel matchups record both, and the standings/tape re-resolve names **by fingerprint** so
    bouts fought before naming (or before a rename) read under today's names.
  - `timezones.py` — **the one place a stored IANA zone name becomes a `tzinfo`.** A
    schedule's hour/minute mean the *user's* wall clock, so each schedule stores the browser
    zone it was saved from — which only resolves if the system has an IANA tz database. A slim
    container may not, and then `zoneinfo` raises for **every** name: that surfaced as
    "unknown timezone 'America/Chicago' (use an IANA name)" — a 422 that blocked every save on
    the Dueling Champions page, blaming the input for a missing OS package. So
    `validate_timezone` rejects a name only when the system *can* resolve names and this one
    isn't among them (no database → accept a well-formed name and warn; a saveable schedule
    that falls back to container-local beats an unsaveable one), and `schedule_zone` never
    raises — an unresolvable zone degrades to container-local instead of killing a scheduler
    tick. The backend now depends on the `tzdata` package, so a normal install *has* the
    database and the fallback is a safety net rather than the usual path. `routes_baseline`,
    `routes_duel`, and `scheduler` all go through it.
  - `database.py` — engine/session + additive SQLite `_migrate()` (ALTER for new
    columns; `create_all` for new tables).
  - `api/` — REST routers mounted at `/api`.
- `frontend/` — React + TS + Vite + MUI dashboard (dark mode). **Every route is code-split**
  (`App.tsx`: `lazy` + `Suspense`): the app was one 1.2 MB chunk, so opening any page first
  parsed every other page plus recharts — over a second of blank screen on a phone before a
  single request was sent. Now the shell paints immediately, each page is its own small chunk
  (Duels ~40 kB), and the 384 kB chart bundle loads only for the three views that draw charts.
  Keep new pages lazy. Pages: Dashboard,
  History, Trends, Compare, Settings Impact (**paginated** sortable table — 25/page —
  with standard **Overall + the crown metrics** columns (the metrics the Overall corners over,
  from the response's `overall_metrics` — fcp/lcp/network_stall_all under v13 — ranked by each metric's
  **field-normalized raw** value via a `crown:<metric>` field key → `crown_norm` (no grading),
  so the pinned columns are the raw measurements that actually *compute* Overall; the headline
  axes Responsiveness/Smoothness/Speed are a different graded decomposition, demoted to opt-in)
  plus a **"% vs SQM off"** column (`pct_vs_sqm_off`, server-computed in `compute_profiles`):
  each profile's Overall improvement over the honest unshaped baseline — the best Overall among
  measured "SQM off" profiles (`is_sqm_off`; response `sqm_off_overall`) — green when shaping
  helps, red when the profile is *worse* than turning SQM off. It's derived straight from the
  methodology's Overall, so it re-derives when the methodology changes (no separate knob). A
  **"Hide profiles worse than SQM off"** checkbox (on by default; inert until a baseline exists)
  drops every profile with `pct_vs_sqm_off < 0` from the table + scatter — dead weight we don't
  care about. (This replaced the old "vs weather" column.) Plus an optional
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
  Experiments, Shotgun Sweep, **Explore** (`Explore.tsx`, `/api/explore/landscape` — the
  what-haven't-we-tried view: the ranked **next profiles to test** with a one-click **"Test now"**
  (5 iterations — the cheap "did this go anywhere?" answer) beside "Test to minimum" (the full
  confidence top-up), the **"Was the data right?"** ledger (`/api/explore/recommendations`) grading
  every past recommendation against what the link actually did — predicted ± band vs measured, a
  sentence on *why* it missed, and the calibration split **by evidence class** (do matched pairs
  predict better than confounded curves? on your link, measured), a response curve per lever per pipe (marginal solid + reference-conditioned dashed,
  with a **confounded** chip where the two disagree), **"What changing one lever actually did"**
  (the matched-pair contrasts), **local optima** (coupled-basin detection), the holes in
  coverage, and the lever pairs that genuinely interact; fetched on demand, read-only apart
  from the test button. **The long sections grow with the field, so they page**: a lever with
  twenty tested values has 190 possible one-lever transitions and a 150-profile field holds
  dozens of local optima — printed whole that's a wall nobody reads, which is the same as not
  reporting it. Matched pairs are **flattened across levers** (grouped, the strongest finding in
  the field can sit halfway down the fourth group) and default-sorted by **evidence** — several
  agreeing pairs first, then pair count, then effect size — so a dramatic single-pair anecdote
  can't lead. Local optima default to best-Overall-first and are sortable by *siblings beaten*;
  the coupling alarm counts only **well-surrounded** optima (≥2 measured siblings), because
  "39 of 40" from a sparse field is noise wearing a warning's clothes. Lever charts lead with
  the levers that actually spread the Overall, with the flat ones behind a **show all** toggle),
  **Dueling Champions** (the duel ladder's own view — the
  *controlled-trial* counterpart to Settings Impact's observational standings, so it speaks in
  fight-card terms rather than means: the reigning champion + reign length + which crowning
  policy is live, the **ladder standings** league table (`GET /duel/standings`), the
  **head-to-head grid** over the top of the table, the **bout tape** of every matchup with its
  pair scoreline / margin / what ended it, and **rules of the ring** — the nightly window plus
  the sequential stopping rule (min/max pairs, practical margin, rematch cooldown) editable
  from the page. Settings Impact keeps only a one-line **Duel ladder** pointer strip so the two
  rankings don't duplicate controls; `Duels.tsx`, `/api/duel/*`), **Baseline (SQM off)** (the "Test baseline behavior" tab: arm the
  nightly schedule — time/iterations/settle all configurable — or run one on demand, with a live
  stage readout; `Baseline.tsx`, `/api/baseline/*`), Config, Methodology, Plugins, Data Dump, AI,
  Run Detail. A
  top-right **jobs dropdown** (`JobStatus`) shows every running/recent background job
  (re-grade, sweep, run, profile test, challenger race, …); next to it the top-bar
  **"Follow best" switch** (`FollowBest.tsx`) arms the crown follower
  (`crown_follower.py`) and opens a popover with the current crown, whether the firewall
  is on it, the crown-churn stats, the recent crown-change ledger, and a "Check now". The **Data Dump** page has two
  exports: the raw run dump (`/api/history/dump`) and the **AI optimizer export**
  (`GET /api/settings/export/optimizer`, `build_optimizer_export`) — a profile-centric JSON of
  each profile's **full details** (complete shaper settings + first/last seen) **and scoring
  data** (percentile Overall + IQR, per-crown-metric percentile, axis scores, raw metric medians,
  and per-run raw scoring metrics), plus the methodology objective (crown metrics + lower-is-better
  + observed best/worst) and the shaper field model (writable + sweepable fields + ranges). It also
  carries a deterministic **`analysis.field_sensitivity`** block (`_field_sensitivity`): for each
  writable lever **per pipe label** × each crown metric, the Spearman rank correlation across the
  exported profiles (one (field value, profile-median metric) point per profile), with
  `metric_direction` (does the metric rise/fall as the field rises) + `effect` (improves/worsens the
  crown). This is the settings→outcome relationship map computed *server-side* — trustworthy and
  chartable regardless of the model — handed to the LLM so it reasons over an explicit "this up →
  that down" map instead of eyeballing raw rows. Each lever is also correlated against the
  **Overall** itself (the rank-corner we crown on), since a lever can move the Overall while barely
  correlating with any single raw metric. They're **marginal** (profiles vary several fields
  at once → possibly confounded), not partial. It **also** carries a deterministic
  **`analysis.top_profile_signature`** block (`_lever_signature`): for each writable lever, what the
  **top-Overall quartile** of profiles runs vs the whole field — `pattern` (higher/lower/`sweet_spot`
  /none), `top_value`+`top_range` (the value the winners share), `field_range`, plus shift /
  concentration / Cliff's delta. This answers what the correlations **can't**: when every ρ≈0 the
  winners can still cluster on a specific value (a sweet spot both extremes miss) or run a lever
  systematically higher/lower — a combination/non-monotone edge a single-lever correlation is
  blind to (rendered as a **"What the top profiles share"** card on the AI page). It **also**
  carries **`analysis.coverage_gaps`** (`_coverage_gaps`): levers with a **promising but
  under-sampled** signal — a directional pattern or suggestive ρ, but too few distinct values
  measured (or the favored direction runs off the edge of what's been tested). Each is a concrete
  **data request** (`suggested_values`, `action` extend_lower/extend_higher/resolve, `sweepable`)
  so the model can **kick back "go measure here" instead of a speculative profile** (the AI returns
  these as `data_requests`; rendered as a **"What to measure next"** card linking to the Shotgun
  Sweep). This is the active-experiment layer: a signal is only actionable once resolved.
  `interval` is now a **sweepable** field so the most common recommendation (sweep CoDel interval)
  is directly runnable. The prompt also forbids the model from inventing statistics (only cite ρ /
  medians present in the JSON — a lever with too few distinct values has no `field_sensitivity` row
  and must be described from `top_profile_signature`). Bounded
  by `runs_per_profile` and `profile_limit` (top-N by Overall). The **AI** page (`ai.py`,
  `routes_ai.py`) sends that export to an LLM via **OpenRouter** and shows proposed new profiles:
  the API key lives in its own `AppConfig` `"ai"` row (isolated from the benchmark config so it
  never leaks into run snapshots / the data dump; returned **masked** via `ai.public_config`), the
  model + editable prompt are saved there too. `GET/PUT /api/ai/config`, `DELETE /api/ai/config/key`,
  `GET /api/ai/models`, `POST /api/ai/suggest` (builds the export, calls OpenRouter chat-completions,
  best-effort parses `{relationships:[…], suggestions:[{settings, displacement_likelihood, rationale}]}`,
  **ranked by the model's crown-displacement estimate**). The prompt now runs a **two-step** interp:
  the model FIRST returns `relationships` — its read of how each lever moves each crown metric
  (`inverse`/`linear`/`none` + confidence + evidence), grounded in `analysis.field_sensitivity` —
  THEN proposes suggestions consistent with them. The AI page renders a **"Settings ↔ outcome
  relationships"** card: the deterministic `field_sensitivity` table (direction + improves/worsens
  chips, echoed on `/ai/suggest` and the stream `meta` event) plus the model's own interpretation.
  A **streaming** variant `POST /api/ai/suggest/stream`
  (`ai.suggest_stream` + `_stream_chat`) returns Server-Sent Events — a `meta` event (with
  `field_sensitivity`) then
  `reasoning`/`content` token deltas then a terminal `done` (parsed suggestions + relationships) or `error` — so a
  long request keeps the connection alive (no timeout) and the AI page shows the model's reasoning +
  answer live (default on, `Stream` toggle; `client.aiSuggestStream` consumes the SSE via `fetch` +
  `ReadableStream`). Config secrets are resolved before the generator starts, so it's session-free.
  Each suggestion has a **one-click "Test to minimum"**:
  `POST /api/settings/test-settings` (`_apply_writable_overrides` + `TestSettings`) materializes the
  suggestion onto the **live** profile — overriding **only writable fields** so it's always reachable
  — then runs a normal profile test (apply → benchmark to `min_iterations` → restore baseline). No
  firewall write happens for an unreachable or no-op suggestion (rejected up front). Each override
  value is run through `shaper_fields.coerce_value` so an AI's `"5ms"`/`5`/`"5"` all become the
  firewall's **bare-number** wire form (CoDel `target`/`interval` are option-keyed selects keyed by
  the bare number — writing `"5ms"` doesn't take). The optimizer export tells the model the exact
  per-field format up front (`value_format` + a real `example` per shaper field, pulled live).
  Each suggestion also has a one-click **Apply** — `POST /api/settings/apply-settings` writes the
  suggestion to the firewall **permanently** (one-way, no restore; the arbitrary-settings sibling of
  `apply-profile`): overlays only writable fields onto live, `preview` returns the exact planned
  writes for the shared **`ApplyConfirmDialog`** (the same confirm-diff UI as Settings-Impact "Apply
  this profile"), commit applies via `provider.apply()` + kicks a 1-iteration benchmark. Rejects a
  no-op / unreachable change.
- `Dockerfile` (Playwright base image) / `docker-compose.yml` +
  `docker-compose.ghcr.yml` — single-container deploy (API serves UI). CI publishes
  `ghcr.io/jmorganthall/pathbrain:latest` via `.github/workflows/docker-publish.yml`,
  stamping the build commit (`--build-arg GIT_SHA=$github.sha` → `PATHBRAIN_GIT_SHA`).
- **Version awareness** (`updates.py`, `GET /api/version`): a cached, best-effort
  compare of this build's `git_sha` against the latest commit on `update_repo`'s
  default branch (GitHub API; on by default, `PATHBRAIN_UPDATE_CHECK=false` to disable).
  The top-bar `UpdateChip` shows "Update available" (→ the GitHub compare) when the
  branch has moved past the running build — i.e. a newer `:latest` image is pullable.
  To keep "up to date" from being a black box, `version_info` also returns `update_repo`/
  `update_branch`/`checked_at`, and the footer renders the full comparison (running SHA · latest
  on repo@branch · when last checked) in a tooltip plus a **"check now"** link
  (`POST /api/version/refresh`, `version_info(force=True)`) that bypasses the 1-hour cache — so a
  stale cached answer can be corrected on demand instead of waiting out the TTL. (Note: the check
  is *commit*-based against GitHub, which can briefly disagree with the *image* actually published
  to GHCR; a registry-digest check would track images exactly.)
- **One-click self-update via Watchtower** (`updates.trigger_update`, `POST /api/update/trigger`):
  when `WATCHTOWER_URL` (+ optional `WATCHTOWER_TOKEN`) is set — unprefixed, not `PATHBRAIN_*`
  (a per-field `validation_alias` in `config.py` bypasses the class-wide prefix) — the
  `UpdateChip` gains an **"Update now"** button (gated on `version_info()["self_update"]`) that
  POSTs to `{url}/v1/update` with a `Bearer` token — Watchtower's HTTP API — telling it to pull the
  newer image and recreate this container. Because a *successful* update severs the response as the
  container is recreated, a dropped/reset/timed-out connection is reported as **triggered** (the
  frontend then polls `/api/version` until the backend returns on a new `git_sha` and hard-reloads);
  a **refused** connection (Watchtower not listening) or an **auth** error (bad token → HTTP 401) is
  a real failure surfaced to the user. Endpoint returns `409` when unconfigured, `502` when
  unreachable/rejected. Both env vars live in `config.py` (infra settings) + the compose files +
  `.env.example`; empty URL (default) leaves the chip a plain link. The **Plugins page** carries a
  **Watchtower integration card** (`WatchtowerIntegration`) showing configured/URL/token state
  (`GET /api/update/config`, `self_update_config` — no network) with a **"Test connection"** button
  (`POST /api/update/test`, `test_update_connection`) that probes reachability **without triggering
  an update** — it hits the API **root**, never `/v1/update` (Watchtower's only endpoint *performs*
  the update), so any HTTP response = reachable and only a connection-level failure = unreachable;
  the token is verified for real only by "Update now".
  **The self-update ledger** (`models.UpdateAttempt`, `updates._record_attempt`/`_finish_attempt`/
  `verify_pending_updates`/`update_log`, `GET /api/update/log`): self-update is the one operation
  that **destroys its own evidence** — a successful update recreates the container mid-response, so
  the request is indistinguishable from a dropped connection, in-memory state is wiped, and the
  container log the user would grep is replaced along with it. "It never seems to work" was
  therefore unfalsifiable: `triggered: true` only ever meant *Watchtower took the call*, never
  *the build changed*, and a Watchtower whose scope excludes this container answers **HTTP 200 and
  updates nothing**. So every attempt is persisted to `update_attempts` **before** the request goes
  out (url, token-sent, and crucially `git_sha_before`), completed with how the call went
  (`outcome` = accepted / dropped / rejected / unreachable / not_configured + status, body, elapsed),
  and **resolved afterwards across the restart** by `verify_pending_updates()` — called at startup
  (the moment an update would have landed) and on every log read — which compares the running build
  against `git_sha_before`: **confirmed** (build changed), **no_change** (accepted, but still the
  same build after `VERIFY_AFTER_SECONDS` — detail names the usual causes: container outside
  Watchtower's `--scope`/label filter, image already current, registry unreachable), or **failed**
  (never got out). Rendered as the **"Update history"** section of the Plugins card. The chip's
  post-trigger poll also stops guessing: after `UPDATE_POLL_MS` (4 min) with no new build it reports
  the no-show and points at the ledger instead of spinning forever.

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
  2. **No frontend edit needed.** The Settings-Impact view is fully crown-driven off the
     profiles response's `overall_metrics` (the methodology's `overall` spec, exposed by the
     API): the pinned **standings columns**, the **quadrant default axes** (X/Y/Shade =
     crown[0]/[1]/[2], until the user manually picks an axis), and the **scatter dot-selection
     panel's** per-metric breakdown all read that one set, so a crown change (new methodology)
     re-wires the whole view automatically with zero `Settings.tsx` edits. Keep it that way —
     don't hardcode a crown metric key in the frontend.
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
- **One universal `required` field (Overall == Crown == required).** A methodology's
  required set is the *single* `methodology.required_metric_keys(definition)` accessor —
  *(metrics flagged `required`) ∪ (the Overall/crown `required` set)* — and nothing
  re-derives it ad hoc. `build_definition_from_spec` **materializes** `required: true` onto
  every crown metric in the frozen snapshot (so the definition self-describes), and an
  import-time invariant refuses a methodology whose crown-`required` metric isn't actually
  scored (the "valid but unscorable Overall" trap). `comparability()`, `summarize()`
  (`required_metrics`, what the Methodology page shows), and `serialize()` (per-metric
  chips) all read the one accessor — so the page can no longer under-report the crown as
  required, and the re-grade enforces exactly what's displayed.
- **Comparability is tied to crownability.** `methodology.comparability()` flags a run
  `incomparable` when its raw can't supply a required metric (`required_metric_keys` — i.e.
  any flagged metric **or** the current methodology's crown metrics) — so a run that can't
  produce the headline Overall (e.g. a pre-v6 run with no `total_stall`) is quarantined,
  never silently scored without the metrics that define the score. A re-grade reports the
  `exact`/`partial`/`incomparable` split (surfaced in the job summary). Every scored view
  filters through the **single central predicate** `methodology.is_comparable(score)`
  (`routes_settings._comparable` delegates to it; rolling/axis-series/trends/history/
  smoothness-compare all gate on current-methodology comparability, **not** the static
  metric marker) — so an incomparable run can't leak a headline number into a view that
  forgot the filter. This auto-adapts to every future methodology, so adding a crown metric
  can't silently leave stale-but-valid-looking scores. (`marks_latest`/`has_latest_metrics`
  is the separate, static at-measure legacy marker — still `longest_stall` — used only for
  the per-run Run-Detail "legacy" badge, not for gating scored aggregations.)
- **Unmeasurable ≠ a sentinel value — the interpret layer must omit, not fabricate.** The
  comparability gate only quarantines on an **absent** required metric (`mv.get(k) is None`),
  so the *whole* guarantee rests on the `interpret` layer emitting **nothing** for a metric it
  can't genuinely compute — never a default like `0`. A metric fabricated as a "perfect" value
  for a run that couldn't measure it slips past the gate and, worse, out-ranks real measurements
  (the crown's lower-is-better legs treat `0` as best). The concrete bug this rule was written
  for: `network_stall_all` (v13 crown leg) needs LoAF/longtask provenance to split network- vs
  render-attributed dead-air; a pre-instrument run has `loaf_source is None`, so the split is
  unmeasurable — `stall_attribution_times` degenerated `network_ms` to `0`, handing those runs a
  perfect smoothness leg. They ranked #1 until fresh, attributable runs arrived and dragged the
  crowned profile down the standings (the "best drops to 65th over time" report). Fix (derive-v14):
  `smoothness_metrics` omits `network_stall_ms`/`render_stall_ms`/`network_stall_all_ms` when
  `loaf_source is None`, so those runs are quarantined `incomparable` instead. Two import-run tests
  enshrine the guarantee: `test_every_current_crown_metric_gates_comparability` (dropping *any*
  current crown metric → `incomparable`) and `test_unmeasurable_crown_metric_is_quarantined` (a
  no-LoAF browser raw derives *without* the crown leg → quarantined end-to-end). **When adding a
  crown/required metric, its derive function must return `None`/omit on absent input** — the tests
  will fail if it fabricates. After a change like this, re-derive (drop the bogus values from raw)
  then re-grade (re-quarantine), then optionally **Re-run top-N profiles** (Settings → Re-run
  profiles, winner-first `top`+`rank_by`) to collect fresh comparable data on the best performers.
- **Data-integrity audit (recipe vs. ingredients).** `GET /api/runs/{id}/verify-derivation`
  (`runner.verify_run_derivation`) and `GET /api/settings/profiles/{fp}/verify-derivation` are
  **read-only** audits that answer "are we keeping the same data the same?" without changing any
  score. The **recipe** check re-derives every metric from a run's immutable raw and diffs against
  the stored value — a mismatch means a *stale-formula* value (derived under an older
  `DERIVATION_VERSION`, never re-derived); the profile endpoint samples the oldest + newest runs and
  flags `stale_history` when old drifts while new is clean. The **ingredients** check
  (`runner.browser_collection_shape`/`compare_collection_shapes`) compares what the raw actually
  *captured* across cohorts — URL set, LoAF coverage + sources, per-URL median resource count — so a
  faithful recipe applied to *different ingredients* (the browser navigating a changed URL set,
  LoAF added mid-history, page composition shifting) is caught even though each run still reproduces
  from its own raw. Surfaced as the **"Data integrity"** card on the Profile Detail page ("Verify old
  vs new"). This is diagnosis, not a scoring change.
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
- **Phase 7 (done):** **first-class Overall + crown intelligence.** Methodology
  `speed-smoothness-v5` made the Overall a first-class, versioned, persisted quantity;
  **v6** decomposed the crown to FCP × total_stall × load_event (dropping the uncalibrated
  `perceived_time`); **v7** swapped the completion leg `load_event → lcp` so the crown is
  **FCP × LCP × total_stall** — three independent dimensions (start / main-content-visible /
  fill-steadiness) rather than two correlated paint milestones + technical completion; **v8**
  swapped the stall leg `total_stall → stall_time` — the *relative* dead-air (excess over each
  run's own median pace, an average baked into the metric) for the *absolute* dead-air (summed
  duration of every gap over a fixed 200ms threshold, an actual per-run measurement like FCP/LCP),
  so profiles compare on measured values, not averages-of-averages. The
  crown is the **highest Overall among confident profiles** (the Bayesian/Thompson
  probability-of-best layer was removed — it over-credited thin, high-variance profiles for
  their upper tail; selecting *where to run next* is the separate hunting job). Settings
  Impact gained the **"Heirs to the crown"** card (reachable contenders by optimistic
  ceiling), a **saturation check** with a one-click **GUI re-anchor** (`/api/methodologies/
  reanchor`), and **"Re-run all profiles"** (`refresh.py`). The SQM field model was unified
  into the **`shaper_fields` registry** (identity/writable/sweepable derive from one
  declaration; executable invariants) — fixing the challenger's "unreachable profile" abort
  (`environment_signature` reachability) and making the sweep/experiment engines **and the
  Shotgun Sweep UI** registry-driven. Run-perf pass: per-plugin iteration caps, reused
  Chromium, bounded networkidle, screenshot/HAR off by default.
- **Phase 8 (done):** **absolute stall measurement.** Methodology `speed-smoothness-v8`
  replaces the *relative* `total_stall` (cumulative excess over each run's **own median** gap —
  an average baked into the metric, so cross-profile comparison compared deviations-from-own-
  baseline) with the *absolute* `stall_time` (`interpret/smoothness.stall_time`: summed duration
  of every completion gap over a fixed 200ms perceptible-stall threshold) as the Smoothness
  scored-stall metric and the crown's stall leg. Like FCP/LCP, `stall_time` is an actual per-run
  measurement against a fixed yardstick, so Settings-Impact compares profiles on measured
  dead-air. `derive-v5` adds `stall_time_ms` (purely additive → history re-grades from raw;
  `compute_profiles` sources the crown's raw values from the re-graded `Score.metric_values` when
  the plugin cache predates the metric). `total_stall` stays a display-only diagnostic. **"Re-run
  profiles"** gained a **winner-first top-N** mode (`refresh.ranked_profiles` / `_select`): after
  a publish, re-run only the best performers first, ranked by their Overall under the prior
  methodology, instead of an arbitrary sweep.
- **Phase 9 (done):** **bronze-layer completeness + first-principles crown.** Added the
  **navigation waterfall** (`interpret/waterfall.py`: the load's independent phases —
  nav_dns/tcp/tls/request/response/render from Navigation Timing marks, surfaced as a
  left-to-right waterfall on Dashboard + Run Detail) so the network setup chain baked into
  FCP/LCP is visible; the **metric ledger** (`metrics.METRIC_ROLES`, roles W/N/C/S/O +
  `RANKABLE_ROLES`) that keeps weather instruments and opaque milestones out of *automatic*
  ranking; fixed **`jank_fraction`** (was 0 everywhere — the smoothness instrument counted
  post-load background resources; `resources_within_load` now bounds the whole instrument to
  `loadEventEnd`, derive-v9). Methodology **v9** (short-lived) chased shaper-movability
  (nav_response/byte_earliness/jank_fraction); **v10** reverts to first principles —
  **FCP × LCP × stall_energy** (`√Σgap²`, the L2 magnitude of the in-load gaps = worst hang +
  accumulation in one threshold-free number), the three things a human directly experiences
  (fastest initial progress × fastest load × smoothest fill), *not* what the shaper can move.
  `stall_energy` takes the Smoothness scored-stall slot (`stall_time` → display-only);
  derive-v10 is purely additive. Re-anchored the **DNS `best` threshold** 10ms → 0.5ms (a
  sub-ms local resolver saturated the old 10ms; a Completion diagnostic only). Re-grade + re-
  derive were sped up (skip-if-current filter + batched savepoint commits) and each got its
  own Methodology-page button with a tooltip explaining bronze/silver/gold.
- **Phase 10 (done):** **the FCP→LCP journey crown.** Methodology `speed-smoothness-v11` refines
  the crown's smoothness leg from the absolute `stall_energy` to `worst_void_fraction`
  (`interpret/smoothness.worst_void_fraction`) — the **"pregnant pause" index**: the single
  longest void between resource completions **within the FCP→LCP window**, as a fraction of that
  window. The felt difference between two profiles with identical fast FCP and LCP is the *shape*
  of the journey between them — steady consistent progress vs FCP → dead pause → lurch to LCP.
  `stall_energy` missed this two ways: it spanned past LCP (punishing a post-content tail the user
  never felt) and, being absolute ms, correlated with LCP — double-counting a slow load's freeze
  on both the LCP and smoothness legs. `worst_void_fraction` is **scale-free**, so it measures
  *only* the evenness of the fill, decoupled from how long the journey took (LCP's job) — making
  the three crown legs genuinely independent (when it starts × when it's done × how steady the
  trip was). Crown = FCP × LCP × worst_void_fraction; `stall_energy` → display-only. `derive-v11`
  adds `worst_void_fraction` (purely additive → history re-grades straight from raw). Re-grade
  history + re-check crownings against felt experience to validate.
- **Phase 11 (done):** **widen the pregnant pause to the whole load + threshold re-anchors.**
  Methodology `speed-smoothness-v12`. Fast-link measurements showed `worst_void_fraction` reading
  **0 for nearly every profile** — an inert crown leg — because FCP→LCP is near-instant on a fast
  link (FCP ~307ms, LCP ~348ms), so the felt dead-air is in the *post-LCP settle*, which the
  FCP→LCP window excluded. `derive-v12` widens the metric's window `FCP→LCP → FCP→loadEventEnd`
  (same crown metric key). This reverts only v11's *window* decision, not the *form*: the metric
  stays a scale-free fraction, so — unlike v10's absolute `stall_energy` (√Σgap² ms) — it still
  doesn't correlate with the load duration or double-count the freeze on the LCP + smoothness legs.
  The `resources_within_load` bound still excludes the post-load background trickle. Plus two
  saturated `best` re-anchors surfaced by the Settings-Impact saturation check: DNS 1.0 → 0.8ms
  (91% saturated) and page-load 800 → 556.2ms (100% saturated) — both secondary axis metrics, so
  they sharpen their subscores + clear the warnings without moving the Overall. `derive-v12`
  *changes* `worst_void_fraction`'s value (a formula change, not additive), so **re-derive from raw
  first, then re-grade**.
- **Phase 12 (done):** **floor-free network-attributed stall crown + methodology GUI.** Fast-link
  measurements confirmed (via the new **"Where's the pause?"** Run-Detail diagnostic —
  `interpret/smoothness.longest_void_diagnostic`, per-URL longest void + phase + network/render
  attribution) that on fiber the resource waterfall is gated by **round trips**, not bandwidth: the
  voids are sub-perceptible (<200ms) and part render-bound. So `worst_void_fraction` (200ms floor)
  read **0 for every profile** — inert. Methodology `speed-smoothness-v13` swaps the crown's
  smoothness leg to **`network_stall_all`** — network-attributed dead-air with the minimum-gap floor
  dropped to 0 (`stall_attribution_times(..., min_stall_ms=0)`), isolating the SQM-movable
  resource-handoff gaps (render excluded via LoAF overlap), **deliberately sub-perceptible** to rank
  the best profile rather than gate on human-noticeable stalls. Crown = FCP × LCP ×
  network_stall_all; worst_void_fraction → display-only. derive-v13 is purely additive (re-grades
  from raw). Also: the **Settings-Impact standings** now render **"no signal"** (not a misleading
  "#1 for all") for a crown metric with zero spread (`rankByMetric.inert`); the **Methodology page**
  gained an **"Active methodology"** card to switch/adopt/clear the `methodology_version` pin from
  the GUI (`POST /api/methodologies/set-current`) — no API poke — plus a subtle build-version footer
  and centralized stored-raw access (`raw_access.py`).
- **Phase 13 (done):** **close the "unmeasurable = perfect" comparability leak.** A crowned
  profile kept dropping in the standings over time (in one case to 65th) even with hundreds of
  runs — historical measurements scored radically better than fresh ones. Root cause: the v13
  crown leg `network_stall_all` was fabricated as a *perfect* `0` for any run without LoAF/longtask
  provenance (`loaf_source is None` → `stall_attribution_times` routes all gap time to `unknown`
  and returns `network_ms=0`). Pre-instrument history rode that bogus 0 to #1 until fresh,
  attributable runs arrived and dragged the applied profile down. **derive-v14** stops synthesizing
  the network/render attribution metrics when provenance is absent — `smoothness_metrics` omits
  `network_stall_ms`/`render_stall_ms`/`network_stall_all_ms`, so `comparability` quarantines those
  runs `incomparable` and `compute_profiles` drops them from crown ranking (the correct behavior:
  a run that can't compute the crown metric is comparable to nothing). The crown still pools across
  **all** times (comparing profiles across every scenario is the point — deliberately no
  recency-window / weather de-confound). Enshrined the guarantee against recurrence with two tests
  (`test_every_current_crown_metric_gates_comparability`, `test_unmeasurable_crown_metric_is_quarantined`)
  and the **"unmeasurable ≠ a sentinel value"** convention: any crown/required metric's derive
  function must omit on absent input, never default. Post-change workflow: re-derive → re-grade →
  **Re-run top-N profiles** (the existing winner-first `refresh` with `top`+`rank_by`) to rebuild
  fresh comparable data on the best performers after old runs quarantine.
- **Phase 14 (done):** **magnitude-aware crown + measured signal-vs-noise.** The crown-lead-vs-noise
  readout exposed that the field-percentile **corner** was un-crownable on a fast link: ~149 profiles
  packed into a few ms, so a sub-ms per-run wobble crosses dozens of profiles and the normalized
  Overall carried a **±17-point SE** — the top ~66 were a statistical tie no amount of runs could
  separate (SE shrinks only as √n; the noise is *manufactured* by percentile ranking, not present in
  the raw ms). Two changes. **(1) Sample-size-aware ties** (methodology unchanged): `_clearly_better`
  now uses the **standard error of the median** (`IQR/√n`, pooled) × `crown_tie_sigma` (2.0) instead
  of raw IQR — so collecting runs can *break* a tie (`crown_tie_iqr_fraction` → `crown_tie_sigma`);
  `crown_confidence` in the profiles response surfaces the crown Overall ± SE, the gap to the
  runner-up, and the σ·pooled-SE threshold on the Profile-Detail standings. **(2) Methodology
  `speed-smoothness-v15`** crowns by a **weighted average of the perception-calibrated subscores**
  (`overall.method: "weighted"`, `weights` in the spec) instead of the percentile corner —
  **FCP 1 · LCP 1 · network_stall_all 0.5** (fastest-to-first and fastest-to-main-content even,
  smoothness secondary). Magnitude-aware (a 5 ms and a 500 ms edge scaled by human perceptibility)
  and low-noise, so a profile's median Overall is pinned to ~±1 and the field actually separates.
  It's **additive, not an intersection** — a strong FCP/LCP is no longer vetoed by a mediocre stall
  leg (the corner's behavior). Same metrics/derivation as v14 → history **re-grades from cached
  scalars** (no re-derive). Fully methodology-driven: `overall_from_definition`/`compute_profiles`
  read `overall_method`/`overall_weights` from the spec, and the per-metric **percentile standings
  columns stay for display** — a future methodology changes the method/weights in one place and the
  wiring re-points automatically. Trade-off named: a calibrated crown means re-anchoring a threshold
  *can* move it — but that lever is exactly what makes the field distinguishable, which percentile
  could not.
- **Phase 15 (done):** **the exploration feedback loop.** Explore proposed profiles and then
  forgot it had: the only action was "Test to minimum" (a full confidence top-up before you knew
  whether the idea went anywhere), and the prediction evaporated the moment the benchmark started
  — so nobody could answer the question that decides whether the page is worth reading, *when the
  data suggested something, was the data right?*. Two changes. **(1) Two test lengths**
  (`POST /api/explore/test`): **"Test now"** runs 5 iterations (`explore_tracker.QUICK_ITERATIONS`,
  one chunk) for the cheap "did this go anywhere?" reading; omitting `iterations` keeps the
  top-up. Both share `routes_settings.start_settings_test`, and the candidate is now materialized
  on the **parent's** stored settings (`explore.full_overrides`) rather than pasted onto whatever
  the firewall is currently on — without that, every test after the first measured a profile
  nobody proposed. **(2) The recommendation ledger** (`explore_tracker.py`,
  `models.ExploreRecommendation`, `GET /api/explore/recommendations`): the claim is written down
  *before* it is measured, the verdict is **derived on every read** from the measured field
  (`on_target`/`better`/`worse` against the candidate's own stated band, plus `pending` and
  `incomparable` for a claim made under another methodology), and each row says **why** it landed
  or missed. The aggregate splits calibration **by evidence class** (matched pair / conditioned
  neighbourhood / marginal curve / confounded curve), which turns the model's own stated
  uncertainty about confounding into a measured number instead of a hunch.
  **(3) Fixed the correlation bug that (1) introduced** — reported as *"tested a recommendation,
  the tests ran, but the profile shows no history"*. Materializing the parent meant handing the
  apply path the parent's **whole** writable set, and `_apply_writable_overrides` re-canonicalized
  every field through `coerce_value` ("5ms" → 5): semantically identical, textually different, so
  no write was planned, the firewall kept reporting "5ms", and `fingerprint(target)` named a
  profile that could never exist. The runs were filed — correctly — under the firewall's own
  fingerprint while the recommendation held the invented one. Three fixes, in increasing order of
  robustness: `_apply_writable_overrides` keeps live's representation for any value already
  `_field_equal` to it; `settings_profile.unwritable_diffs` surfaces the differences `plan_apply`
  silently drops on a pipe it can't write, which `start_settings_test` reverts (reporting them) so
  its fingerprint is reachable; and the ledger resolves each claim's profile from the **runs its
  profile test actually produced** (`job_group`), re-pointing and noting any row the benchmark
  contradicts — correlation by construction rather than by a hash agreeing with reality.
- **Next:** multi-parameter Bayesian search + interleaved A/B with effect-size/CI + hysteresis;
  routing intelligence / SD-WAN. (Latency-under-load/bufferbloat is explicitly **out of scope**.)

⚠️ Firewall **writes** go only through `provider.apply()`. Eight callers use it, all
snapshot/restore, reversible, or explicitly armed: the experiment engine (disarmed +
dry-run by default), the Shotgun Sweep (restores baseline at end + on startup), config
test-apply (+1 then revert), sweep apply-best (explicit, supervised), the
profile test (`profile_test.py`: apply → benchmark → restore, baseline persisted +
reconciled on startup), the **challenger race** (`challenger.py`: time-boxed
apply → 1 iteration → re-rank, restoring the baseline at the end — or applying the
winner when `auto_promote` — baseline persisted + reconciled on startup), the
**profile refresh** (`refresh.py`: for each stored profile apply → benchmark N
iterations → next, restoring the baseline at the end — baseline persisted + reconciled
on startup), and the **crown follower** (`crown_follower.py`: disarmed by default;
when armed, a deliberately one-way apply of the crowned profile — being on the crown is
the desired steady state, so like the supervised apply-profile there is no baseline to
restore). Keep new write paths to `provider.apply()` and always snapshot/restore.
The **one** firewall write that is *not* a `provider.apply()` shaper-param change is the
pipe on/off toggle `provider.set_pipe_enabled()` — used only by the **baseline test**
(`baseline_test.py`: snapshot pipe states → disable SQM on every pipe → settle → benchmark →
restore, persisted + reconciled on startup). It's deliberately separate because `enabled`
isn't a profile-identity/writable shaper field; it still obeys the same snapshot/restore +
coordinator-lock discipline.

⚠️ Any **apply-firewall + benchmark** session must hold the `coordinator.py` lock so
two never overlap (user-triggered ones — sweep, profile test, challenger race, profile
refresh, baseline test, manual `/api/run` — `hold` and queue; periodic ones — monitoring,
experiment, the nightly baseline test — `try_hold`/`hold` and defer/queue).
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
