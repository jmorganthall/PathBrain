# Plan: act on the strategic review, in 30-minute sprints

Status: **proposed, 2026-11-15.** Nothing here is built yet. The review it implements is the
Claude Doc "PathBrain strategic review" (2026-11-14, revised after Josh's correction that the
goal is everyday, not-under-load experience and that explore and the duel run all night).

## Goal

Get PathBrain back to answering one question well: **does this fq_codel profile make the
Internet feel better day to day?** That means a quiet instrument at night, a crown that is
tied to how pages feel to a person, one referee, and raw data that is never rewritten.

## How to run this plan in Claude Code

- One sprint is one Claude Code session and one PR, sized to about 30 minutes of work
  including tests. If a sprint grows past that, stop, commit what is green, and split the rest
  into a new sprint at the end of its phase.
- Start each session with: "Do sprint S<n> from docs/plan-strategic-review.md." The sprint
  says which files it touches and what "done" means. Tick its box in this file in the same PR.
- `pytest backend/tests` must pass before every PR. Frontend sprints also run the frontend
  build and tests. Known failure on main at 3033502, before any sprint:
  `test_overall_ranking.py::test_the_endpoint_ranks_off_the_rollup_and_the_ledger`
  (expects `verdict-test-lo`, gets `verdict-test-hi`). A sprint must not add failures.
- Each merge to main publishes a new image (`docker-publish.yml`). The NAS runs it once it
  pulls or self-updates, so behaviour-changing sprints reach the live system on merge.
- Anything that writes to the firewall goes through `firewall_write.py` and its guard, as
  today. No sprint adds a second write path.
- Sprints marked **Josh** need a decision or an action from him before they start.

## Principles

1. **Raw is never rewritten.** New facts land as new rows or new derived versions.
2. **The methodology is the only place a crown rule lives.** Code reads it; it never repeats it.
3. **Measure the instrument before the profiles.** A night the host was busy is not evidence.
4. **A person's rating outranks a proxy.** Weights are fitted to ratings, not chosen.
5. **Small, reversible steps.** Each sprint ships behind the current behaviour or replaces it
   with a test that proves the two agree.

## Order and why

| Phase | Review rec | Sprints | Why here |
|---|---|---|---|
| A | 3. CLAUDE.md to a rulebook | S1–S3 | Every later session reads it; 415 KB of diary slows and misleads them. |
| B | 1. Quiet the night | S4–S9 | Profile gaps (~0.1 pt) are buried by the host slowdown (~3–4 pt from 07:00 UTC). |
| C | 6. Snowflake sweep | S10–S17 | Makes later methodology changes one-line. Pure refactors, low risk. |
| D | 5. Immutability breaks | S18–S26 | Stops history moving under the ladder. |
| E | 2. Ground truth | S27–S36 | Ratings and real-browser metrics; needs a clean store (D) to land in. |
| F | 4. One referee | S37–S40 | **Josh** picks the crown policy first. |
| G | 7. Reference page | S41–S42 | Stops the test page itself from drifting. |
| H | 8. Refocus Explore | S43–S44 | Last: needs the calibrated target from E. |

About 44 sprints, roughly 22 hours of focused work. Phases A–C can start today. B can run in
parallel with C once S4 is done.

---

## Phase A: CLAUDE.md becomes a rulebook (rec 3)

- [x] **S1. Move the diary.** Moved the `## Layout` section (lines 10–4081, the design diary)
  verbatim to `docs/history/claude-md-diary.md` with a short header; CLAUDE.md keeps a
  five-line Layout that links to it. (Done first, so no text is lost between PRs.)
- [x] **S2. Write the rulebook.** Condense what is left (Conventions and Phase map, about 530
  lines) into a CLAUDE.md of about 100–150 lines: layout, the medallion rule, where a crown
  rule may live, the firewall write path, test commands, PR style. Move the long-form
  Conventions and Phase map text verbatim to `docs/history/` too.
  Done when: CLAUDE.md is under 20 KB and every rule in it points to a file or test.
- [x] **S3. Size guard.** Add a test that fails if CLAUDE.md passes 25 KB, with a message
  pointing to `docs/history/`. Done when: the test passes on S2's file.

## Phase B: take the host's noise out of the night (rec 1)

