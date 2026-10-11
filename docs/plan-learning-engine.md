# Plan: a self-learning experiment engine + a value-driven comparison engine

Status: agreed design (grilling session, 2026-10-11). Nothing here is built yet.

## Goal

Find the profile that delivers the most pleasing human experience, using limited
overnight test time as efficiently as possible:

- **Experiment engine (Explore)** — proposes untested profiles with the most potential to
  beat everything measured, and *learns* from every result.
- **Comparison engine (duel)** — separates the candidates with paired, same-weather
  evidence until the best one is known.
- Both sit on top of **the right metrics** and **a sound crown**.

## Where we start (review findings)

- **Explore is a heuristic proposer with a scorecard, not a learner.** A prediction is
  parent Overall + an independently summed per-lever delta from a four-tier evidence lookup
  (`explore._predict`). The recommendation ledger widens bet bands by evidence class but
  never corrects the predictions. Candidates come from the top 5 parents and a fixed move
  menu; two-lever moves only from #1. Uncertainty is a kernel heuristic, the exploration
  weight is fixed, and Explore optimizes the **pooled** Overall while the crown is the
  **fused** ranking. It has never been validated in simulation.
- **Budget:** ~5% of each duel window, 5-iteration solo bets → ~1–2 weak signals a night.
- **Resolution:** the ring resolves ~0.85 Overall pts while top profiles sit ~0.1 apart;
  ~60% of matches end with no result. A 0.1-pt edge needs roughly 850 rounds (~12 nights)
  per pair at today's noise. Noise reduction is the highest-leverage fix.
- **Crown fidelity:** FCP·1 + LCP·1 + sub-perceptible `network_stall_all`·0.5, cold first
  visit only. Felt stalls, INP and warm repeat-visit clicks are not in it.
- **Strong already:** the duel's paired/ABBA/weather-stamped design, the fused ranking,
  instrument-health quarantine, the comparability gate.

## Decisions

### Objective and value
1. **Any real gain counts.** No margin floor on what can win; `duel.min_margin` stays 0.
2. **Tiny vs meaningful is a scheduling question, not a verdict.** Nothing is ever declared
   dead or parked. Every lever, candidate and match carries a posterior (mean + uncertainty);
   work is ordered by **expected improvement per hour**. A likely +0.00001 waits behind
   larger expected gains and rises when they are resolved.
3. **Revisit the crown before optimizing harder** — in order: (a) the existing cold-vs-warm
   agreement audit, (b) a new felt-stall audit (does the crown rank profiles with perceptible
   stalls / jank highly?), (c) blind 1–10 A/B ratings by the user via the calibration harness,
   including a known fast-but-janky profile, to fit crown weights. Any change ships as a normal
   methodology publish.

### Measurement noise (first)
4. Reduce per-round noise before anything else, each technique measured on existing duel data
   with "rounds saved" reported: (a) **per-site paired margins** within a round, (b)
   **weather-covariate adjustment** of round margins (a CUPED-style correction, checked against
   the existing variance decomposition), (c) **drop or replace the noisiest sites**.

### Explore (the learner)
5. **Target the fused Overall**, pooled used only as the prior for profiles the ring has
   never fought.
6. **A real surrogate model** fitted on *both* records, like `overall_ranking`: pooled medians
   as noisy point observations (with the measured slack τ), ring rounds as exact pairwise
   differences. Uncertainty comes from the model's own posterior. The recommendation ledger
   closes the loop: systematic misses refit the model's noise and length-scale hyper-parameters
   (self-learning), rather than only widening bands.
7. **Acquisition = expected improvement over the crown** (probability × size), replacing the
   fixed-weight UCB and the fixed move menu.
8. **numpy is allowed** (pinned in `backend/requirements.txt`); nothing else new. scipy only if
   the simulator shows it is needed.
