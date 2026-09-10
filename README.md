<h1 align="center">PathBrain</h1>

<p align="center">
  <b>An empirical optimizer for how your Internet actually <i>feels</i>.</b><br>
  It doesn't ask "is your ping low?" — it asks <i>"does the Internet actually <b>feel</b> faster?"</i><br>
  …then tracks that score over time and correlates it with your network settings
  (OPNsense FQ-CoDel / SQM being the first-class integration).
</p>

<p align="center">
  <img alt="OPNsense" src="https://img.shields.io/badge/firewall-OPNsense-D94F00?logo=opnsense&logoColor=white">
  <img alt="SQM" src="https://img.shields.io/badge/SQM-FQ--CoDel-blueviolet">
  <img alt="Python" src="https://img.shields.io/badge/python-3.11+-3776AB?logo=python&logoColor=white">
  <img alt="FastAPI" src="https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white">
  <img alt="React" src="https://img.shields.io/badge/UI-React%20%2B%20MUI-61DAFB?logo=react&logoColor=black">
  <img alt="Docker" src="https://img.shields.io/badge/deploy-Docker-2496ED?logo=docker&logoColor=white">
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green">
</p>

---

## What is PathBrain?

PathBrain measures the one thing common tools don't: **how responsive your
Internet connection actually *feels*.** It loads real pages in a real browser, scores
them on **Responsiveness / Smoothness / Speed** plus a single first-class **Overall**,
and tracks all of it over time — so you can finally answer *"when was the Internet
fastest?"* and *"did that change make it feel better or worse?"* with data instead of
folklore.

Most tools optimize for **ping**, **throughput**, or synthetic scores. None of
those reliably answer what a human cares about: *when I click something, how fast
does it feel?* PathBrain is built for exactly that — and deliberately keeps raw ping
from dominating.