- [x] **S4. Baseline numbers (read-only).** Add `scripts/night_quality.py` that prints, per
  night for the last 30 nights: matches, decisive share, resolving power over 30 rounds, and
  the share of runs whose `host_pressure` attribution is `host`. Save today's output to
  `docs/history/night-quality-2026-11.md`. Done when: the 845 / 342 and ~0.85-point figures
  reproduce (or the gap is explained).
  Built as `backend/scripts/night_quality.py` (test: `test_night_quality.py`); no live DB from
  the dev session, so Josh runs `docker exec pathbrain python scripts/night_quality.py --nights 30
  --tz America/Chicago` on the NAS and pastes the output into `docs/history/night-quality-2026-11.md`.
- [ ] **S5. Josh: decide the host fix.** Read the NAS slowdown task's finding. Choose one:
  move the NAS's 07:00 UTC job out of the window, end the window before it, or run the
  sensor on a dedicated quiet box. No code.
- [x] **S6. Host-quiet gate in config.** Add a `host_quiet` block to `config_store.py`
  (`enabled`, `max_host_share`) and a pure function in `host_pressure.py` that says whether a
  run was measured on a quiet host. Unit tests only. Done when: the function is covered and
  nothing calls it yet.
- [x] **S7. Quarantine slow-host runs from the duel.** In `duel.py`, a round whose legs fail
  the host-quiet gate is recorded but not counted, and the match asks for a replacement round.
  Done when: a test with one noisy round shows it excluded and the verdict unchanged.
  Replacements are capped by `host_quiet.max_replacement_rounds` (default 10) per match; past
  the cap rounds count as before, so a busy night still decides.
- [x] **S8. Blackout hours.** Add `blackout` (list of local hour ranges) to the duel and
  explore schedules in `schedule.py` / `scheduler.py`, so neither starts or continues a round
  inside it. Default empty. Done when: tests cover a window that straddles midnight.
  Built as `duel.blackout` only: Explore has no schedule of its own (its bets ride the duel's
  explore share), so it stops with the ring. Applies to scheduled and continuous sessions, not manual ones.
- [ ] **S9. After snapshot.** Two weeks after S5–S8 are live, re-run S4's script and append
  the result. Success: resolving power under 0.85 points and fewer than 59.5% of matches
  undecided. If not, reopen S5.

## Phase C: one source for crown rules (rec 6)

- [x] **S10. Crown keys from the methodology.** Add `methodology.crown_keys(version)` and use
  it for `instrument_drift.CROWN_KEYS` and `evidence.EPOCH_KEYS`. Done when: both constants
  are gone and their tests pass unchanged.
  The names stay as derived values (callers import them), no longer literals. `EPOCH_KEYS` keeps
  cadence out through a documented `evidence.EPOCH_EXCLUDED_KEYS` filter (its daily medians are too coarse).
- [x] **S11. Raw metric list from the methodology.** Same for
  `profile_aggregates.RAW_METRICS`.
  Read as the crown's millisecond legs (`methodology.crown_metric_keys(unit="ms")`): cadence is
  unitless and the raw-ms crowns compare times, so it stays out by a named filter.
