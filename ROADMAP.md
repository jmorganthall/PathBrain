# PathBrain roadmap — future ideas

A running file for good ideas we've deliberately deferred. Keep the `Next:` bullet
in `CLAUDE.md` for the short list of what's coming up; this doc is the fuller backlog
with the reasoning preserved so we don't re-derive it later.

**Format for each entry:**

> **CHANGE HEADLINE:** a few sentences about the problem.
> **So what / then what:** what are we going to do about it?

Newest ideas at the top. When an item ships, move it to `CLAUDE.md`'s Phase map and
delete it here (or strike it through with the version it landed in).

---

## Open ideas

**THE OVERNIGHT MODULE (nobody schedules explore and adjudicate together).** Two engines
now do the right halves of one job and neither knows about the other. `explore.rank_bets`
answers *what should we measure tonight?* — the candidates ranked at the pessimistic end of
their band, calibrated against the recommendation ledger's measured miss per evidence class.
`duel.build_queue` answers *who should fight?* — the ring's own optimistic ceilings, tiered
so the bout most likely to unseat the belt runs first. Both are **pure functions** over the
field, which is exactly what makes them composable; what's missing is the thing that alternates
them. Today a person presses "Run the best bets" and, separately, arms the ladder, so a night
either explores or adjudicates and the field grows only as fast as someone remembers to press
a button.
**So what / then what:** A scheduler that alternates *explore* (queue tonight's smartest bets
through `job_queue`, which already survives a restart) with *adjudicate* (a lever or ladder
session over the survivors). The bets mature into pooled evidence as they run; the ring then
decides between them and the ledger grades what the model claimed. Every piece exists —
`rank_bets`, `build_queue`, the queue, the ledger, the campaign — so this is composition, not
new machinery. The open design question is the *budget split*: exploring is how the field grows
and adjudicating is how it's trusted, and a night spent entirely on either is a night wasted on
the other.

**SHOULD THE CROWN READ THE REPEAT VISIT?** The crown grades a **first visit** — a fresh
context per page, every handshake paid. Most of a person's clicks are not that: a site's next
page reuses its connections, a resumed TLS/QUIC session skips the round trips. The browser
plugin already measures both (`browser.warm_loads`, one warm load per page on a run's first
iteration, same-context with the HTTP cache disabled), and `GET /api/methodologies/warm-agreement`
already answers the empirical question — every profile ranked by a cold and a warm Overall built
from the methodology's **own** crown metrics, thresholds and weights, reported as the Spearman ρ
plus each side's #1. What nobody has done is act on a `disagree`.
**So what / then what:** Watch the card. `agree`/`same_top` means the cold crown stands for the
warm case and nothing needs doing. A sustained `disagree` is the measured argument for publishing
a version whose Overall reads the warm legs — or corners over both, which is a genuinely different
rubric and deserves the argument written down before the version is cut. Deliberately a decision
and not an automation: the crown metric set is the one thing in PathBrain that should never move
on its own.

**`compute_profiles` IS STILL BOUNDED BY TIME, NOT BY THE QUESTION.** `profile_aggregates`
fixed this for the crown grades — `profile_overalls` 1.55s → 0.13s, `duel.standings()` 4.09s →
0.35s — by materializing one row per profile and re-verifying it against a cheap stamp. The
field pass itself was left behind, measured at **~22s over 120k runs**, of which
`_completed_runs_with_scores` is ~107%: it materializes full ORM `Run`+`Score` entities plus
~6 `BenchmarkResult.metrics` blobs per run, about **840k JSON documents** decoded to keep ~150
profiles. The memo and the single-flight gate hide it from most page loads, but the cost is
still proportional to all history rather than to the number of profiles anyone asked about.
**So what / then what:** Migrate it onto the rollup. That needs two things this change doesn't
have yet: the rollup carrying **axis and raw-metric series** (it holds crown medians and
quartiles today), and the genuinely per-run passes — the trends baseline, the recent-iteration
window, the weather cohort — restructured to read aggregates instead of rows. Its own change,
with its own equivalence harness proving the migrated field is identical to a full rescan, the
same discipline `profile_aggregates` shipped under. A measured, contained interim is already in:
deferring `Run.settings` from that load is 8.0s → 5.0s.

