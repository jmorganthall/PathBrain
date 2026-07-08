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

**INTERLEAVED A/B CHALLENGER RACE (weather-cancelling paired comparison).** The challenger
race eliminates a contender on its *absolute* standing — the optimistic ceiling (a raw
field-percentile corner) vs the crown's Overall. Because a contender's percentile is computed
against a field measured at different times, a profile that happens to run during a bad-network
window scores low in absolute terms, ranks low, and can be eliminated — even though the badness
was environmental, not the profile. The race mitigates this only indirectly (interleaving,
incumbent refresh, good-side quartile, provisional re-evaluation), never removes it: there is no
common-mode cancellation of conditions in the eliminate-decision.
**So what / then what:** Measure challenger and incumbent as **time-adjacent pairs** (A/B/A/B…)
and eliminate on the **paired delta + a confidence interval**, not on an absolute percentile.
Weather hits both halves of a pair equally, so it cancels inside the difference — a bad moment
can no longer sink a real competitor. Pairs with effect-size/CI so we stop as soon as the delta
is decisively for or against, not after a fixed iteration budget. This is the gold-standard
de-confounder and supersedes the vs-weather column as the *actionable* weather control.

**METRIC-BASED "VS WEATHER" (stop inferring conditions from the profiles under test).**
Today's "vs weather" baselines each run against the rolling ±2h median of *all other profiles'
runs* (`trends.rolling_baseline_deltas`). That baseline is contaminated by *which profiles we
chose to run*: a 2h window full of bad profiles pushes the median down, so a mediocre profile in
that window shows a falsely-high "vs weather"; a window full of a good sweep does the opposite.
During sweeps/races the window is often dominated by the very candidates under test, so it
measures "did I beat what else I was running," not "did I beat the network." It's also display-
only, so even when right it changes nothing. The fix is to make weather **per-run and
self-contained**: every run already co-measures the weather right next to the crown metrics.
**So what / then what:** Use the signals each run co-measures instead of the neighbor pool.
- **Arithmetic decomposition for FCP/LCP (no model).** `fcp`/`lcp` are ledger role **O**
  *milestone sums* that the nav waterfall (role **N**) already splits into
  `nav_dns + nav_tcp + nav_tls + nav_request + …`. Those setup phases are RTT-dominated path
  weather measured *on the load's own socket*. So `weather_adjusted_fcp = fcp − (nav_dns +
  nav_tcp + nav_tls)` strips the ambient connection weather per-run, no neighbors, no model.
- **Covariate adjustment for `stall_energy` (role S — doesn't decompose).** Regress it on the
  clean weather covariates over long-history / high-iteration profiles; take the residual
  (observed − predicted) as the weather-adjusted value. Fit robustly + deterministically.
- **Covariate hygiene (the one way it breaks):** only adjust for signals the shaper *doesn't*
  move — `nav_dns/nav_tcp/nav_tls`, `latency/jitter/packet_loss`, probe `dns/tcp/tls`. Never
  `download`/`transfer` (bandwidth is a knob) or `nav_response` (SQM-facing delivery), or we
  subtract away real profile effect. Same-socket nav phases beat separate-socket probes.
- **Then:** eventually rank/eliminate on the adjusted values (the real "don't discard a good
  profile that ran in bad weather" win) — once the sensitivity numbers justify it.
- **DONE (step 2):** display-only `weather_adjusted_overall` — the Overall re-cornered over
  setup-stripped fcp/lcp (each run's own `nav_dns+nav_tcp+nav_tls` subtracted; stall_energy raw),
  per-run and self-contained, in the same percentile space as Overall. Surfaced as the
  **"Weather-adj"** column in Settings-Impact (sortable, next to "vs weather"). Not a crown input.
- **DONE (step 1):** `GET /api/settings/weather-sensitivity` (`routes_settings._weather_sensitivity`)
  — the deterministic validation block: per-covariate × crown-metric Spearman ρ, pooled **and**
  within-profile (holds the profile fixed = the causal signal), so we can see which crown metrics
  are actually weather-sensitive and which clean covariates to build the adjustment from before
  wiring anything into the score. Informational only.
- A **fixed reference-profile control probe** (measured on a cadence, express every profile vs the
  reference nearest in time) is a heavier alternative if the co-measured covariates prove too weak
  — related to the interleaved-A/B idea, where the incumbent is already a partial reference.

**MAGNITUDE-AWARE CROWN / HEIR CEILING (percentile rank is magnitude-blind).** The crown and the
heir/optimistic ceiling rank on **percentile within the field**, which by design gives every
metric equal spread (so no one metric dominates the corner) — but it's magnitude-blind: a 1 ms
edge and a 200 ms edge both count as "one rank better." A profile with a large real improvement
that happens to be rank-adjacent to its neighbours gets no credit for the size of the win, and a
thin-sample profile just gets a flat +5-point percentile bump rather than a magnitude-aware
estimate.
**So what / then what:** Explore a hybrid that keeps percentile's equal-spread property but folds
in effect size — e.g. blend the rank with a calibrated magnitude term, or rank on
weather-adjusted absolute values once the control above exists. Deferred deliberately: the
equal-spread property is load-bearing (it's why one outlier metric can't steamroll the corner),
so any change has to preserve that.
