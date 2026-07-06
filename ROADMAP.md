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

**INDEPENDENT NETWORK-WEATHER CONTROL (stop inferring conditions from the profiles under test).**
"vs weather" baselines each run against the rolling ±2h median of *all other profiles' runs*
(`trends.rolling_baseline_deltas`). That baseline is contaminated by *which profiles we chose to
run*: a 2h window full of bad profiles pushes the median down, so a mediocre profile in that
window shows a falsely-high "vs weather"; a window full of a good sweep does the opposite. During
sweeps/races the ±2h window is often dominated by the very candidates under test, so the stat
measures "did I beat what else I was running," not "did I beat the network." It's also display-
only, so even when it's right it changes nothing.
**So what / then what:** Introduce a real, profile-independent conditions signal — e.g. a **fixed
reference profile measured on a cadence** as the control, and express every profile relative to
the reference measured *nearest in time*. Then weather-adjust the raw crown metrics **before**
ranking/elimination instead of showing a confounded column after the fact. (Closely related to
the interleaved-A/B idea: the incumbent already acts as a partial reference — this generalizes it
to a dedicated control so the adjustment is trustworthy even outside a race.)

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
