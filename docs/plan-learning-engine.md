# Plan: one evidence backbone, and a learning Explore on top of it

Status: **revised design, 2026-11-09**, replacing the phase order agreed on 2026-10-11 (that
version is in git history). Decisions from it are kept unless marked *changed*. Phase 1 (the
research export) is built. Phase 2 is built except moving the duel's own round margins onto
`evidence.py` (they are already same-window paired differences; the move is a refactor that
touches `duel.py`, so it gets its own PR behind the firewall gate).

**Phase 2 check on live data (2026-11-09, approximated from per-run Overalls through the
API).** Of the ten graded bets, none had a parent run within two hours and five had one in
the same instrument epoch. Those five went from absolute errors of −8.3 to −18.4 to margin
errors of −0.1, −0.3, −2.4, −2.5 and −10.3; the last one claimed +9.8 over its parent from
a matched pair, so its miss is real. The other five have no control at all, because their
parents were never re-measured after the step. They stay graded absolutely and no longer
calibrate anything. That gap is what phase 4's paired bouts close.

## Goal

Find the profile that delivers the most pleasing human experience, using limited overnight
test time as efficiently as possible, with **one system that follows itself**: every engine
reads the same beliefs, writes the same kind of evidence, and reacts to the same audits.

## Why the revision (review of 2026-11-08, live data, read-only)

The October plan treated Explore as the thing to rebuild. The data says the deeper fault is
that each engine keeps its own yardstick, so a finding in one never reaches the others.

- **Three yardsticks.** The duel compares profiles as same-window paired margins. Explore's
  ledger grades a bet against the all-time pooled best (37.8). The AI page reads all-time
  per-profile distributions. They disagree by construction whenever the instrument moves.
- **The instrument moved and only one card noticed.** Field daily median Overall ~36 from
  Oct 11–25, ~27–29 from Oct 27–Nov 8. `instrument_drift` flags unexplained steps (LCP
  654 ms → 2.0 s on Oct 19; 1.3 s → 2.7 s on Nov 2). Nothing downstream reacts.
- **So the ledger reads 0 for 10.** Ten graded bets, mean error −12.2, none on target.
  Against the median of all runs within ±4 h of each bet the same ten come out mean −0.6,
  median +0.2, seven within about a point: level, not wrong.
- **And the drift steers Explore backwards.** The measured evidence-class bands (9.9 for the
  parent's neighbourhood, 13.2 for matched pairs) bury the best-evidenced candidates, and
  the top bet becomes an upload-bandwidth step from a class too thin to be distrusted.
- **Knowledge in one module is ignored by another.** `levers.MECHANISM` predicts CoDel
  target/interval/limit/ECN inert on this unsaturated link; 39 of 67 proposed lever moves
  (58%) went to upload limit, target and interval, and "upload target 3 → 4" was proposed
  nine times. This plan held download bandwidth; Explore still proposes the Upload pipe's.
- **A third of bets measure nothing.** 6 of 16 bets under v17 are pending although their
  tests completed: one 5-iteration run per bet, and the site-coverage gate quarantines the
  whole run when any page fails in any iteration (~22% of recent runs are incomparable).
- **The ring is ahead of Explore.** The fused top three (Peppy Sloop, Mystic Meerkat,
  Alpine Aurora; ratings 1565/1550/1547, SE 21–36, order not settled) all run Upload
  quantum 500 with Download quantum 5800–6300. Explore is not proposing there.
- **The ring has almost no controlled single-lever evidence**: the lever ledger uses 2 of
  50 sessions (Download quantum, 45 rounds, −0.41 ± 0.26, no direction).

## The principle

One backbone in three layers, the same medallion shape the raw store already has:

| Layer | Module | What it holds | Rule |
|---|---|---|---|
| Bronze | runs + raw (exists) | immutable observations | never modified |
| Silver | `evidence.py` (new) | **margins**: every comparison PathBrain makes, as a same-window paired difference, stamped with methodology version, derivation version, weather severity and **instrument epoch** | the only place a comparison is computed |
| Gold | `beliefs.py` (new, generalises `overall_ranking.fuse`) | a posterior per profile (θ, SE) and per lever effect, fitted from silver | recomputed on read, versioned, never stored as truth |

Audits stop being cards and become **conditions every consumer honours**:

- `instrument_drift` steps → **epoch boundaries**. Pooled anchors are only compared within an
  epoch; a step is absorbed as an epoch offset instead of poisoning every comparison.
- `levers.MECHANISM` + the lever ledger → **lever status**: `live`, `inert` (predicted, not
  refuted), `surprise` (predicted inert, measured moving).
- `reachability` + bandwidth held → part of one **`search_space()`** that every proposer
  calls: writable ∩ reachable ∩ not held ∩ (live, or inert with its surprise check owed).

Every consumer reads gold and writes silver:

- **Crown / crown-follow / duel `ring_target`**: read θ (already the fused ranking today).
- **Explore**: proposes from the lever-effect posterior; a bet is silver evidence.
- **The recommendation ledger**: grades every claim in margin units against the same-window
  control. One grading rule for every engine.
- **The AI page**: its export reads beliefs and margins, not all-time distributions.

Enforced the way the firewall write path is (`test_no_module_writes_the_firewall_except_
through_firewall_write`): architecture tests that fail when a module computes a comparison
outside `evidence.py`, builds a candidate outside `search_space()`, or reads pooled medians
across an epoch boundary.

## The belief model

    θ_i      = f(x_i) + u_i            u_i ~ N(0, s²)        profile residual
    ring     d_ab  = θ_b − θ_a + ε       ε ~ N(0, σ²)          σ = decidability.round_noise
    pooled   μ_i,e = θ_i + c_e + η       η ~ N(0, SE² + τ²)    c_e = epoch offset

`x_i` is the profile's live levers, normalised; `f` is a Bayesian quadratic (main effects
plus the Download-quantum × Upload-quantum interaction) with a shrinkage prior. Everything is
linear-Gaussian, so the posterior is closed form: the same weighted least-squares family as
`fuse`, numpy only. `s`, `τ`, `c_e` and the prior scales are fitted by empirical Bayes, and
refitted after every graded claim. That refit is the self-correction: a model that keeps
missing widens or reshapes itself instead of having its bands widened from outside.