- [x] **S12. Drop the settings fallback.** Remove `CROWN_METRICS` in
  `api/routes_settings.py:75` and make its callers read the methodology; add a test that the
  Overall page and the duel name the same crown legs.
  The fallback only fired for a definition with no `overall` spec (a pinned v1–v4, or an
  unregistered version id); that case now reads `methodology.ranking_crown` (v6's legs, the same set).
- [x] **S13. Leg-first specs from the definition.** Build `pooled_field.LEG_FIRST` from the
  methodology's legs instead of a literal dict.
  Only `then` is derived (the other millisecond crown leg); each policy's lead and band stay named
  policy data (`LEG_FIRST_POLICIES`). A policy the crown cannot serve falls back to the pooled crown.
- [x] **S14. Frontend reads the verdict.** `frontend/src/utils/ringReading.ts` stops
  re-deriving the verdict state; the backend sends it. Done when: the UI shows the same states
  for the fixtures in the existing tests.
  There were no frontend tests, so the 17-case table (`tests/fixtures/ring_reading_cases.json`) is new;
  `test_ring_reading.py` runs the TS through node (skipped without node_modules). Served on `/api/duel/status`.
- [x] **S15. Guard test.** A test that scans `backend/pathbrain` for crown metric key
  literals (`"fcp_ms"`, `"lcp_ms"`, `"cadence_cov"`) outside `methodology.py` and an allow-list.
  Built as a ratchet (`test_crown_literal_guard.py`): per-file counts with a reason each, also
  counting `"fcp"`/`"lcp"`; 13 files, 88 literals, 42 marked to move (crowning.py, pooled_field.py).
- [x] **S16. Move the ranking engine out, part 1.** Pure move of the percentile crown helpers
  (`_crown_corner`, `_percentile_norm`, `_crown_field_values`, `_crown_tie_params`, about
  lines 101–300) from `routes_settings.py` to a new `pathbrain/ranking.py`. No behaviour change.
  Moved verbatim (78 lines); routes_settings re-exports them. No other private helper was used only by
  these four, and the moved code held no crown literals, so S15's allow-list is unchanged.
- [x] **S17. Move the ranking engine out, part 2.** Move the remaining ranking code (around
  lines 2340–2740) to `ranking.py`; routes only call it. Done when: `routes_settings.py` is
  under about 3,500 lines and tests pass unchanged.
  `_compute_profiles_uncached` and the 25 helpers and constants it reaches moved (4362 → 3159 lines);
  `compute_profiles` (the field cache and single-flight) stays in routes, which re-exports every moved name.

## Phase D: stop rewriting history (rec 5)

- [x] **S18. Experiments read versioned scores.** `experiment.py:155` reads `scores` for the
  current methodology instead of `run.score.sops`.
  Reads the Score row's persisted Overall via the duel's `_run_overall`; no row reads as missing (None, skipped).
- [x] **S19. Sweeps read versioned scores.** Same for `sweep.py:209`.
  Same reader as S18; no row reads as None, so ranking and apply-best skip it and an all-None sweep has nothing to apply.
- [x] **S20. Insert-only at-measure, design.** Write a half-page note in this file: how
  `methodology.upsert_score` (line ~1616) becomes insert-only for `is_at_measure=True` rows,
  and what reads "latest". Add a failing test that a second at-measure write raises.

  *Design (S20).* Today a `(run, version)` pair has one `scores` row, and a re-grade under
  the run's own version rewrites it in place with `is_at_measure=True`. That happens because
  `runner.score_metrics_under` (`runner.py:813`) sets the flag to `run.methodology_version ==
  methodology.version` for capture (`runner.py:1364`) and re-grade (`score_run_under`,
  `runner.py:879`) alike. So the capture-time record is lost.
  - **Write rule.** The at-measure row is written once, at capture. `upsert_score`
    (`methodology.py:1667`) looks rows up by `(run, version, is_at_measure)`. A second
    at-measure write for the same run and version raises `ValueError` and does not insert:
    a second capture of one run is a bug, and raising surfaces it where a silent second
    row would hide it.
  - **Re-grades.** `score_run_under` always writes `is_at_measure=False`. It upserts one
    at-present row beside the at-measure row and never touches the at-measure row. So a pair
    holds at most two rows: then (at-measure) and now (latest re-grade). Re-grading again
    rewrites only the now row.
  - **What "latest" means.** For every reader of a run's score under a version, "latest" is
    the at-present row if one exists, else the at-measure row. That is the newest row by
    `id`, which always beats `computed_at` order because the at-present row is written
    later. S21 adds one selector for this in `methodology.py` (the newest row per
    `(run_id, version)`) and routes every reader through it.
  - **Readers that join or group** would count a run twice once two rows can exist, so
    these must use the selector:
    - `ranking.py:413`, `ranking.py:422`
    - `profile_aggregates.py:120`, `profile_aggregates.py:187`
    - `beliefs.py:737`
    - `why.py:225`, `why.py:325`
    - `refresh.py:185`, `refresh.py:253`
    - `explore_tracker.py:793`
    - `host_adjust.py:230`
    - `publish_followthrough.py:76`
    - `runner.py:1480`
    - `duel.py:879`, `duel.py:3053`
    - `routes_settings.py:293`, `routes_settings.py:1937`, `routes_settings.py:3080`
    - `routes_trends.py:65`
    - `routes_score.py:259`, `routes_score.py:448`
  - **Single-run readers** that today take `.first()` or `scalar()` and need an order:
    - `duel.py:712`, `duel.py:748`, `duel.py:846`, `duel.py:932`
    - `routes_results.py:44`
    - `routes_history.py:27`, where the `{run_id: row}` map is last-wins
  - **Readers that want both rows on purpose:**
    - `routes_score.py:194` (then vs now), which already orders by `is_at_measure desc`
    - `research_export.py:295`/`:303`, which exports the flag
  - **Schema.** Replace `UNIQUE(run_id, methodology_version)` (`uq_score_run_methodology`)
    with `UNIQUE(run_id, methodology_version, is_at_measure)`. No new column is needed: `id`
    orders newest. SQLite can't drop a constraint, so `database._migrate` rebuilds `scores`
    once (create, copy, drop, rename). It finds the old constraint in `sqlite_master.sql`,
    which makes it idempotent.
  - **Existing rows** are kept as they are. A same-version re-grade before S21 has already
    overwritten some at-measure rows in place, and their capture values can't be recovered.
    Those rows have `computed_at` well after the run finished, so S21 can count them and
    report the count.
- [x] **S21a. One "latest score" selector.** `methodology.latest_score()` (the newest row per
  `(run, version)`); every reader goes through it. No schema or write change, so behaviour is neutral today.
  Not routed: `routes_score.py` run_scores and `research_export` want every row on purpose; the refresh and
  Settings-Impact change stamps count every row; `upsert_score`'s lookup is S21b's.
- [x] **S21b. Insert-only at-measure, build.** The schema change, write-once at-measure, re-grades write
  `is_at_measure=False` (`runner.py:813`); make S20's tests pass. `_migrate` copies `scores` to a backup first.
  Built: the key is `UNIQUE(run_id, methodology_version, is_at_measure)`, rebuilt once on start (about 4 s per
  120k rows; needs free disk of about twice the `scores` table). **To restore the rows from the backup** (app
  stopped): `DELETE FROM scores; INSERT INTO scores SELECT * FROM scores_backup_s21b;`
- [x] **S22a. Settings overrides instead of backfill.** Replace `/settings/backfill`
  (`routes_settings.py:685`), which stamps today's settings onto old runs, with a
  `settings_overrides` table that records the correction beside the run.
  Built: append-only `settings_overrides` (created by `create_all`), `/settings/backfill` writes two rows per
  run (same request/response), and `pathbrain/settings_overrides.py` gives the effective value (SQL
  `effective_fingerprint()`/`effective_settings()`, or `overlay()` for loaded runs). Routed: ranking
  (`_completed_runs_with_scores`), `profile_aggregates`, `beliefs`, and routes_settings' own readers.
- [x] **S22b. Route the remaining settings readers** through `settings_overrides`: `refresh`, `explore_tracker`,
  `why`, `runner`, `publish_followthrough`, `levers`, `reachability`, `instrument_health`, `warm_agreement`,
  `crown_agreement`, `host_adjust`, `portable`, `research_export`, and routes `results`, `history`,
  `score`, `smoothness`. Decide what `/settings/refingerprint` does with an overridden run.
  Built: all routed except `portable` (it only copies the just-captured run's own value at capture, so
  it is allow-listed, not routed) and the runner's capture path. `refresh`'s profile cache also keys
  on the override table (`overrides_stamp`), since an override re-keys an old run without moving max id
  or count. `research_export` exports effective values plus `fingerprint_raw`/`settings_raw` and a new
  `settings_overrides.jsonl`. Refingerprint re-keys an overridden run by appending a
  `settings_fingerprint` override (source `refingerprint`), never writing its row; row-only runs are
  re-keyed in place as before. Ratchet: `tests/test_settings_overrides_readers.py` counts direct reads
  per file against an allow-list.
- [x] **S23. Instrument health as a derived table.** Move instrument health off the run row
  into a derived, versioned table (`instrument_health_v`), filled by the derive step.
  Built as `instrument_health_verdicts` (the repo names tables as plural nouns, not `_v`), keyed by
  run and `instrument_health.HEALTH_VERSION` (`ih-1`), insert-only, newest row per (run, version).
  Filled where health is assessed (`assess_run`, on the capture and re-grade score path); it is not
  part of `interpret.derive` because it needs the cross-run baseline. One accessor in
  `instrument_health` (`health_by_run`/`health_of`, SQL `effective_health`/`effective_ratio`); the run
  column is still written and is read only as the fallback for a run with no derived verdict under
  any version. Backfill: an additive, idempotent migration copies existing row stamps (source
  `run_row`), plus a delete trigger so verdicts never outlive their run. Routed: run detail,
  ranking's host model, `host_adjust`, `profile_conditions`/`slow_profiles`, research export (effective
  + raw + `instrument_health_verdicts.jsonl`). One sprint, no split. Ratchet:
  `tests/test_instrument_health_derived.py`.
