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
    ("wins above replacement"). Powers `/api/trends/*`, the Dashboard delta chip,
    and the Settings-Impact "vs typical" column. A second, sharper baseline —
    `rolling_baseline_deltas`/`profile_weather_relative` — is the **contemporaneous
    "network weather"** reading: Overall minus the rolling median of all runs within
    **±2h in absolute time** (excluding the profile's own runs, `RunPoint.fingerprint`),
    so it neutralizes drift + one-off congestion + sweep-slot bias rather than only
    recurring day/hour patterns (which pool Jan and Jul into one cell). `profile_weather_relative`
    (`weather_overall`) is retained as a library reading but **no longer surfaced** — the
    Settings-Impact "vs weather" column was replaced by the **"% vs SQM off"** column (see
    below); the metric-based **"Weather-adj"** column (`weather_adjusted_overall`) stays.
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
    best profile (`compute_profiles` → `best_fingerprint`) as the crown changes. On its own
    interval (`config.crown_follow.interval_minutes`, default 30; scheduler-driven) each check
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
    posterior, no variance penalty, no time-window de-confounding enters the verdict. Returns
    `best_fingerprint` (+ `co_leaders` + `crown_confidence`). The
    challenger race reads `compute_profiles` and its bar is `best_fingerprint`'s Overall.
    **Finding challengers that could overtake the crown is a separate,
    smarter job** — the **Heirs to the crown** card + the challenger race rank under-sampled
    / stale profiles by their *optimistic ceiling* (`optimistic_overall`, the crown corner
    over each crown metric's p75 upper estimate) against the crown's Overall, to decide where
    to spend iterations to confirm or deny an heir. The **vs-typical** (`relative_overall`)
    delta is kept as an informational column (and a hook for smarter heir-hunting), not a
    crown input.
  - `database.py` — engine/session + additive SQLite `_migrate()` (ALTER for new
    columns; `create_all` for new tables).
  - `api/` — REST routers mounted at `/api`.
- `frontend/` — React + TS + Vite + MUI dashboard (dark mode). Pages: Dashboard,
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
  Experiments, Shotgun Sweep, **Baseline (SQM off)** (the "Test baseline behavior" tab: arm the
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
  when `PATHBRAIN_WATCHTOWER_URL` (+ optional `PATHBRAIN_WATCHTOWER_TOKEN`) is set, the
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