**With `f ≡ 0` and one epoch it is exactly today's `fuse`**, pinned by an equivalence test,
so the backbone can ship without moving the crown. A GP replaces the quadratic only if the
ledger shows the quadratic missing systematically.

## Explore on the backbone

- **Search space**: `search_space()` only. Download quantum × Upload quantum first; inert
  levers get one controlled surprise check each (a lever-session bout), as a hypothesis to
  refute rather than open space to fill. Values: firewall-offered only, at most one step
  past the measured edge per lever per night.
- **Acquisition**: Thompson sampling. Draw a surface from the posterior, test its best
  untested point. It balances exploring and exploiting with no hand-set weight and batches
  naturally.
- **Bets are paired bouts**: candidate vs the fused #1, interleaved 1-iteration legs
  (ABBA), through the ring's own machinery, so a bet is the same silver record as a duel
  round. Drift and time of day cancel, and a failed page costs one leg, not the bet.
  Sequential stop: abandon when P(beats #1) < 5% or a round is worth more elsewhere; cap
  rounds. Runs still land in the pooled record.
- **Hypothesis ledger**: each bet names the model term it tests, in a sentence ("Upload
  quantum 500 is worth +0.4 over 3000 when Download quantum is 5800–7300"), with predicted
  margin ± sd and the result that would refute it. Status (open / supported / refuted) is
  derived from the posterior on every read, like every other verdict here.
- **"Is it learning?" card**: prediction error over time, the #1's θ over time, bets beating
  the #1 vs a random-proposal baseline.

## What the backbone retires

| Today | Replaced by |
|---|---|
| `explore._predict` four-tier lookup, fixed-weight UCB, kernel uncertainty | the belief posterior + Thompson sampling |
| ledger grading against the pooled best | margin grading against the same-window control |
| evidence-class calibration bands | empirical-Bayes refit of the model |
| AI export's all-time distributions | beliefs and margins |
| 5-iteration solo profile tests as bets | paired bouts |

Matched pairs, conditioned curves, basins and interactions stay as **display diagnostics**;
they no longer price anything.

## Decisions

Kept from 2026-10-11: **1–4** (any real gain counts; tiny vs meaningful is scheduling; revisit
the crown before optimising harder; reduce per-round noise first), **8** (numpy allowed),
**10** (search space ⊆ `WRITABLE_FIELDS`, tested), **12** (values: measured, or one step past
the edge, firewall-offered), **13–17** (value-based duel stopping, one value-of-information
scheduler, research export, simulator as the bar, the learning card).

Changed:

- **5** (target the fused Overall) → target **θ from `beliefs`**, which is the fused Overall
  generalised; identical at `f ≡ 0`, one epoch.
- **6** (surrogate GP on both records) → **quadratic on margins with epoch offsets**; GP is
  the upgrade path.
- **7** (EI acquisition) → **Thompson sampling** for proposing; expected improvement stays
  as the value measure the scheduler (14) orders work by.
- **9** (head-to-head bets) → unchanged in substance, **moved to phase 4** from phase 5.
- **11** (download bandwidth excluded) → **both pipes' bandwidth held**, enforced in
  `search_space()`.

New:

- **18. One comparison primitive.** Every comparison is a silver margin from `evidence.py`.
- **19. Audits are conditions.** Drift steps define epochs; lever status and reachability
  define the search space; consumers honour them, tested.
- **20. The crown keeps reading `f ≡ 0`** until a backtest shows the lever model improves
  held-out ring predictions, and then only through a methodology-style publish.

## Phases (one PR each)

| # | Phase | Done when |
|---|-------|-----------|
| 1 | Research export | **Built.** |
| 2 | Evidence layer: `evidence.py`, instrument epochs from `instrument_drift` steps, `search_space()`; duel rounds and the ledger's grading move onto it; ledger re-graded in margins | Architecture tests pass; the re-graded ledger reproduces the 2026-11-08 finding (bets level with the same-window control); bandwidth never in `search_space()` (test) |
| 3 | Belief layer: `beliefs.py` generalising `fuse` with epochs and `f` | `f ≡ 0`, one epoch equals `fuse` (test); on the research export, predicts held-out ring margins at least as well as `fuse` |
| 4 | Explore on beliefs: `search_space()`, Thompson sampling, paired bouts with sequential stop, hypothesis ledger | Beats today's Explore in the simulator on nights-to-true-best (decision 16); never proposes held or unwritable fields (test) |
| 5 | Consumers: AI page export reads beliefs; "Is it learning?" card | Renders from existing endpoints; no field pass on page load |
| 6 | Noise audits and the value-of-information scheduler (old phases 2 and 6) | As in the 2026-10-11 plan |

## Open

- **The instrument step itself.** Epochs make every engine robust to it, but whether LCP
  doubling twice is a site change, a Chromium or container update or the network matters for
  whether the crown still measures what a person feels. Its own investigation.
- **Explore's share of the night** stays at 5% until the scheduler (phase 6) replaces it.
- **The site-coverage gate** quarantines a whole multi-iteration run on one failed page.
  Paired 1-iteration legs make bets immune; whether the gate should work per iteration is a
  methodology question, not decided here.