- [x] **S24. Version the verdict rules, part 1.** Move the duel's verdict constants
  (`ROUND_WEATHER_SHIFT`, `WEATHER_SCALE_SAMPLE`, SPRT and Pocock settings) into the
  methodology record, read through one accessor.
  Built: `methodology.VerdictRules` (frozen) via `methodology.verdict_rules(version)`; v17 and the v18
  candidate state them as `verdict_rules` in the registry record (outside the frozen definition, so no
  stored definition changes), and every other version, fork base or GUI version falls back to
  `DEFAULT_VERDICT_RULES`, today's values. Moved, beyond the two named: `WILCOXON_MIN_PAIRS`, the
  exact-test cutoff (25), the Pocock-style peek penalty (3.32, 2.97, floor 2.0), the SPRT null p (0.5),
  p1 clamps (0.501/0.999) and default (0.70), the alpha clamps (0.001/0.2) and default (0.05).
  User dials (`duel.p1`, `alpha`, `min_pairs`, `max_pairs`, `min_margin`, `streak_wins`, `method`, the
  presets) stay in config; the abort streaks (`MAX_CONSECUTIVE_*`) end a session, not decide a verdict,
  and stay in duel.py. Old module names stay as aliases. Duel rows don't store the version yet (S25);
  the duel reads the code's current version, and the weather scale reads the stamper's version.