**THE RING'S MARGINS ARE SLIGHTLY CORRELATED, AND THE TEST DOESN'T KNOW IT.** Under the ring
design each challenger leg yields one margin against the mean of the belt legs flanking it, so
**consecutive margins share a belt leg** — correlation ≈1/6 at equal leg noise. The Wilcoxon
signed-rank test the bout is adjudicated on assumes independent margins, so it is a little
optimistic. This is stated in the design rather than hidden, and it is small: the peek penalty
that holds the realized false-verdict rate at ~alpha was fitted by simulation against the
*uncorrelated* case, so the true rate sits marginally above the nominal one.
**So what / then what:** Two honest options, in order of cost. **(1) Re-fit the peek penalty
under the real correlation structure** — the simulation harness already exists, so this is
changing what it simulates rather than writing anything new, and it moves one constant. **(2) A
block-aware variance estimate** in `PairedEvidence`, which is the statistically correct answer
and a much larger change to a component the whole ladder's verdicts run through. Do (1) first,
measure whether the shift is worth (2), and don't do either until `belt_every=3` is actually in
use — at strict alternation the correlation is the price of the strongest shared-weather
guarantee we have, and it's a price worth paying.

**THE UPDATE CHECK COMPARES COMMITS, NOT IMAGES.** `updates.version_info` compares this build's
`git_sha` against the latest commit on the update repo's default branch, and calls the difference
"an update is available". That is a *proxy*: what's actually pullable is whatever `:latest`
resolves to in GHCR, and the two disagree for a window after every merge — the commit lands, CI
is still building, and the chip promises an image that doesn't exist yet. The self-update ledger
makes this visible rather than mysterious (an attempt that Watchtower accepts while the registry
still serves the old image is recorded `no_change`, with the causes named), but the chip still
overstates.
**So what / then what:** Check the **registry digest** instead — resolve `:latest`'s manifest
digest and compare it against the running image's, which tracks images exactly and needs no
guess about CI timing. The cost is a second auth path (GHCR's token endpoint, and a real one for
a private package) where the commit check rides an unauthenticated GitHub API call, which is why
it wasn't done first. Keep the commit compare as the fallback when the registry can't be read.

**MULTI-PARAMETER BAYESIAN SEARCH + HYSTERESIS.** Explore proposes candidates from response
curves, matched pairs and the ring's paired transitions, ranked by a deterministic UCB in
normalized lever space — dependency-free and explainable in a sentence, which is the standard
every score here is held to. What it isn't is a *model*: it can't represent the coupling the
basins demonstrate (several local optima ≥2 levers apart, so no single change crosses between
them), and it prices a two-lever move by widening a band rather than by knowing anything about
the interaction.
**So what / then what:** A surrogate over the whole lever space — the classic answer is a GP
with an expected-improvement acquisition — fitted on the pooled field and *checked against the
ring*, which is the part that makes it safe: the recommendation ledger already grades every
claim by evidence class, so a model's predictions become a measured track record rather than an
assertion. Two conditions before it earns its dependency: the deterministic version has to be
demonstrably out of road (the ledger will say so — a model class that keeps missing is the
signal), and whatever replaces it must still explain a proposal in a sentence. Hysteresis
belongs with it: crown-follow is deliberately a mirror with no damping today, and a search that
proposes faster is exactly what makes damping necessary.

**ROUTING INTELLIGENCE / SD-WAN.** Everything PathBrain measures is one path. A household with
two uplinks has a second empirical question — *which path feels better right now, and is that
difference worth the disruption of moving?* — that none of the current machinery answers, since
a profile is a shaper configuration and not a route.
**So what / then what:** Deliberately last, and deliberately hysteretic. The measurement half is
mostly reusable (the crown metrics and the weather stamp don't care which uplink carried the
bytes) but the *acting* half is not: route flapping is worse than a slightly slower route, so the
bar is a sustained, meaningful, weather-controlled win — the duel's paired design applied to paths
rather than profiles — and never a single reading.

