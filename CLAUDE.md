# CLAUDE.md — PathBrain developer guide

PathBrain is an empirical network-optimization platform that maximizes
*human-perceived responsiveness* — scored as the **Responsiveness / Smoothness /
Speed** axes (+ an **Overall** corner roll-up), not raw ping/throughput. (The
original single *Seat of Pants Score* was split into these axes; SOPS is now
legacy.) The optimizer is classical (deterministic sweep + hysteresis), not
LLM-based. See `README.md` for the product overview.

## Layout

- `backend/pathbrain/` — FastAPI app (the core), `backend/tests/` — pytest suite.
- `frontend/` — React/MUI app, built into the backend image.
- `docs/` — plans and methodology notes; `docs/history/claude-md-diary.md` — the design
  diary that used to live here (why each module is the way it is).

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
  0. **Read what it changed.** The Methodology page's **Crown agreement between versions**
     card (`GET /api/methodologies/crown-agreement`) prices the field under the prior and the
     new version and reports how far the ranking moved — before the flip (name the new version
     as `b` while it is still only in the registry) or after it (the defaults).
  1. **Re-grade history.** New/changed metrics derive from the **already-captured raw**
     (Resource Timing etc.), so a re-grade re-scores every run with the new
     metrics — **no re-collection / re-run needed**. Trigger it via the **Methodology
     page → "Re-grade history under current"** button (or `POST /api/score/regrade`).
     Only pre-raw-collection legacy runs (no raw) can't be re-derived — they stay
     quarantined as legacy. There is deliberately no "physically re-run every profile"
     batch; re-grading from raw is the supported way to bring history onto a new rubric.
     **The pass is best-first, and runs that cannot score are not visited**
     (`runner.regrade_order`): a publish exists to produce a crown under the new rubric,
     and oldest-first put the runs that decide one last — the first minutes of a
     40,000-run pass were spent on runs with no re-gradeable raw (*"scored 108/40430 ·
     7%"*). The order is the prior version's standings (`refresh.prior_field`, the same
     seed the ladder uses — a prior for *ordering*, never a score): its **confident**
     profiles best first, newest run first within a profile, then its thin profiles, then
     everything the prior never ranked newest first. A COMPLETE run with no benchmark
     result rows is left out up front and counted as `unscorable` in the summary rather
     than visited and skipped. Every run that can score still scores, in the one pass.
     The job summary carries `unscorable` and `ordered_by`.
  2. **Changing the sites is a publish too.** Do it from the Methodology page's "Sites
     measured" card (`POST /api/methodologies/sites`), never by editing config: the new
     version owns the list and quarantines runs measured against the old one, and the
     field is seeded from the prior version's standings until fresh runs arrive (see the
     "site list is part of the methodology" note above).
  3. **No frontend edit needed.** The **Dashboard's Overall card** and the Settings-Impact
     view are both fully crown-driven. The hero card's smaller gauges are the metrics the
     Overall is *actually* computed from, read from `GET /score/rolling`'s `overall_metrics`
     / `overall_method` / `overall_weights` (straight off the methodology's `overall` spec at
     request time), with each leg's subscore, measured value and weight; the axes are demoted
     to a clearly-labelled **"Axis breakdown"** strip. That distinction is the fix: the card
     used to show the three headline **axes** captioned as "the axes the Overall is built
     from", which stopped being true at **v5** when the Overall became a first-class quantity
     over the crown metrics — by v16 those gauges were dominated by metrics the Overall never
     reads (`render`, `load_event`, `cadence`, `evenness`, `byte_earliness`, `cls`), and the
     Completion axis it also rendered had lost every one of its metrics. A headline describing
     a rubric that stopped being current months earlier. `test_scores` pins it: the payload
     carries the crown, the crown is disjoint from the axis keys, and pointing the endpoint at
     another version re-points the card with no frontend edit.
     **The same correction applies to "Scores over time"** (`GET /score/axis-series`, the chart
     on the Dashboard and on Profile Detail): it trended the Overall beside Responsiveness /
     Smoothness / Speed, which invites the one wrong reading available — that the top line is a
     roll-up of the three under it. It is not, and has not been since v5, so a dip in the leg
     that actually moved the Overall could sit in none of the lines drawn. The endpoint now
     returns one list in which every series says what it *is*: `role` (`headline` = the Overall
     and the legs it is computed from, `axis` = the graded decomposition, kept and demoted) and
     `source` (`metric` = read from `Score.subscores`, `axis` = from `Score.axis_scores`) — the
     axes are kept rather than dropped because they are already on the Score row and a caller
     may want the graded breakdown trended, though no view draws them from here today — plus
     each leg's `label` and `weight` — both taken from the **frozen definition**, which is the
     snapshot the version was published with rather than today's registry. The frontend filters
     on `role` and names no metric, so that is the whole of the wiring. Three tests in
     `test_scores` pin it: the headline set is `["overall", *crown]`, the axes are present and
     disjoint from it, and pointing the endpoint at a v6 stub re-points the chart to
     `fcp`/`total_stall`/`load_event` with no frontend edit. Settings-Impact reads the same
     `overall_metrics` from the profiles response: the pinned **standings columns**, the **quadrant default axes** (X/Y/Shade =
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
- **Instrument health is part of comparability — a run measured on a sick machine is not a
  measurement of the link** (`instrument_health.py`, `Run.instrument_health`,
  `methodology.INSTRUMENT_MARKER`, `config.instrument`,
  `GET /api/methodologies/instrument-health`). Every other comparability check asks whether
  a run measured the same *thing* (sites, client, page coverage, the metrics the crown
  needs); none asked whether the thing doing the measuring was **working**. When the host
  degrades — leaked Chromium, a swapping NAS, a wedged session — the browser itself gets
  slower, and that lands **inside FCP and LCP through the render phase**: the link was fine,
  the numbers are worse, and the profile that happened to be on the firewall wears it in its
  pooled median forever, because the crown pools all history with no window and no
  weighting. Reported as *"profiles showing bad results that weren't actually bad"* after a
  stretch of NAS trouble — and those runs were on the record and counting.
  **What it reads**: only quantities the shaper cannot move (`HOST_QUANTITIES`) — the
  browser plugin's own host-side phases (context setup, context close, the timing reads,
  which touch no network at all) plus `nav_render_ms`, the ledger's client-role metric that
  sits inside the crown's own FCP and LCP. **How it grades**: deliberately *not* a percentile
  of the field, because the recent history being ranked against **is** the contaminated
  stretch, so a bad run among mostly-bad runs reads as normal. Each quantity is a **ratio
  against a robust baseline** — the 25th percentile of recent history (`BASELINE_PERCENTILE`),
  "what this machine does when it is well" — which holds until more than three quarters of
  history is bad, where a median gives up at half; and a ratio is absolute, so the verdict
  does not drift as the field grows. The run's reading is the **median ratio** across its
  quantities, so one noisy phase cannot condemn a run and one flattering one cannot rescue
  it. A baseline under `FLOOR_MS` is dropped outright: a 3 ms close against a 1 ms baseline
  is a 3× ratio that says nothing. **What it decides**: `healthy` / `strained` / `degraded`
  against two configured ratios. Only **degraded** quarantines, under its own token
  (`instrument`) through the one `comparability()` gate every scored view already filters on
  — so the crown, the standings, the rollup, Explore and the weather cohorts all drop it with
  no further edits. `strained` is recorded and shown and **still counts**, the same
  flag-and-steer discipline the weather stamp follows, and **no opinion never quarantines**:
  the gate removes measurements taken on a machine we can *show* was sick, and "we could not
  tell" is not that. **Why the record heals**: the readings have been written since the
  browser began timing its phases, so a run's health is derivable from what is already on
  disk. It is stamped at capture, and a run **without** the stamp has it computed and written
  back the first time it is graded — so one re-grade under the current methodology
  retroactively stamps and re-quarantines the bad stretch, with **nothing re-measured**,
  exactly as a new crown metric re-derives from stored raw. **The threshold is an evidence
  question, not a guess**: the audit endpoint reports the baseline, the spread of ratios over
  recent runs, and *how many runs each candidate threshold would quarantine* — a gate nobody
  can price is a gate nobody should arm. Run Detail carries the card (per-quantity this-run /
  healthy / ratio, and the verdict as a sentence with its numbers in it) and the
  comparability tip spells the token out. Best-effort throughout: an unreadable baseline, a
  run with too few host readings, or the gate switched off all degrade to `None` — a guess
  about the machine must never cost a real measurement. `test_instrument_health`.
  **When a profile was measured** (`instrument_health.profile_conditions`,
  `GET /api/settings/profiles/{fp}/conditions`, the **When it was measured** card on Profile
  Detail). The pooled median averages whatever runs a profile got, and the machine is slower
  for hours every night; a profile measured mostly then (the nightly duel window sits there)
  wears it in its standing — on the live ledger a #214-of-222 profile had most of its runs
  from one 05:00 session and was level with a #2 head to head. Per profile: runs by hour of
  day (UTC, shown local) and `slow_share`, the share of its runs whose host-speed ratio
  (`Run.instrument_health["ratio"]`, nothing the shaper moves) sits in the field's slowest
  quarter — a fair sample puts 25% there, `flag` at `CONDITIONS_SLOW_SHARE` (50%). Bounded to
  the newest runs, read-only, recorded only: nothing grades or gates on it.
  `test_a_profile_measured_mostly_on_a_slow_machine_is_flagged`.
