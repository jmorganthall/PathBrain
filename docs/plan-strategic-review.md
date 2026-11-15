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

- [ ] **S10. Crown keys from the methodology.** Add `methodology.crown_keys(version)` and use
  it for `instrument_drift.CROWN_KEYS` and `evidence.EPOCH_KEYS`. Done when: both constants
  are gone and their tests pass unchanged.
- [ ] **S11. Raw metric list from the methodology.** Same for
  `profile_aggregates.RAW_METRICS`.
- [ ] **S12. Drop the settings fallback.** Remove `CROWN_METRICS` in
  `api/routes_settings.py:75` and make its callers read the methodology; add a test that the
  Overall page and the duel name the same crown legs.
- [ ] **S13. Leg-first specs from the definition.** Build `pooled_field.LEG_FIRST` from the
  methodology's legs instead of a literal dict.
- [ ] **S14. Frontend reads the verdict.** `frontend/src/utils/ringReading.ts` stops
  re-deriving the verdict state; the backend sends it. Done when: the UI shows the same states
  for the fixtures in the existing tests.
- [ ] **S15. Guard test.** A test that scans `backend/pathbrain` for crown metric key
  literals (`"fcp_ms"`, `"lcp_ms"`, `"cadence_cov"`) outside `methodology.py` and an allow-list.
- [ ] **S16. Move the ranking engine out, part 1.** Pure move of the percentile crown helpers
  (`_crown_corner`, `_percentile_norm`, `_crown_field_values`, `_crown_tie_params`, about
  lines 101–300) from `routes_settings.py` to a new `pathbrain/ranking.py`. No behaviour change.
- [ ] **S17. Move the ranking engine out, part 2.** Move the remaining ranking code (around
  lines 2340–2740) to `ranking.py`; routes only call it. Done when: `routes_settings.py` is
  under about 3,500 lines and tests pass unchanged.

## Phase D: stop rewriting history (rec 5)

- [ ] **S18. Experiments read versioned scores.** `experiment.py:155` reads `scores` for the
  current methodology instead of `run.score.sops`.
- [ ] **S19. Sweeps read versioned scores.** Same for `sweep.py:209`.
- [ ] **S20. Insert-only at-measure, design.** Write a half-page note in this file: how
  `methodology.upsert_score` (line ~1616) becomes insert-only for `is_at_measure=True` rows,
  and what reads "latest". Add a failing test that a second at-measure write raises.
- [ ] **S21. Insert-only at-measure, build.** Make S20's test pass; reads pick the newest row.
- [ ] **S22. Settings overrides instead of backfill.** Replace `/settings/backfill`
  (`routes_settings.py:685`), which stamps today's settings onto old runs, with a
  `settings_overrides` table that records the correction beside the run.
- [ ] **S23. Instrument health as a derived table.** Move instrument health off the run row
  into a derived, versioned table (`instrument_health_v`), filled by the derive step.
- [ ] **S24. Version the verdict rules, part 1.** Move the duel's verdict constants
  (`ROUND_WEATHER_SHIFT`, `WEATHER_SCALE_SAMPLE`, SPRT and Pocock settings) into the
  methodology record, read through one accessor.
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