**POSTGRES / OAUTH.** SQLite with WAL is genuinely the right store for a single-container
appliance measuring one household's link, and the pool sizing is now tuned for it rather than for
a networked database's scarcity model. Multi-tenant or multi-node changes both answers.
**So what / then what:** Not until something actually needs it. `PATHBRAIN_DATABASE_URL` is a
SQLAlchemy URL and the migrations are additive, so the seam exists; the work is the SQLite-specific
pool sizing, `json_extract` (used in several hot reads) and the advisory `flock` leader election,
which would want a real advisory lock on Postgres.

---

## Explicitly out of scope

**LATENCY UNDER LOAD / BUFFERBLOAT.** Every SQM tool measures this and PathBrain deliberately
doesn't. Saturating the link to score it measures a state the household is almost never in, and
it makes the measurement itself the disruption — while the everyday effect of fq_codel on an
unsaturated link is the round-robin interleaving of a page-load burst, which the crown and the
burst-fairness metrics measure directly. If bufferbloat ever comes back it comes back as a
*diagnostic*, never as a crown metric.

---

## Shipped

- ~~**INTERLEAVED A/B CHALLENGER RACE (weather-cancelling paired comparison).**~~ Shipped, and
  larger than proposed: the **duel ladder** (`duel.py`) runs counterbalanced ABBA rounds — one
  iteration a side with the lead alternating, `iterations_per_round` medianed — so weather hits
  both halves of a round and cancels inside the margin. Bouts are adjudicated on the **paired
  margins** (one-sided Wilcoxon signed-rank, exact below 25 pairs, with a simulation-fitted
  Pocock peek penalty holding the realized false-verdict rate at ~alpha) rather than on the
  sign test the first cut used, plus a practical-significance floor and the SPRT walk retained
  as the futility detector. The shared-weather assumption isn't trusted either: every leg is
  stamped with its measured severity and the round records the **shift** between its legs.
- ~~**METRIC-BASED "VS WEATHER" (stop inferring conditions from the profiles under test).**~~
  Shipped as `weather.py`, and the neighbour-pool baseline it was written against was **deleted**
  rather than adjusted. Weather is defined by each run's **own clean covariate readings** — probe
  DNS/TCP/TLS/latency plus the browser's own nav setup phases, profile-orthogonal by construction
  — ranked against their all-history distribution into a 0–100 severity; `cohort_residuals` bands
  runs into severity quintiles and compares each run's Overall against **other profiles' runs in
  the same band**. Surfaced as the one canonical "vs weather" column, the `weather_beater` flag
  and the response-level `weather_crown_suspect` alert — strictly flag-and-steer, never a crown
  input, and a suspect triggers a race rather than a re-rank. The dedicated **Weather** page adds
  the empirical gate the original entry asked for: per-covariate × crown-metric Spearman ρ (pooled
  *and* within-profile) beside the **variance decomposition** — the adjusted within-profile R² of
  the covariates jointly, read as a ceiling on what any covariate-based adjustment could remove,
  whose complement is the duel's paired design justified as a measured number. The metric-based
  `weather_adjusted_overall` (step 2 of the original plan) survives in the payload but is no
  longer rendered: the cohort residual is the better answer to the same question.
- ~~**MAGNITUDE-AWARE CROWN / HEIR CEILING (percentile rank is magnitude-blind).**~~ Shipped in
  **`speed-smoothness-v15`**, and the trigger was the failure the entry predicted: on a fast link
  ~149 profiles packed into a few milliseconds, so the percentile corner carried a **±17-point
  standard error** and the top ~66 were a statistical tie no amount of running could break — the
  noise was *manufactured* by rank normalization, not present in the raw ms. The Overall is now a
  **weighted average of the perception-calibrated subscores** (FCP 1 · LCP 1 · network_stall_all
  0.5), magnitude-aware and low-noise, so a profile's median Overall pins to ~±1 and the field
  separates. The equal-spread property the entry worried about losing is kept where it was
  load-bearing: the per-metric **percentile standings columns** remain, so the raw ranking is
  still legible beside the graded verdict. Ties became sample-size-aware in the same pass
  (`crown_tie_sigma` × the pooled **standard error of the medians**, so collecting data can now
  *break* a tie the old raw-IQR fraction froze forever).