- [ ] **S25. Version the verdict rules, part 2.** Each duel row stores the methodology version
  it was judged under; a test shows an old duel keeps its verdict after a rule change.
- [ ] **S26. Retire legacy score paths.** Deprecate `/score/rescore` and `/score/rederive` in
  favour of the methodology-aware endpoint in `routes_score.py`; retire `score_results` reads.

## Phase E: ground truth from a person (rec 2)

- [ ] **S27. Ratings store.** Append-only `perception_ratings` table (time, profile
  fingerprint, A/B slot, rating, free note) and a POST/GET API. Never updated in place.
- [ ] **S28. Blind A/B swap job.** A daytime job that switches between two profiles at random
  through `firewall_write.py`, logging which was live, without showing which.
- [ ] **S29. Rating UI.** A one-screen mobile page: "Did that feel better or worse than a
  minute ago?" with three taps. Writes to S27.
- [ ] **S30. Calibration harness reads the table.** Point
  `calibration/smoothness_calibration.py` at `perception_ratings` instead of `ratings.json`.
- [ ] **S31. Chrome extension skeleton.** An MV3 extension that records FCP, LCP and long tasks
  with `PerformanceObserver` on pages Josh visits. Local only, no upload yet.
- [ ] **S32. Real-browser ingest.** A raw ingest endpoint that stores the extension's records
  unchanged, stamped with the live firewall fingerprint.
- [ ] **S33. LAN token.** The ingest endpoint and rating page accept only a LAN token from
  config. Tests for accepted and refused.
- [ ] **S34. Extension uploads.** The extension posts to S32 with the token.
- [ ] **S35. Ratings vs metrics view.** A page that plots ratings against crown legs and
  real-browser metrics per profile, with counts.
- [ ] **S36. Josh: fit and publish.** When there are enough ratings (target: 100 per profile
  pair, an Assumption to revisit), fit crown weights to them and publish a new methodology.

## Phase F: one referee (rec 4)

- [ ] **S37. Josh: pick the surviving crown policy.** The review recommends the duel ring.
- [ ] **S38. Revert f88f97d.** Revert that commit's crown change, with a test.
- [ ] **S39. Default to the chosen policy.** Set the default in `crowning.py` and hide the
  others (`math`, `lcp_first`, `fcp_first`), the fallback order and the speed budget from the UI.
- [ ] **S40. Remove the retired policies.** After two weeks with no use, delete them from
  `crowning.POLICIES`, `BUDGET_FALLBACK_ORDER` and `DEFAULT_SPEED_BUDGET`.

## Phase G: a reference page Josh controls (rec 7)

- [ ] **S41. Static reference page.** A fixture page in the repo with fixed text, images and
  scripts, sized like a typical site. Josh deploys it where he likes (for example Cloudflare
  Pages on his own domain, an Assumption).
- [ ] **S42. Add it to the methodology.** Publish a methodology version that includes the
  reference page as a site.

## Phase H: refocus Explore (rec 8)

- [ ] **S43. Explore targets the calibrated crown.** Explore's model scores bets against the
  methodology published in S36, not pooled Overall.
- [ ] **S44. Explore runs only in quiet hours.** Explore uses S8's blackout and S6's gate.

## Decisions

- No load testing. The goal is everyday, not-under-load experience (Josh, 2026-11-14).
- The plan file lives in the repo; each sprint is one PR.

## Open

- S5: which host fix.
- S37: which crown policy survives.
- S36: how many ratings are enough before fitting weights.
- Legacy methodology configs (v1–v4 or an unknown version): the Overall page and the duel read no crown legs, while the settings ranking falls back to v6's. Predates S12; decide whether to align.
- Sweep page's relative reading asks for the `sops` axis, which v17 has no longer, so it likely always reads empty (routes_sweep.py; predates S19).