9. **Bets are measured head-to-head**, as duel legs paired with the current #1 (ABBA, shared
   weather), with an **adaptive, value-based stopping rule**: continue while P(beats crown) ×
   expected size justifies the next round, abandon early losers. Runs still enter the pooled
   record. "Test now" stays as a manual shortcut.

### Search-space safety (hard rules)
10. **Only fields the registry marks writable** (`shaper_fields.WRITABLE_FIELDS`) are ever
    experimented on. Non-writable fields (`flows` and any future one) are never proposed,
    stepped or offered. A test asserts the model's search space ⊆ `WRITABLE_FIELDS`, so a field
    made unwritable later drops out automatically.
11. **Download bandwidth is excluded from experimentation entirely.** Explore and bets never
    propose a new bandwidth value; it is held at its current value. (Its effects include
    things PathBrain cannot measure.)
12. **Values:** within the experimentable fields, anything already measured on this link, plus
    **at most one step past the measured edge per lever per night**, and only to a value the
    firewall itself offers.

### Comparison engine (duel)
13. **Duels stop on value, not a cap.** A match pauses when one more round is worth less than a
    round elsewhere and resumes later with its evidence intact (carried matches). The
    Wilcoxon/SPRT/streak rules still decide verdicts; `max_pairs` remains only as a safety cap.
14. **One value-of-information scheduler** owns the night: each next block goes to the highest
    expected gain per hour — a bet, a duel round that could flip #1, or a re-check — replacing
    the fixed `explore_share` and the pair cap. Firewall writes keep every existing rule
    (coordinator lock, snapshot/restore, write ledger, guard).

### Data and validation
15. **Research export** (new): a background job that writes a streamed `.zip` of JSONL files plus
    a manifest — profiles + settings; every run with per-iteration crown metrics, nav phases,
    weather covariates, instrument health, timestamps, methodology; every duel match with
    per-round deltas, leg run ids, weather shifts, ending; the lever ledger; the Explore
    recommendation ledger; config + every methodology definition. **Excludes** secrets/API keys,
    the firewall address/credentials, and Away-test IPs/ISP data. Optional, off-by-default
    **per-site per-iteration crown values** re-derived from raw, with an estimated cost shown
    first. Date-range filter. Downloaded from the Data Dump page; progress in the jobs feed.
16. **Simulator built from real data** (fitted response surface, measured round noise, slack and
    weather), scoring each strategy by *nights until the true best is crowned*. It is the bar:
    nothing replaces current behaviour unless it beats it there.
17. **"Is it learning?" card:** model prediction error over time (should fall), crown fused
    Overall over time (should rise or hold), and the share of bets beating their parent versus
    a random-proposal baseline.

## Phases (one PR each)

| # | Phase | Done when |
|---|-------|-----------|
| 1 | Research export | Export of the full DB completes as a background job without blocking the API; manifest counts match table counts; secrets/IPs absent (test); per-site option works on a sample. |
| 2 | Noise + crown audits (read-only) | Rounds-saved reported for per-site margins, weather adjustment and site pruning; cold-vs-warm and felt-stall audit results in hand; blind-rating protocol ready. Any crown change goes through a methodology publish. |
| 3 | Simulator | Reproduces today's behaviour (no-result rate, resolution) from the exported data within stated tolerance; scores today's Explore + duel as the baseline. |
| 4 | Surrogate model + fused target + EI | Beats the baseline in the simulator on nights-to-true-best; search-space ⊆ writable test passes; bandwidth never proposed (test). |
| 5 | Head-to-head adaptive bets | Beats solo 5-iteration bets in the simulator. |
| 6 | Value-of-information scheduler + value-based duel stopping | Beats fixed share + pair cap in the simulator; all firewall-write rules unchanged (existing guard tests green). |
| 7 | "Is it learning?" card | Renders from existing endpoints; no field pass on page load. |

## Open item

- **Existing profiles that differ in bandwidth.** Decision 11 stops *new* bandwidth values.
  Whether the duel may still race *already-stored* profiles whose bandwidth differs from the
  live value (which writes that bandwidth during the leg) is not yet decided.