- **Data-integrity audit (recipe vs. ingredients).**
 `GET /api/runs/{id}/verify-derivation`
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
  DB row FAILED; a live benchmark thread can't be force-killed mid-call — which is why
  failing the row was only ever bookkeeping, and the watchdog now also
  `coordinator.evict_if_stalled()`s a holder that has stopped beating (see `probes` +
  the coordinator lease above). `GET /api/health/pipeline` is the read that turns "it
  seems stuck" into an answer: lock owner, how long since progress, queue depth, probe
  worker health, and **every thread's stack** — the only thing that says which call is
  not returning. Read-only, no DB access.
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
- **Next:** the evidence backbone in `docs/plan-learning-engine.md` (2026-11-09): one comparison
  primitive (`evidence.py`), one belief layer generalising `fuse` (`beliefs.py`), audits as
  conditions, then Explore on top of it (Thompson sampling, paired bets); hysteresis;
  routing intelligence / SD-WAN. (Latency-under-load/bufferbloat is explicitly **out of scope**.)

⚠️ **Every user-triggered session goes through `job_queue.submit()`, and no start path
refuses because something else is running.** A busy pipeline is a *queue*, never a 409: the
coordinator has always serialized sessions, so an "already running" guard in front of it only
removes the user's ability to line work up. A new engine therefore (a) submits through
`job_queue`, (b) returns the standard placement block (`queued`/`ticket_id`/`queue_position`/
`queue_ahead`/`blocked_by`) from its start endpoint, (c) registers its `active()` with
`job_queue.register` so a second session of *its own kind* still waits, and (d) is driven from
the frontend by `useQueuedAction`, never by a bare `api.*` call — otherwise that button becomes
the one place with different behaviour, which is the bug this whole contract exists to end.
And it submits a JSON **`spec`** with a **starter** registered in `job_queue.register_engines`
(never a closure), so its queued ticket is written to disk and comes back after a restart like
every other kind's.
Reserve a 4xx for a genuinely bad request (an unreachable profile, an empty spec, a no-op),
never for "the pipeline is in use".