Where it gets powerful: PathBrain can **correlate your score with the network
settings that were live when each run ran.** Its first-class integration is the
**[OPNsense](https://opnsense.org/) API**, which it uses to discover your
**FQ-CoDel / SQM** traffic-shaper configuration (bandwidth, quantum, limit,
target, interval, ECN, flows, …). That turns the eternal SQM question — *what
settings are actually best?* — into an empirical, measured answer.

- **No firewall?** It's still a first-class responsiveness tracker for your connection.
- **Running OPNsense SQM?** You also get settings-vs-responsiveness correlation and
  **closed-loop tuning**: propose a candidate, apply it, benchmark it, adjudicate it
  head-to-head against the incumbent under shared weather, and — once you arm it — keep
  the firewall on whichever profile the verdict you chose says is best. Every session
  snapshots first and restores afterwards; only the crown follower writes to keep.

> The provider layer is pluggable (pfSense / Linux `tc` can follow), with OPNsense
> traffic shaping as the first-class integration.

> **Philosophy:** Empirical. Never assume. Never rely on folklore. Every
> optimization is tested, measured, scored, and historically tracked.

### The score: three perceptual axes (methodology `speed-smoothness-v16`)

PathBrain scores the **three temporal phases of a page load** as independent
0–100 axes, rather than blending them into one number. (The original single
*Seat of Pants Score* was split into these — SOPS is now legacy.) Each metric is
normalized to a 0–100 subscore against configurable *best/worst* thresholds
(perception-calibrated log curve), then weight-averaged within its axis:

| Axis | Answers | Metrics (weights) |
| --- | --- | --- |
| **Responsiveness** | How fast does the *first* content appear? | byte-earliness (30) · FCP (25, required) |
| **Smoothness** | How steadily does it fill in (minimized wait)? | longest-stall (40, required) · network-stall-all (30, required) · cadence (15) · evenness (15) |
| **Speed** | How soon is it *fully visible + interaction-ready*? | LCP (40, required) · INP (40) · render (20) · load-event (20) |

Plus a **secondary** axis: **Stability** (layout shift / CLS) — diagnostic only,
never folded into the headline axes since it barely moves human feel.

Everything scored is **browser-derived**, on purpose. The crown is FCP × LCP ×
network-stall-all, and the five network probes (ICMP/DNS/TCP/TLS/HTTP) never fed it —
they ran alongside, costing about half of every run's wall clock, for a Completion
axis the docs already described as barely moving human feel. So a run now **measures
only what the methodology requires** (`measurement.methodology_only`, default on) and
v16 drops the probe-supplied metrics from the rubric, so old and new runs grade on one
definition instead of every new run reading *partial*. Weather still gets its
covariates — from the browser's own `nav_dns`/`nav_tcp`/`nav_tls`/`nav_request` phases.

**Overall** is a single higher-is-better roll-up — and since `speed-smoothness-v5`
it's a **first-class, versioned, persisted** quantity, not just a presentation
measure. Since **v15** it's a **weighted average** of the perception-calibrated
subscores over a small, hand-picked **crown metric set**: **FCP (1) · LCP (1) ·
network-stall-all (0.5)** — shows initial progress fastest × reaches "main content
visible" fastest × spends least time stalled on the network. FCP and LCP are *native*
browser paint timestamps; `network_stall_all` is the summed duration of every
network-attributed inter-resource gap with **no minimum-gap floor**, so it counts the
sub-perceptible RTT/handoff gaps a page load on fiber is actually made of (render-covered
time excluded via LoAF overlap, so it isolates the shapeable share). It is deliberately
below human perception: the objective is to *crown the best profile* by measured network
dead-air, not to gate on human-noticeable hitches.

Why weighted and not the corner it used to be: on a fast link ~149 profiles packed into
a few milliseconds, and the field-percentile corner carried a **±17-point standard error**
— the top ~66 were a statistical tie no amount of running could break, because rank
normalization *manufactured* noise that wasn't in the raw milliseconds. A magnitude-aware
weighted average pins a profile's median Overall to ~±1 and the field separates. The
per-metric percentile columns stay for display, so the raw ranking is still legible
beside the graded verdict.

The crowned **"best"** profile is the confident profile (≥ `min_iterations` total) with
the **highest Overall** — a deterministic argmax, no hysteresis. A photo finish is
*labelled*, not smoothed: a lead must clear both an absolute floor and `crown_tie_sigma`
× the pooled **standard error of the medians**, so collecting runs can break a tie
instead of freezing it.

Design choices:

- **The journey beats the endpoint.** Smoothness isolates *how* the page filled in
  (the network layer you can actually tune), kept distinct from when it started
  (Responsiveness) and when it finished (Speed).
- **Missing metrics never penalize.** If a metric is unavailable (e.g. a paint
  metric where the browser engine didn't run), its weight is redistributed across the
  metrics that *are* present within the axis.
- **Unmeasurable is never a value.** A metric the raw genuinely can't supply is
  **omitted**, never defaulted — a fabricated `0` on a lower-is-better crown leg would
  out-rank real measurements, which is exactly how a crowned profile once slid to 65th
  as honest runs arrived. A run that can't produce a required metric is quarantined
  *incomparable* instead.
- **Axes are never blended.** Each is reported and ranked on its own.

Plugins are **pure sensors** that store raw observations; all interpretation
(jitter = stddev of pings, byte-earliness = area over the cumulative-bytes curve,
the axis scores themselves) lives in a separate, **versioned methodology** layer —
so a new metric, a re-weight, or an axis re-partition is published as a new
methodology version and re-graded over history straight from raw, without
re-collecting (`POST /api/score/regrade`). `speed-smoothness-v16` is the
published-now version. The **sites and the browser client are part of the methodology
too** — a site hands an automated 800×600 headless shell a different page than it hands
a person, so changing either is a publish that forks a version and quarantines runs
measured against the old collection. A too-lenient threshold can be **re-anchored from
the UI** (`POST /api/methodologies/reanchor` forks the current version with a tightened
`best` and re-grades), and every shaper field PathBrain reads/writes/sweeps is
declared once in a single **`shaper_fields` registry** so identity/writable/sweepable
can't drift apart.

### Two verdicts, deliberately allowed to disagree

The pooled crown above is an **observational** statistic: it averages runs taken at
different times under conditions nobody held equal. That is the right way to rank a
whole field cheaply, and the wrong way to settle a close question — so PathBrain runs a
second, **controlled** verdict beside it and shows both.

The **duel ladder** fights profiles head to head in *rounds*: two legs back to back,
counterbalanced (the lead alternates every round), each leg medianing several
iterations. Adjacency is the whole instrument — weather hits both legs of a round and
cancels inside the margin — and it isn't merely assumed: every leg is stamped with its
measured weather severity and the round records the **shift** between them. Bouts are
decided by a one-sided Wilcoxon signed-rank test on the paired margins with a
simulation-fitted peek penalty, plus "if it wins back to back at a length that isn't
luck, it wins". The standings rank on a **Bradley–Terry** fit over the whole ledger, so
beating a strong profile counts for more than beating an unmeasured one and profiles
that never met are comparable through shared opponents — ordered by the *conservative
floor* (rating − 1 SE), because a claim to be best has to be earned. The **belt** is
separate and lineal: you take it by beating whoever holds it.

Duel *runs* flow into the pooled record like any others; duel *verdicts* live beside it
and never enter the pooled score. Which verdict actually governs what gets written to
the firewall is a single first-class setting — the **crowning policy** — and the two
naming different profiles is not a bug to reconcile but the most informative thing on
screen.

---

## Status — what works today ✅

- 🔌 **Plugin benchmark engine** — seven registered benchmarks (**pure sensors** that
  store raw observations only, so every metric can be re-derived from history without
  re-measuring): `icmp` (per-ping RTT series), `dns` (per-resolver lookup), `tcp` (connect),
  `tls` (handshake), `http` (TTFB / bytes / timing), `browser` (real-Chromium nav/paint
  timing, Resource Timing + Long Animation Frames, an optional filmstrip, and a
  repeat-visit warm load), and `portable` (the Away test's recipe run at home, the
  per-profile reference for away runs). Which of them a run actually executes is decided by
  the methodology, not by the plugin list.
- 🧮 **Three perceptual axes** — perception-calibrated **log curve** (Weber–Fechner):
  **Responsiveness** (time-to-first), **Smoothness** (the steady fill, led by byte-
  arrival metrics — longest-stall/network-stall/cadence/evenness), and **Speed** (time-to-
  last + interactive), plus a first-class **Overall** (v16: a weighted average over
  FCP · LCP · network-stall-all). Raw-only collection + a **versioned methodology**:
  `POST /api/score/regrade` re-scores history from raw under any published methodology —
  without re-collecting. The **sites and the browser client** are part of that version too,
  so changing either is a publish that quarantines runs measured against the old collection
  rather than silently averaging two different measurements together.
- 🌦️ **Historical trends + "vs typical"** — per-metric baselines by day-of-week ×
  hour-of-day (`/api/trends/*`); the Dashboard, a dedicated **Trends** page, and
  **Settings Impact** read each result *relative to its historical norm* ("wins
  above replacement"), so a config is judged fairly for the times it actually ran.
- ⛅ **Measured weather, not the clock** — every run co-measures its own conditions
  (probe DNS/TCP/TLS/latency plus the browser's nav setup phases — signals the shaper
  doesn't move), ranked against all history into a 0–100 **severity**. Runs are banded by
  severity and each profile's Overall compared against **other profiles' runs in the same
  band**: "wins above the weather", plus a **weather-beater** flag (delivered average
  outcomes where the field delivered below-average — race it) and a **crown-suspect** alert
  when the residual ranking's #1 isn't the raw crown. Strictly flag-and-steer — a suspect
  triggers a head-to-head race, never a re-rank. The **Weather** page adds the empirical
  gate: covariate × crown-metric correlations (pooled *and* within-profile) beside a
  **variance decomposition** answering "how much of the noise is measurable weather?" —
  read as a ceiling on what any adjustment could remove, whose complement is why the duel
  is paired.
- 🎯 **Shotgun Sweep** — an on-demand grid sweep over the registry's sweepable shaper
  fields (quantum × target today): applies each variant for real, benchmarks it, ranks
  by Overall + "vs typical", and **restores the baseline** at the end (and on startup if
  interrupted). Marking another field sweepable surfaces it end to end — engine *and* UI
  control — with no code branch. Plus a reversible **config write-test**
  (`POST /api/config/test-apply`) to validate the firewall apply path.
- ✈️ **Away test ("vs home")** — a phone-first test any device can run from a plain browser
  tab: a synthetic CDN resource waterfall, a streamed download and warm round trips, scored on
  its own small rubric and compared **only against directly comparable data** — the same device,
  the same test version, home runs on one firewall profile, the nearest time of day with enough
  runs, and only the resources both sides completed. Home is detected by comparing the device's
  public address with PathBrain's own, not declared. PathBrain also runs the same recipe itself
  as a `portable` plugin in every benchmark, so a per-profile, per-hour home baseline is always
  there — shown as a second, labelled reference beside the device's own. Its own table; never
  the pooled crown.
  (A browser tab can't read google.com's paint timing, so this is a different instrument from
  the Chromium test — `/away`, `/api/portable/*`.)
- ⏱️ **Measure only what the methodology requires** — the crown is FCP × LCP ×
  network_stall_all, all browser-derived, so by default a run skips the five probe plugins
  (about half of its wall clock) and measures the crown on every iteration; `measurement.
  methodology_only: false` restores the full suite. Methodology v16 drops the probe metrics
  from the rubric so old and new runs grade on one definition. An **Idle-wait audit** on the
  Methodology page reads stored raw to say whether the post-load settle ever moved LCP.
- 🔁 **Multi-iteration runs** — repeat the suite N times and take the **median**,
  with a per-run **confidence band** (± / range) and an **ETA**. Per-plugin iteration
  caps keep runs fast: the heavy browser runs fewer iterations than the cheap network
  probes and **reuses one Chromium** across them, and unscored captures (screenshot/HAR)
  are off by default — without changing what's scored.
- 📈 **Continuous monitoring + rolling score** — optional scheduler runs the suite
  on an interval; the Dashboard shows a windowed **median (24h) + IQR** so
  "current responsiveness" is stable, not point-in-time noise.
- 🔍 **OPNsense discovery + settings correlation** — each run captures the live
  FQ-CoDel/SQM settings + a **fingerprint**; runs group into **profiles** with their score
  distribution and a **significant-change** banner. Every profile gets a memorable **call
  sign** ("Speedy Sloth", not `q1514 t5ms`) derived deterministically from its fingerprint,
  because 150 profiles differing in one number are unreadable and an unnarratable duel is an
  unread one. The **crowned "best"** is the confident profile with the highest **Overall** —
  so "best" is genuinely *starts fast, loads fast, **and** spends least time stalled*.
  The quadrant is **dynamic** (plot any two numeric fields we collect; the crowned profile is
  ringed; a **Shade** picker encodes a third field as dot **opacity**), it **warns when an
  axis is saturated** (every profile already past the methodology's `best` threshold, so the
  spread carries no score signal), and the **paginated** profiles table (25/page) pins the
  crown metrics as columns — read straight from the methodology, so publishing a new crown
  re-wires the view with no frontend edit — with an optional column selector for anything
  else. A page-level **saturation check** flags a too-lenient threshold and offers a one-click
  **re-anchor**; an **outlier check** flags a profile sitting >3.5 robust SDs off the field
  (on the MAD, not the stddev, which the outlier itself would drag) and offers to hide it from
  the view *or* re-run exactly those profiles, which are different answers to different
  questions. A **"% vs SQM off"** column prices every profile against the honest unshaped
  baseline, so a profile that's *worse* than turning shaping off reads red — and can be hidden.
  A profile is **confident** once its runs total ≥ `correlation.min_iterations` (default **15**).
  Mock provider for offline dev.
- 🥊 **Duel ladder ("Dueling Champions")** — the controlled-trial counterpart to the pooled
  crown: profiles fought head to head in counterbalanced back-to-back **rounds**, so weather
  cancels inside the margin instead of being averaged over. A **ring** runs several challengers
  at once against the belt-holder, matchmaking is re-decided before every bout (always be
  running the bout most likely to unseat the best profile we have), bouts are adjudicated on
  the paired margins by a Wilcoxon signed-rank test with a simulation-fitted peek penalty, and
  the standings rank on a **Bradley–Terry** fit — by its conservative floor, because a claim to
  be best has to be earned. The **belt is lineal** and allowed to disagree with the standings
  and with the pooled crown; the page shows all three side by side and says which edge of the
  triangle the live match is testing. Open matches **survive the window** and resume next
  session with their evidence intact, and a mid-session methodology publish re-seeds the ring
  rather than stranding it. While a session runs, the board puts every profile in the ring —
  belt included — **on one scale** with a sentence per seat saying where it stands and whether
  that's called yet, rather than a column of p-values. Runs nightly, continuously, or on
  demand — and never writes a winner to the firewall.
- 🎚️ **Lever duels** — a bout between two profiles differing in four settings is four questions
  asked at once with one answer. A lever session pins a **campaign base** and fights it against
  **single-lever variants of itself**, generated inside the range that lever has already run on
  this link (a halved queue limit nobody has ever run here is exactly the step that makes the
  connection unusable for the minutes it's measured). Evidence accrues per *transition* — rounds,
  margin, signed-rank p, which crown leg moved — until each lever reads *improves* / *no gain* /
  *open*, and each sits beside its **mechanism prediction** for an unsaturated link, so a lever
  predicted inert that the ring finds moving the Overall is flagged a **surprise**: the model of
  the link is wrong, which is the row worth reading.
- 🧭 **Explore the space** — the one engine that asks what we *haven't* tried. Per-lever response
  curves (marginal *and* reference-conditioned, so confounding is modelled rather than ignored),
  **matched pairs** (profiles differing in exactly one lever — a controlled experiment already
  sitting in the observational record), **local optima** under one-lever moves (several basins
  ≥2 levers apart is the demonstration that the levers are coupled), the holes in coverage, and
  the lever pairs that genuinely interact. From those it ranks **candidates** — real profiles
  with a lever moved somewhere nobody has been — each stating *why* and priced from the best
  evidence available: the duel ring's paired rounds on that exact move first, then a matched
  pair, then the parent's own neighbourhood, then a marginal curve (shrunk when it's flagged
  confounded). **"Test now"** measures one in 5 iterations; **"Run the best bets"** queues the
  top N — ranked at the *pessimistic* end of their band, because optimism decides what to
  explore and pessimism decides what to back.
- 📒 **Was the data right?** — every recommendation is written down **before** it's measured
  (what was proposed, what the model asserted ± its band, and what evidence the claim rested
  on), and the verdict is re-derived from the measured field on every read. So each evidence
  class gets a **measured** track record on your link — do matched pairs really predict better
  than confounded curves? — which then feeds back: the band a bet is ranked on is the *wider*
  of the model's stated band and that class's actual miss.
- 👑 **Follow best** — a single **crowning policy** decides which verdict governs what gets
  written to the firewall (the pooled crown, or the duel's champion), and one component does
  the writing. Crown *tracking* is always on regardless, so the **churn ledger** — how often
  the best profile changes, median reign, changes/day — accrues before you ever arm following.
  That's the number that says whether auto-following would thrash. It refuses to auto-apply
  "SQM off" and any profile the live firewall can't actually be driven to.
- 🔬 **Why it wins** — the crown says *which* profile is best and by how much; a 3-point lead
  could be three points of network stall on every page or a 30 ms LCP edge on one site, and one
  number reads the same for both. This splits any two profiles' gap three ways: by **crown leg**
  (exactly additive under the weighted crown, so the legs sum to the gap), by **navigation phase**
  (request wait is the server answering sooner; response is bytes through the queue; render is the
  machine, not the shaper), and **per site** (everywhere, or one page?). Each delta carries a noise
  bar, and the verdict is one paragraph with its numbers in it.
- 🚦 **Baseline test (SQM off)** — measure the *unshaped* link to see what the shaper is actually
  buying: snapshot each pipe's state, disable shaping everywhere, settle, benchmark, then restore
  — always, in a `finally`. On demand or on a nightly schedule. All SQM-off runs collapse into one
  canonical profile, since the shaper params don't apply when a pipe is off.
- ✏️ **Test what's on the firewall now** — a time-boxed collection loop on whatever profile is
  live, which is the one engine that never writes the firewall at all. It benchmarks in short
  chunks so an interruption keeps every completed chunk; big manual runs chunk the same way.
- ⬆️ **Version awareness + one-click self-update** — the image is stamped with its build
  commit; the app does a cached, best-effort check against the latest commit on `main`
  (`GET /api/version`) and shows an **"Update available"** chip in the top bar when a newer
  `:latest` is pullable. Point it at a **Watchtower** instance and the chip gains an
  **"Update now"** button. Self-update is the one operation that destroys its own evidence —
  a success recreates the container mid-response, so "it worked" and "the connection dropped"
  are the same observation — so every attempt is **persisted before the request goes out** with
  the running build recorded, and resolved *after the restart* by comparing builds:
  **confirmed** (the build changed), **no_change** (Watchtower took the call and nothing
  happened — usually its `--scope` excludes this container), or **failed**. Rendered as an
  update history on the Plugins page. On by default; `PATHBRAIN_UPDATE_CHECK=false` to disable.
- 🔔 **Background jobs + one queue** — long operations run in the background with live
  progress; a top-right dropdown shows every active and recently-finished job — score passes,
  benchmark runs, sweeps, profile tests, duel sessions, races, experiments — in one place.
  **Pressing Run while something else is running always queues**, never 409s: submitting a
  job always succeeds and every button returns the same placement block, so "did anything
  happen?" has one answer whichever button asked. A busy pipeline opens a
  **"Busy now — queue this?"** dialog naming the holder and what's already waiting, and the
  queue **survives a restart** — the person who queued twelve bets for the night pressed the
  button once, and a container recreate used to empty the line with nothing on screen to say so.
  Every job carries an honest **ETA** that says which of four bases it used (a deadline, a
  measured per-iteration cost, the job's own rate, or — for a queued job — the size of the work,
  rendered standing still, because nobody knows when the current holder finishes). Cancelling
  takes effect **within an iteration** and the row says *stopping…* until it lands.
- 🔒 **Firewall/benchmark coordination** — a single lock serializes every
  apply-firewall-and-benchmark session so two never overlap, with **one leader per
  deployment** (an advisory file lock, so two workers can't both schedule and benchmark
  through each other's firewall writes). It's a **lease, not a promise**: holding it for hours
  is normal (a duel window is a night), so a holder that stops *making progress* — a wedged
  browser call, say — is **evicted** by any waiter or by the watchdog, its lease revoked and
  its eventual release made a no-op. A long session also **zipper-merges**: it can let one
  waiting session through at a natural seam, so a "Test now" pressed at midnight doesn't queue
  behind the whole night. Each run re-reads the firewall fingerprint **before and after**
  measuring and is FAILED on drift, so "what we tested" always matches "what we thought".
- 🧾 **Data Dump** — one consolidated JSON export of the last *N* runs, including each
  plugin's **raw observations** per iteration (the per-run view omits raw); view, copy,
  or download (`GET /api/history/dump`).
- 🧪 **Experiment engine** — within a configurable **window**, sweep one shaper
  parameter across candidates, benchmark each, and **restore the pre-window
  baseline** at close (or auto-promote a clear winner). **Disarmed + dry-run by
  default.** Firewall writes go only through `provider.apply()` (experiment, Shotgun
  Sweep, config write-test, profile test, sweep apply-best) — each reversible and
  snapshot/restore, and serialized by the coordination lock above.
- 🛡️ **Run-lifecycle safety** — startup reconciliation, a watchdog timeout and manual cancel
  so a restart or hang never leaves a zombie "running" job. No measurement may park the
  pipeline: a probe runs on a worker thread with a deadline, and on expiry it comes back as an
  ordinary failed measurement while the wedged worker is **abandoned** — one leaked thread and
  one leaked Chromium is the deliberate trade against a dead platform. A firewall call that
  times out is retried before it's allowed to fail anything.
- 🩺 **The app polices its own footprint** — a dropped browser handle raises nothing, logs
  nothing and moves no number PathBrain reports; its only symptom is the *host's* memory hours
  later, as an OOM kill (observed: 224 node drivers at 13 GiB, 887 Chrome processes, 388 of them
  zombies). So process trees are counted straight from `/proc` and reported on
  `GET /api/health/pipeline`; orphaned trees are reaped at points where the caller provably holds
  no browser (never mid-session, which once killed the browser a duel leg was measuring with);
  Chromium is **recycled by age** before it bloats; and a **resource guard** reads the cgroup's
  own memory limit and the host load, grades the pressure, and backs off — reaping while idle,
  asking for a recycle, deferring a scheduled run — so "the NAS is struggling" is a number rather
  than a feeling.
- 🌡️ **Instrument health is part of comparability** — a run measured on a sick machine is not a
  measurement of the link. When the host degrades, the browser itself gets slower, and that lands
  *inside* FCP and LCP through the render phase: the link was fine, the numbers are worse, and the
  profile that happened to be on the firewall wears it in its pooled median forever. So every run
  is graded on quantities the shaper **cannot** move (the browser's own host-side phases plus the
  render metric), as a ratio against the healthy quarter of recent history — deliberately not a
  percentile of the field, because the history being ranked against *is* the contaminated stretch.
  **Degraded** quarantines through the same comparability gate every scored view already filters
  on; **strained** is shown and still counts; "we couldn't tell" never quarantines. The readings
  were always on disk, so **one re-grade heals the record with nothing re-measured** — and the
  threshold is priced first (`GET /api/methodologies/instrument-health` reports how many runs each
  candidate line would quarantine), because a gate nobody can price is a gate nobody should arm.
- 📉 **Instrument drift audit** — when a run gets longer, did the *measurement* get slower, and
  does it move a graded number? A wall clock climbs for two opposite reasons — the run got bigger
  (more iterations measure the crown; ungraded) or the machine got slower (which corrupts grading)
  — and the audit separates them by trending three clocks per run (the suite iteration, the browser
  iteration, and the page's own clock) plus the shaping-immune client metrics against the network
  phases as a control. A **step** is distinguished from a gradual drift and read against what
  changed at that boundary, because a thirds comparison dilutes a real one-day step into a
  meaningless "+6%".
- 📊 **Web dashboard** — React + MUI, dark mode, every route code-split so opening one page
  doesn't parse all of them. **Dashboard** (a status strip of KPI tiles, the 24h Overall gauge,
  the profile the firewall is on now, running jobs, and the three verdicts side by side),
  **History**, **Trends**, **Weather**, **Compare**, **Settings Impact** (the paginated
  profiles table + dynamic quadrant + heirs card), **Dueling Champions**, **Lever duels**,
  **Explore**, **Baseline (SQM off)**, **Away test**, **Experiments**, **Shotgun Sweep**,
  **Config**, **Methodology**, **Plugins**, **Data Dump**, **AI**, plus Profile Detail and Run
  Detail — and a global **jobs** dropdown and **Follow best** switch in the top bar. The pages
  are read on a phone, so control rows wrap and wide tables become stacked lists rather than
  hiding the answer behind a sideways scroll.
- 🤖 **AI suggestions (optional)** — hands an LLM (via OpenRouter) a profile-centric export of
  the field *plus* the relationships computed **server-side**: per-lever × per-crown-metric rank
  correlations, what the top-Overall quartile of profiles runs that the field doesn't, and the
  coverage gaps. The model reasons over an explicit map rather than eyeballing rows, is forbidden
  from inventing statistics, and answers in two steps — first its reading of how each lever moves
  each metric, then suggestions consistent with that. It can also kick back **"go measure here"**
  instead of a speculative profile. Each suggestion has a one-click test (apply → benchmark →
  restore) or a supervised apply. The API key lives in its own config row, isolated from the
  benchmark config so it can never leak into a run snapshot or the data dump.
- 💾 **SQLite persistence** with additive auto-migrations; background execution.

**Next:** the overnight module (alternate *explore* — queue tonight's best bets — with
*adjudicate* — duel the survivors), multi-parameter Bayesian search + hysteresis, routing
intelligence / SD-WAN. Latency-under-load / bufferbloat is explicitly **out of scope**:
saturating the link measures a state the household is almost never in. See
[`ROADMAP.md`](ROADMAP.md) for the reasoning behind each.

---

## Quick start (Docker)

The whole stack runs as a **single container** — the API serves the built UI.

### Option A — pull the pre-built image (recommended)

A GitHub Action publishes a ready-to-run image to the **GitHub Container
Registry** on every push. No source checkout, no build:

```bash
# Grab just the compose file
curl -O https://raw.githubusercontent.com/jmorganthall/pathbrain/main/docker-compose.ghcr.yml

docker compose -f docker-compose.ghcr.yml up -d
```

Update later with `docker compose -f docker-compose.ghcr.yml pull && docker compose -f docker-compose.ghcr.yml up -d`.
Pin a release by changing `:latest` to a tag like `:v0.1.0`.

> If the GHCR package is **private**, log in once first:
> `docker login ghcr.io -u <you> -p <token-with-read:packages>`.

### Option B — build from source

```bash
git clone https://github.com/jmorganthall/pathbrain.git
cd pathbrain
docker compose up --build
```

Then open **http://localhost:8000** and click **Run Benchmark**.

Persistent state (SQLite DB, snapshots, browser artifacts) lives in the
`pathbrain-data` Docker volume, so it survives restarts and image rebuilds.

### `docker-compose.yml`

The bundled compose file (excerpt) — point it at your firewall by setting the
`PATHBRAIN_*` variables (see [Configuration](#configuration)):

```yaml
services:
  pathbrain:
    build: .                       # or: image: pathbrain:latest
    container_name: pathbrain
    restart: unless-stopped
    ports:
      - "8000:8000"
    environment:
      PATHBRAIN_DATABASE_URL: "sqlite:////data/pathbrain.db"
      PATHBRAIN_LOG_LEVEL: "INFO"
      PATHBRAIN_CONFIG_PROVIDER: "${PATHBRAIN_CONFIG_PROVIDER:-mock}"
      PATHBRAIN_OPNSENSE_URL: "${PATHBRAIN_OPNSENSE_URL:-}"
      PATHBRAIN_OPNSENSE_API_KEY: "${PATHBRAIN_OPNSENSE_API_KEY:-}"
      PATHBRAIN_OPNSENSE_API_SECRET: "${PATHBRAIN_OPNSENSE_API_SECRET:-}"
      PATHBRAIN_OPNSENSE_VERIFY_TLS: "${PATHBRAIN_OPNSENSE_VERIFY_TLS:-false}"
    volumes:
      - pathbrain-data:/data

volumes:
  pathbrain-data:
```

> **Unraid:** the simplest path is the **Docker Compose Manager** plugin —
> create a stack from [`docker-compose.ghcr.yml`](docker-compose.ghcr.yml) and
> drop a `.env` file (copied from [`.env.example`](.env.example)) next to it;
> Compose auto-loads it. Publish port `8000` and keep the single `/data` volume.
> Because PathBrain measures *your* path to the Internet, run it on the network
> whose responsiveness you want to score.
>
> **Resource guardrails.** The compose files set `init: true`, `mem_limit: 4g` and
> `pids_limit: 2048` so a runaway browser is contained inside PathBrain instead of
> taking the host with it. If you run the image some other way (an Unraid template,
> plain `docker run`), pass the equivalents yourself:
> `--init --memory=4g --pids-limit=2048`. The image ships `tini` as PID 1 regardless,
> so zombie reaping never depends on the flags. `GET /api/health/pipeline` reports the
> live process counts (`processes.drivers` should read 0 between measurements).

---

## Configuration

PathBrain separates **infrastructure** config (env-only) from **runtime**
benchmark config (DB-backed, editable live).

### Infrastructure (environment variables)

Copy [`.env.example`](.env.example) to `.env` and edit. Most are prefixed
`PATHBRAIN_` (plus the standard `TZ`).

| Variable | Default | Purpose |
| --- | --- | --- |
| `PATHBRAIN_DATABASE_URL` | `sqlite:///./data/pathbrain.db` | SQLAlchemy DB URL (Postgres later) |
| `PATHBRAIN_ARTIFACT_DIR` | `./data/artifacts` | Browser screenshots / HAR files |
| `PATHBRAIN_HOST` / `PATHBRAIN_PORT` | `0.0.0.0` / `8000` | Bind address / port |
| `PATHBRAIN_LOG_LEVEL` | `INFO` | Log verbosity |
| `TZ` | `UTC` | Local timezone for the experiment **window** hours (and logs) |
| `PATHBRAIN_CONFIG_PROVIDER` | `mock` | `mock` or `opnsense` |
| `PATHBRAIN_OPNSENSE_URL` | — | OPNsense base URL, e.g. `https://192.168.1.1` |
| `PATHBRAIN_OPNSENSE_API_KEY` | — | OPNsense API key |
| `PATHBRAIN_OPNSENSE_API_SECRET` | — | OPNsense API secret |
| `PATHBRAIN_OPNSENSE_VERIFY_TLS` | `false` | Verify the firewall's TLS cert |
| `PATHBRAIN_OPNSENSE_TIMEOUT_S` | `30` | Per-call HTTP timeout (one attempt; calls are retried) |
| `PATHBRAIN_UPDATE_CHECK` | `true` | Check whether a newer build is pullable |
| `PATHBRAIN_GIT_SHA` | — | Build commit, stamped into the image by CI |
| `WATCHTOWER_URL` / `WATCHTOWER_TOKEN` | — | Watchtower HTTP API, for the "Update now" button (deliberately **unprefixed**) |

**Example `.env`:**

```dotenv
# Storage
PATHBRAIN_DATABASE_URL=sqlite:///./data/pathbrain.db
PATHBRAIN_LOG_LEVEL=INFO

# Use the live firewall instead of the mock provider
PATHBRAIN_CONFIG_PROVIDER=opnsense
PATHBRAIN_OPNSENSE_URL=https://192.168.1.1
PATHBRAIN_OPNSENSE_API_KEY=your_api_key_here
PATHBRAIN_OPNSENSE_API_SECRET=your_api_secret_here
PATHBRAIN_OPNSENSE_VERIFY_TLS=false
```

> **OPNsense permissions.** Create the API key/secret under **System → Access →
> Users → (your user) → API keys**. The user needs **traffic-shaper read** access
> (page privilege **"Firewall: Shaper"**, and/or **"System: Settings: Traffic
> Shaper"**), or use an admin account — without it discovery returns 403. Everything
> that measures a *different* profile than the one you're on additionally **writes** to
> the shaper (`setPipe` + `reconfigure`): profile tests, the challenger race, the duel
> ladder, lever sessions, profile re-runs, the Shotgun Sweep, the experiment engine, the
> config write-test, and the baseline test (which toggles the pipes off and back on).
> Each snapshots the baseline and restores it — always, including on startup after an
> interrupted session — and the experiment engine is disarmed + dry-run by default. The
> only deliberately one-way writes are the supervised "Apply this profile" and, once you
> arm it, **Follow best**: being on the crown *is* the intended steady state, so there's
> nothing to restore.

### Runtime (DB-backed, edit on the Config page or `PUT /api/config`)

All of this is stored in the database and deep-merged over defaults, so the first
run needs no setup:

- **Benchmark targets** — ICMP/DNS/TCP/TLS/HTTP/browser hosts & URLs. While a published
  methodology owns the site list and browser client, the corresponding editors go read-only
  and point at the Methodology page — changing what you measure is a **publish**, not a config
  edit.
- **`iterations`** — suite repeats per run; each headline metric is the **median**.
- **`measurement`** — `methodology_only` (default on): skip every plugin the current
  methodology doesn't require, and lift the browser's per-plugin cap so every iteration
  measures the crown.
- **`monitoring`** — `enabled`, `interval_minutes`, `run_timeout_minutes` (watchdog),
  `probe_timeout_minutes` (the deadline after which a wedged probe is abandoned).
- **`correlation`** — `significant_change_pct`, `min_iterations` (default 15; the
  total-iterations bar a profile must clear to count as confident), `crown_tie_sigma` and
  `crown_tie_min_margin` (when a lead is a real lead rather than a photo finish),
  `crown_window_iterations` (the "Overall (recent)" drift lens).
- **`instrument`** — the instrument-health gate: `enabled`, `strained_ratio`,
  `degraded_ratio`, `min_quantities`, `baseline_runs`. Price a threshold with
  `GET /api/methodologies/instrument-health` before moving it.
- **`browser`** — the client (`headless_mode`, `user_agent`, `viewport`, `locale`,
  `timezone_id`, `hide_automation`), `iterations`, `networkidle_timeout_s`, `warm_loads`
  (the repeat-visit load), and recycling (`recycle_after_pages` / `recycle_after_minutes`).
- **`duel`** — the ladder: schedule (`enabled`/`hour`/`minute`/`timezone`/`duration_minutes`,
  or `continuous` + `continuous_gap_minutes`), the ring (`seats`, `belt_every`,
  `iterations_per_round`, `settle_seconds`, `browser_only`), and the stopping rule — pick a
  **preset** (snap / quick / balanced / strict, each labelled with its *measured* behaviour)
  rather than hand-editing `p1`/`alpha`/`min_pairs`/`max_pairs`, since "when is someone the
  winner?" is one question that had been spread across six interacting fields nobody could
  reason about together.
- **`crown_follow`** — `enabled` (arm the firewall write), `policy` (pooled or duel),
  `ranking`, `interval_minutes` (the backstop full check).
- **`baseline_test`** — the nightly SQM-off test: `enabled`, `hour`, `minute`, `iterations`,
  `settle_seconds`.
- **`portable`** — the Away test's recipe, `min_home_runs`, `home_ip`/`home_ipv6_prefix`
  overrides, and `enabled`/`iterations` for the in-suite `portable` plugin.
- **`trends`** — `lookback_days`, `window_hours`, `min_samples` (historical baselines).
- **`rubric_version` / `methodology_version` / `weights` / `thresholds`** — the scoring
  rubric. After editing, **re-grade history** to keep the timeline comparable.
- **`experiment`** — `enabled`, `dry_run`, `auto_promote`, `param`, `candidates`,
  `window` (days/hours, container `TZ`), `dwell_minutes`, `min_trials_per_value`,
  `improve_pct`. Disarmed + dry-run by default.

Run results show per-metric **median ± stdev** and a confidence band; an ETA is estimated
from the *recent* per-iteration cost (recent-first, iteration-weighted median), because after
anything changes what a run does, a run from ninety minutes ago describes a different job.

---

## API reference

Interactive docs are served at `/docs` (Swagger) and `/redoc`. Base path: `/api`.

Grouped by area; this is the useful subset, not the full surface.

**Runs, scoring & the methodology**

| Method & path | Description |
| --- | --- |
| `POST /api/run` | Trigger a benchmark suite (body: optional `iterations`) |
| `POST /api/runs/{id}/cancel` | Cancel a run — takes effect before the next iteration |
| `GET /api/runs/estimate` | Per-iteration cost from recent runs (ETA), with the tier it used |
| `GET /api/results/latest` / `…/{id}` | Latest / specific run detail (poll while running) |
| `GET /api/history` / `…/count` / `…/series` | Paginated runs / total / time-series for charts |
| `GET /api/history/dump` | Consolidated JSON of the last `limit` runs incl. raw observations |
| `GET /api/score/{id}` / `…/rolling` / `…/weights` | Run score / windowed median + IQR / weights |
| `POST /api/score/regrade` | Re-score history under the current methodology (background job) |
| `POST /api/score/rescore` · `…/rederive` | Re-grade from cached scalars / re-derive from raw |
| `GET /api/methodologies` / `…/{version}` / `…/current` | Published versions and their frozen definitions |
| `POST /api/methodologies/set-current` | Pin / adopt / clear the active methodology version |
| `POST /api/methodologies/sites` | Publish the site list + browser client (forks a version) |
| `POST /api/methodologies/reanchor` | Fork the current version with one threshold tightened, then re-grade |
| `GET /api/methodologies/instrument-health` | Price the instrument-health gate: baseline, spread, runs each threshold would quarantine |
| `GET /api/methodologies/instrument-drift` | Did the *measurement* get slower, and does it move a graded number? |
| `GET /api/methodologies/warm-agreement` | Cold vs warm (repeat-visit) crown: do they rank the field the same? |
| `GET /api/methodologies/idle-audit` | The smallest post-load settle that would still have caught every late LCP |
| `GET /api/runs/{id}/verify-derivation` | Read-only audit: re-derive from raw and diff against what's stored |

**Profiles, the crown & the field**

| Method & path | Description |
| --- | --- |
| `GET /api/settings/profiles` | Per-profile scores, crown metrics, heirs, ties, weather, outliers |
| `GET /api/settings/impact` | Significance of the latest settings change |
| `GET /api/settings/crowns` | Both verdicts side by side (pooled crown + duel champion) |
| `GET /api/settings/crown-follow` · `POST` · `…/sync` | Crowning policy + churn ledger / arm following / check now |
| `GET /api/settings/profiles/{fp}/why` | Where a win lives: by crown leg, navigation phase and site |
| `PUT /api/settings/profiles/{fp}/name` | Rename a profile's call sign |
| `GET /api/settings/weather-sensitivity` | Covariate × crown-metric correlations + the variance decomposition |
| `GET /api/trends/heatmap` / `…/relative` | Day-of-week × hour-of-day baselines / "vs typical" |
| `GET /api/settings/export/optimizer` | Profile-centric export + the server-computed settings→outcome map |

**Sessions that touch the firewall** (all queue; all snapshot and restore)

| Method & path | Description |
| --- | --- |
| `GET /api/queue` · `POST /api/queue/{id}/cancel` | The one "can I start?" read / drop a queued ticket |
| `GET /api/schedule` | What is scheduled to run next, across every engine |
| `POST /api/settings/test-profile` · `…/queue` · `…/{id}/cancel` | Test a profile (top-up or exact) / who's waiting / cancel |
| `POST /api/settings/test-settings` · `…/apply-settings` | Test arbitrary settings (restores) / apply them (one-way) |
| `POST /api/settings/apply-profile` | Write a stored profile to the firewall (`preview` for a dry diff) |
| `POST /api/settings/race` · `GET` · `…/cancel` | Challenger Race: adaptive time-boxed elimination |
| `POST /api/settings/refresh` · `…/preview` · `…/cancel` | Re-run profiles (winner-first top-N, or a named set) |
| `POST /api/current/test` · `…/cancel` | Test whatever profile is live, for X minutes (never writes) |
| `POST /api/baseline/test` · `GET /api/baseline/config` | Test the unshaped link / the nightly SQM-off schedule |
| `POST /api/sweep` · `…/preview` · `…/{id}/cancel` · `…/apply-best` | Shotgun Sweep over the sweepable fields |
| `GET /api/experiments` · `POST /api/experiments/abort` | Experiment status / abort and restore |
| `POST /api/config/test-apply` | Reversible write-path test (nudge quantum +1, then restore) |

**The duel ladder & lever campaigns**

| Method & path | Description |
| --- | --- |
| `POST /api/duel/start` · `…/cancel` · `GET /api/duel/status` | Start / cancel / poll a session (live ring board) |
| `GET /api/duel/standings` | The league table: Bradley–Terry rating, proven floor, W–L–D, ties |
| `GET /api/duel/card` | Who would fight, in order, and why — built by the engine's own queue |
| `GET /api/duel/history` · `…/profile/{fp}` | The bout tape / one profile's slice of the ladder |
| `GET /api/duel/config` · `PUT` | The window and the stopping rule (presets + advanced) |
| `GET /api/duel/weather-distance` | Does weather shift more between legs further apart? (priced from history) |
| `GET /api/levers/campaigns` · `POST` · `…/{id}` · `…/close` | Lever campaigns pinned to one base |
| `GET /api/explore/levers` | The lever ledger: per-transition evidence vs its mechanism prediction |

**Explore & the recommendation ledger**

| Method & path | Description |
| --- | --- |
| `GET /api/explore/landscape` | Response curves, matched pairs, basins, gaps, interactions, candidates |
| `POST /api/explore/test` | Measure one candidate (5 iterations, or top up to confidence) |
| `POST /api/explore/test-batch` | Queue the top N **bets** (ranked pessimistically, calibrated) |
| `GET /api/explore/recommendations` | "Was the data right?" — every claim graded by evidence class |

**Away test (portable)**

| Method & path | Description |
| --- | --- |
| `GET /api/portable/recipe` | The recipe (CDN resources, stream, RTT probe) + `instrument_version` |
| `GET /api/portable/home` | Home detection: the device's egress vs PathBrain's own, both families |
| `POST /api/portable/runs` · `GET` · `DELETE …/{id}` | Upload a run (derived + compared server-side) / history / delete |
| `GET /api/portable/standings` | What your phone measured, per profile — and whether it agrees with the crown |

**Config, ops & health**

| Method & path | Description |
| --- | --- |
| `GET /api/config` / `PUT` / `POST …/reset` | Read / update / reset runtime benchmark config |
| `GET /api/config/provider` · `POST …/discover` · `GET …/snapshots` | Provider health / discover + snapshot / stored snapshots |
| `GET /api/jobs` | Active + recently-finished jobs (powers the status dropdown) |
| `GET /api/health` · `GET /api/health/pipeline` | Liveness / lock owner, stall, queue, probes, processes, pressure, pool |
| `GET /api/monitoring` · `GET /api/plugins` · `GET /api/metrics` | Scheduler status / registered plugins / the metric catalog |
| `GET /api/version` · `POST …/refresh` | Build commit + "newer build available" (cached; force a re-check) |
| `POST /api/update/trigger` · `GET …/log` · `POST …/test` | Self-update via Watchtower / the attempt ledger / probe reachability |
| `GET /api/ai/config` · `PUT` · `POST /api/ai/suggest` | The optional LLM optimizer (key stored in its own isolated row) |

---

## Running from source

**Backend** (FastAPI):

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn pathbrain.main:app --reload --host 0.0.0.0 --port 8000
```

**Frontend** (Vite dev server, proxies `/api` → `:8000`):

```bash
cd frontend
npm install
npm run dev          # http://localhost:5173
```

**Tests:**

```bash
cd backend
pip install -r requirements-dev.txt
python -m pytest
```

---

## Project structure

```
backend/pathbrain/
  main.py            FastAPI app (serves UI; startup reconcile + resume; /artifacts)
  config.py          Env-driven infrastructure settings
  database.py        SQLAlchemy engine/session + additive SQLite migrations; pool sizing
  models.py          ORM: Run, BenchmarkResult (+raw), Score, Methodology, ConfigSnapshot,
                     AppConfig, Experiment, Sweep, ProfileTest, ChallengerRace, Duel,
                     LeverCampaign, ProfileRefresh, BaselineTest, CurrentTest, PortableRun,
                     ProfileAggregate, ProfileName, CrownEvent, ExploreRecommendation,
                     QueuedJob, UpdateAttempt

  — measurement —
  runner.py          Run orchestration; median aggregation; read-before/after integrity;
                     reconcile/watchdog/rescore/rederive; cancel between iterations
  plugins/           Benchmark plugins + registry (base.py) — pure sensors (raw only)
  interpret/         Raw observations → metric values (derive.py, smoothness, waterfall,
                     portable), versioned
  probes.py          Bounded probe execution — no measurement may park the pipeline
  metrics.py         Single source of truth for metrics + the W/N/C/S/O role ledger
  methodology.py     The published, versioned, append-only rubric + comparability gate
  scoring/           The generic score primitive (perception-calibrated log curve)

  — the record —
  settings_profile.py  Normalize/fingerprint/summarize profiles; the pooled crown
  profile_aggregates.py  Per-profile rollup: the layer between the run and the profile
  profile_names.py   Deterministic, persisted call signs for profiles
  instrument_health.py  Was the machine well when this run was measured?
  instrument_drift.py   Did the measurement get slower, and does it move a graded number?
  weather.py         Measured-weather severity + cohort residuals ("wins above the weather")
  trends.py          Day/hour historical baselines + time-adjusted "vs typical"
  drift.py           Is a metric time-stationary enough to rank raw?

  — deciding —
  duel.py            The duel ladder: ring, seats, belt, paired adjudication
  rating.py          Bradley–Terry fit over the head-to-head ledger
  levers.py          Lever duels + campaigns + the lever ledger
  explore.py         The exploration landscape: curves, pairs, basins, candidates, bets
  explore_tracker.py The recommendation ledger — was the data right?
  why.py             Where a win lives (crown leg × navigation phase × site)
  crowning.py        The first-class crowning policy + the field's primary ordering
  crown_follower.py  "Follow best": the one component that writes the crown to the firewall
  warm_agreement.py  Should the crown read the repeat visit? (read-only)

  — sessions that write the firewall —
  coordinator.py     Process-wide lease: serializes apply-firewall + benchmark sessions
  job_queue.py       The universal "add a job" contract; survives a restart
  session_runtime.py What every session shares: firewall retries, failure prose, streaks
  scheduler.py       Daemon thread: watchdog → nightly windows → experiment → monitoring
  schedule.py        What is scheduled to run next, across every source
  profile_test.py    "Test to minimum" / test a profile exactly N times, then restore
  current_test.py    Test whatever is live for X minutes (never writes)
  baseline_test.py   Test the unshaped link (SQM off), then restore every pipe
  challenger.py      "Race challengers": adaptive 1-iteration-at-a-time elimination
  refresh.py         Re-run profiles (winner-first top-N); seeds the field after a publish
  sweep.py           Shotgun Sweep: on-demand grid sweep, applies + restores baseline
  experiment.py      Window-gated autonomous shaper sweep

  — the host —
  resource_guard.py  Reads the cgroup's own limits; reaps, recycles and defers under pressure
  browser_procs.py   Process-tree accounting + orphan reaping
  jobs.py            In-process job registry (progress + ETA) for the /api/jobs feed
  iteration_cost.py  What an iteration costs, read recent-first
  updates.py         Version awareness, Watchtower self-update, and the attempt ledger

  api/               REST routers (one module per resource)
  providers/         Config discovery + apply (opnsense.py, mock.py)
  shaper_fields.py   Single source of truth for the SQM field model
  portable.py        The Away test: recipe, home detection, "vs home", burst fairness
  ai.py              Optional LLM optimizer over the server-computed relationship map
frontend/            React + TS + Vite + MUI dashboard (dark mode, code-split routes)
Dockerfile           Playwright base image; tini as PID 1; build UI, serve from API
docker-compose*.yml  Build (.yml) and pull-from-GHCR (.ghcr.yml) deploys
.github/workflows/   docker-publish.yml → ghcr.io/jmorganthall/pathbrain:latest
```

### Extending PathBrain

- **New benchmark:** drop a module in `plugins/`, subclass `BenchmarkPlugin`,
  decorate with `@register`, and return a `PluginResult` with **raw observations
  only** (`raw=…`) — derive the scoreable metrics in `interpret/derive.py`. Plugins
  must never raise for measurement failures — return `success=False` with an `error`.
- **New firewall:** subclass `ConfigProvider` in `providers/` and implement
  `discover()` / `snapshot()` (and `apply()` to support the experiment engine).

---

## Roadmap

- [x] **Phase 1 — Foundation:** benchmark engine, SOPS scoring, history, config
      discovery, REST API, dashboard.
- [x] **Phase 2 — Browser engine:** headless Chromium via Playwright — navigation and
      paint timing, Resource Timing + Long Animation Frames, and an optional filmstrip.
- [x] **Continuous monitoring:** scheduled recurring runs + a windowed rolling
      score (median + IQR) for stable "current responsiveness."
- [x] **Settings-vs-responsiveness correlation:** each run is fingerprinted with the live
      FQ-CoDel/SQM settings; runs group into profiles (confidence gated on **total
      iterations**) with a significant-change banner, a dynamic any-metric quadrant, and a
      sortable table pinned to whatever the current methodology crowns on.
- [x] **Firewall/benchmark coordination + integrity:** one lease serializes every
      apply-and-benchmark session, and each run re-reads the firewall before and after
      measuring (FAILed on drift). Plus a Data Dump export and a background-jobs system.
- [x] **Trajectory-aware scoring + first-class methodology:** raw-only collection and a
      versioned methodology layer; byte-arrival smoothness metrics lead the score. The
      headline split into **Responsiveness / Smoothness / Speed** with a first-class,
      persisted **Overall**; `regrade` re-scores history from raw with no re-collection.
- [x] **Crown intelligence + a unified field model:** the crown is the confident profile with
      the highest Overall; **"Heirs to the crown"** surfaces reachable contenders that could
      dethrone it; saturation and outlier checks with one-click re-anchor / re-run; and every
      shaper field is declared once in a **`shaper_fields` registry** the settings layer,
      providers, sweep, experiment and UI all derive from.
- [x] **Historical trends + measured weather:** day-of-week × hour-of-day baselines and a
      "vs typical" reading; then weather redefined by each run's **own** clean covariates
      rather than by the clock — severity bands, cohort residuals, a crown-suspect alert,
      and a variance decomposition saying how much of the noise is measurable at all.
- [x] **Experiment engine + Shotgun Sweep:** window-gated single-parameter sweeps and an
      on-demand grid sweep, both applying for real and restoring the baseline. Plus a
      reversible config write-test. *Requires OPNsense write access.*
- [x] **Interleaved paired adjudication (the duel ladder):** counterbalanced back-to-back
      rounds so weather cancels inside the margin, adjudicated on the paired margins with a
      peek-corrected signed-rank test, ranked by a Bradley–Terry fit, with a lineal belt and
      a first-class **crowning policy** deciding which verdict governs the firewall.
- [x] **Magnitude-aware crown (`speed-smoothness-v15`) + methodology-only measurement (v16):**
      a weighted Overall over FCP · LCP · network-stall-all that actually separates a field
      packed into a few milliseconds, and runs that measure only what the rubric requires.
- [x] **Exploring the space + a feedback loop:** response curves with confounding modelled,
      matched pairs, coupled basins, ranked candidates and pessimistically-ranked bets — with
      every claim written down before it's measured and graded afterwards by evidence class.
- [x] **The platform polices itself:** bounded probes, an evictable lease, a universal job
      queue that survives a restart, process-tree reaping, browser recycling, a resource guard,
      and **instrument health as part of comparability** — so a run measured on a sick machine
      is quarantined rather than counted.
- [ ] **The overnight module:** alternate *explore* (queue tonight's smartest bets) with
      *adjudicate* (duel the survivors), so the field grows without anyone pressing a button.
- [ ] **Multi-parameter Bayesian search + hysteresis** over the coupled lever space, checked
      against the recommendation ledger's measured track record before it earns its keep.
- [ ] **Routing intelligence / SD-WAN** with hysteresis (no route flapping; require sustained,
      weather-controlled wins before re-routing).
- [ ] Postgres backend, OAuth/OIDC auth.

Deliberately **not** on the roadmap: latency-under-load / bufferbloat as a scored metric.
See [`ROADMAP.md`](ROADMAP.md) for the full backlog with the reasoning preserved.

---

## License

[MIT](LICENSE) © 2026 jmorganthall