⚠️ Firewall **writes** go only through `firewall_write` (which calls `provider.apply_many()`;
an AST scan in `test_firewall_write` refuses any other caller). Eight callers use it, all
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

⚠️ **A field PathBrain never writes is never OFFERED either — not as a proposal, a range, a
step or a computed recommendation** (`test_no_unwritable_field_is_offered`). The registry
stops the *write path*: `plan_apply` emits only writable fields, `firewall_guard.before_write`
refuses a change naming anything else, `write_probe.NEVER_STEP` keeps `flows` out of the
sweep, `experiment._start` refuses a non-writable `param`, and a leg whose firewall settled on
another profile raises rather than measuring it. None of that stops a **reading** surface from
putting the hazard back on screen, and one did: the reachability card shipped with a *"What
would bring them back / Change the firewall to: Flows 1024 → 512"* table, ranked by the
measurement each change restored. No button, and still a "change this" that PathBrain computed
and is therefore answerable for — for a field that is unwritable precisely because writing it
took the household off the network for ~30 s every time. Naming the value is **capture** and
is fine (the optimizer export carries it with `writable: false` and no range or example; the
Profile Detail Settings card shows it with a *read only* chip); telling anyone to change it is
not. The guard walks the real API surfaces whose job is to say what can be changed — the
optimizer export, the sweep grid, the write-probe sweep preview, the reachability audit, the
per-profile settings view — and fails on a non-writable field appearing under an offer key
(`writable_fields`/`sweepable_fields`/`all_fields`/`proposals`/`steps`) or on prescriptive
prose beside one. It keys on `NON_WRITABLE_FIELDS`, so a field made unwritable tomorrow is
covered the day it changes, and it proves itself by asserting the detector fires on the exact
phrasing that shipped.

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
