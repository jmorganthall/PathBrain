# PathBrain Measurement & Methodology Architecture

**Status:** **Implemented.** All ten phases in §10 shipped; the sections below describe the
system as built, with the original design reasoning preserved. Two things have moved past
what this document originally specified and are marked inline: the **Overall** became a
first-class persisted methodology quantity at `speed-smoothness-v5` (§4.1), and
**comparability** grew from "can the raw supply the required metrics?" to a set of named
quarantine tokens that also cover *what* was measured and *whether the machine was well*
when it was (§4.4).
**Owner:** Josh
**Audience:** anyone adding a metric, changing a weight, or reading a historical score.

## 1. Why this exists

PathBrain rates network responsiveness the way RTINGS rates TVs: with **instrumented
measurements** interpreted through a **published methodology**. RTINGS can still tell
you a 2015 Samsung scored 48/50 *and* tell you that score used an older weighting —
because the methodology is a versioned, documented artifact, not a moving target baked
into whatever code happens to be deployed today.

We want the same property. The governing invariant of the whole system is:

> **raw observations  +  methodology  →  score**, deterministically and reproducibly.

If you have the raw observations and you know the methodology version, you can always
reproduce the score. Methodologies are immutable and append-only; you never edit one,
you publish a new one.

## 2. The four layers

| Layer | What it is | Mutability |
|---|---|---|
| **1. Observations (raw)** | Instrumented truths at run-time: milliseconds, jitter, Mbps, byte-arrival times, paint events, Long Animation Frames. Emitted by plugins, which are *pure sensors* — they never interpret. | **Immutable.** Stored once, never recomputed. |
| **2. Methodology** | How raw becomes a score. Two parts, versioned together: **derivation** (raw → metric scalars: jitter = stddev(RTTs), byte_earliness = area over the cumulative-bytes curve, …) and **rubric** (metric scalars → 0–100 subscores → axis scores: the metric set, weights, thresholds, axes). | **Immutable & append-only.** A new weight, threshold, or metric = a **new** methodology version. |
| **3. Score-at-measure** | The score a run got under the methodology that was current **when it was collected**. "48/50 under the 2015 methodology." | **Frozen.** Never overwritten. |
| **4. Score-at-present** | The same raw seen through the **current** methodology — *when that's possible*. A pure re-weight is always reproducible; a methodology that introduced a metric the old raw never captured is not. | Derived/cached; recomputed when the current methodology changes. |

## 3. What we keep from today

This is an evolution, not a rewrite. Already correct and reused as-is:

- **Layer 1 is done.** Plugins emit `PluginResult.raw`; the runner stores it on
  `BenchmarkResult.raw = {"iterations": [...]}` as the immutable source of truth.
- **The re-interpretation engine is done.** `interpret/derive.py` (`DERIVATION_VERSION`)
  turns raw → scalars and can be re-run over history; `runner.rederive_run` /
  `rescore_run` already recompute from raw or cached scalars. This is the genuinely
  hard part of Layer 4 and it exists.
- **Versioning exists in spirit.** `derivation_version` and `rubric_version` are already
  stamped on every score.

What's missing — and what this design adds:

- **G1 — Methodology isn't stored as data.** We tag a version *string* but the version's
  *definition* (metric set, weights, thresholds) lives only in current code/config. Change
  `metrics.py` and the old definition is gone except in git. Thresholds aren't stored
  per-run at all.
- **G2 — Re-grading mutates score-at-measure.** `POST /api/score/rescore` overwrites the
  `ScoreResult` row in place, destroying "what it scored at the time."
- **G3 — Comparability is binary.** The `marks_latest` legacy flag says comparable-or-not;
  it can't say *which* metrics are reproducible under a given methodology.

> All three are closed. G1: `Methodology.definition` is a frozen per-version snapshot.
> G2: re-grading writes a new `(run × methodology)` row and never touches another version's
> at-measure row. G3: `comparability()` names exactly what's missing — and grew past
> *metrics* into a token set covering the sites, the client, page coverage and the health of
> the machine (§4.4). `marks_latest` survives only as the per-run "legacy" badge on Run
> Detail; nothing gates a scored aggregation on it.

## 4. Data model

### 4.1 `Methodology` (new — the missing Layer 2)

Immutable, append-only. One row per published version.

```
Methodology
  version            TEXT PK         -- e.g. "perceptual-v5" (rubric+derivation bundle id)
  rubric_version     TEXT            -- weights/thresholds identity
  derivation_version TEXT            -- raw->scalar code identity ("derive-v2")
  created_at         DATETIME
  notes              TEXT            -- changelog: "re-anchored thresholds to CWV"
  definition         JSON            -- the full frozen catalog+rubric (schema below)
  is_current         BOOL            -- exactly one true; the published-now methodology
```

`definition` is the complete, self-contained snapshot — everything needed to interpret a
score or re-derive one, with no reference to current code:

```jsonc
{
  "axes": [
    { "key": "responsiveness", "label": "Responsiveness", "role": "headline" },
    { "key": "smoothness",     "label": "Smoothness",     "role": "headline" },
    { "key": "speed",          "label": "Speed",          "role": "headline" },
    { "key": "stability",      "label": "Stability",      "role": "secondary" },
    { "key": "completion",     "label": "Completion",     "role": "secondary" }
  ],
  "metrics": [
    {
      "key": "byte_earliness", "axis": "responsiveness",
      "plugin": "browser", "source_key": "byte_earliness_ms",
      "weight": 30, "best": 200.0, "worst": 5000.0,
      "unit": "ms", "label": "Byte earliness", "higher_is_better": false,
      "required": false,         // a run lacking a *required* metric is not exactly-scorable
      "description": "..."
    }
    // ... one entry per metric in play at this version
  ]
}
```

> The axes above were `speed-smoothness-v4`: the three temporal phases of a load —
> **Responsiveness** (time-to-first), **Smoothness** (the steady fill), and **Speed**
> (time-to-last + interactive) — plus secondary **Stability** (CLS) and **Completion**
> (infra). Each metric maps to exactly one axis, so a new headline framing is just a
> re-partition published as a new version. The published-now version is
> **`speed-smoothness-v16`**, which keeps that partition minus the metrics a *probe*
> plugin supplied (the HTTP-socket TTFB and the whole Completion axis) — because runs now
> measure only what the methodology requires, and a rubric still scoring the probes would
> grade every new run *partial* forever.
>
> **The Overall outgrew this note.** It says the Overall is a derived presentation
> roll-up, "deliberately not a methodology axis, never persisted". That stopped being true
> at **`speed-smoothness-v5`**: the Overall is now a **first-class, versioned quantity
> defined by the methodology itself** (an `overall` spec naming its metric set, its method
> and its weights) and **persisted** on every `Score` at scoring time. The reason is the
> one this whole document is about — the Overall is what crowns a profile and therefore
> what gets written to a firewall, so leaving its definition in API presentation code meant
> the single most consequential number in the system was the one number with no versioned
> definition and no frozen snapshot. It now re-derives from `raw + definition` like
> everything else.
>
> Its shape has moved twice since, both as ordinary published versions. Through **v14** it
> was a **corner** — closeness to the perfect 100-corner, an *intersection* where one weak
> metric can't be averaged away. **v15** made it a **weighted average** of the same
> perception-calibrated subscores (FCP 1 · LCP 1 · network_stall_all 0.5), because on a fast
> link the field-percentile corner carried a ±17-point standard error and the top ~66
> profiles were a tie no amount of measuring could break — rank normalization was
> *manufacturing* noise absent from the raw milliseconds. Both primitives live in
> `methodology.py` (`corner_score` / `weighted_score`) and the spec's `method` field
> chooses; every consumer reads the spec rather than assuming one.

The `definition` is produced from the live registry (`metrics.py` + config) at publish
time, so it's always a faithful snapshot of "the methodology in play."

### 4.2 Observations (unchanged)

`BenchmarkResult.raw` stays exactly as is — the immutable Layer 1.

### 4.3 `Score` (new — replaces in-place `ScoreResult` semantics)

**Decision: a full `(run × methodology)` table.** A run can be scored under any number of
methodologies; each pairing is its own immutable row. This is the most RTINGS-complete
shape — you can view any run under any past or present methodology, not just at-measure +
current.

```
Score
  id                 PK
  run_id             FK -> runs
  methodology_version FK -> methodology.version
  is_at_measure      BOOL          -- true iff methodology_version == the run's at-capture version
  comparability      TEXT          -- "exact" | "partial" | "incomparable"  (see 4.4)
  missing_metrics    JSON          -- keys the methodology wanted but this raw can't supply
  -- per-axis results (Speed / Smoothness / Stability / Completion):
  axis_scores        JSON          -- { "speed": 88.1, "smoothness": 54.3, ... }
  subscores          JSON          -- { metric_key: 0..100 }
  weights_used       JSON          -- redistributed weights actually applied
  metric_values      JSON          -- the scalars scored (derived from raw under this methodology)
  bands              JSON          -- per-axis stdev/min/max + p75/p95 over iterations/window
  computed_at        DATETIME
  UNIQUE(run_id, methodology_version)
```

- The **score-at-measure** is the row with `is_at_measure = true`. It is written once at
  capture and **never updated**.
- The **score-at-present** is the row whose `methodology_version` is the current one. If a
  run was captured under the current methodology, the at-measure row *is* the at-present
  row.
- Re-grading **adds or refreshes** the row for a given methodology; it never touches the
  at-measure row of a different version. G2 solved.

> Migration note: today's single `ScoreResult` becomes the at-measure `Score` row for its
> run (carrying its existing `rubric_version`/`derivation_version`). The legacy quarantine
> (`marks_latest`) is subsumed by `comparability` below.

### 4.4 Comparability (replaces the binary legacy flag — G3)

For a given `(run, methodology)`, re-derive the run's raw under the methodology's
`derivation_version`, then compare the methodology's **required** metrics against what the
raw can actually produce:

- **exact** — every required metric is reproducible. A pure re-weight/threshold change is
  always exact (raw and derivation unchanged). Full score-at-present available.
- **partial** — some non-trivial metrics are missing; the score is computed with the usual
  weight redistribution and `missing_metrics` lists what was dropped, so the number is
  honest about its gaps.
- **incomparable** — a **required** metric the raw never captured (a new instrument added
  after this run) is missing. No faithful score-at-present; the UI says so explicitly and
  shows only the score-at-measure.

This is exactly the Layer-4 distinction: *"sometimes methodology change is just a
re-weighting (re-scorable); sometimes it adds a metric we didn't have (not)."* — made
precise and per-run.

#### What comparability grew into

The three grades above are as built. What the design didn't anticipate is that "did this
run measure the same thing?" has **more than one way to be no**, and each one needs its own
name — because a reader who is told only "incomparable" can't tell a run that measured
other sites from a run whose instrument was missing. `comparability()` therefore returns
`missing_metrics` as a set of **tokens**, and it is the single predicate every scored view
filters on (`methodology.is_comparable`), so adding a token quarantines a run everywhere at
once rather than in the six places that remembered to check:

| Token | Means |
|---|---|
| *(a metric key)* | The raw can't supply a metric this version **requires** — the original case. The required set is one accessor: metrics flagged `required` **∪** the crown's own required set, so the Overall is required *by construction* and the page can't under-report it. |
| `site_set` | Measured against a different **site list** than the version declares. Every browser metric is a mean over the pages loaded, so swapping a site changes what FCP, LCP and the stall metrics measure. |
| `client_set` | Measured with a different **browser client** — headless mode, viewport, user agent, locale, automation hiding. A site hands an automated 800×600 headless shell a different page than it hands a person, so the same URL loaded as two clients is two measurements. |
| `site_coverage` | A declared page **failed to load**, so the run's metrics are a mean over a subset. It wore the correct `site_set` stamp (the stamp hashes the *configured* list), which is exactly why this needed its own check. |
| `instrument` | The **machine** was measurably degraded while measuring — graded on quantities the shaper cannot move, as a ratio against the healthy quarter of recent history. A slow host lands inside FCP and LCP through the render phase, so the link reads worse than it was. |

Two rules hold this together, and both were learned from a failure rather than designed:

- **Unmeasurable is an omission, never a value.** The gate quarantines on an *absent*
  required metric, so the entire guarantee rests on the derivation layer emitting **nothing**
  for a metric it can't genuinely compute. A metric fabricated as a "perfect" default slips
  past the gate *and* out-ranks real measurements, because the crown's legs are
  lower-is-better and a synthesized `0` is the best possible score. That is not
  hypothetical: `network_stall_all` needed Long-Animation-Frame provenance to split
  network- from render-attributed dead-air, degenerated to `0` without it, and pre-instrument
  runs rode that perfect score to #1 until honest runs arrived and dragged the crowned
  profile to 65th. Two import-run tests now pin it — dropping *any* current crown metric must
  quarantine, and a raw without the instrument must derive *without* the leg.
- **Quarantining is for what we can show, not for what we suspect.** The instrument gate has
  three states and only the worst one quarantines: `degraded` is dropped, `strained` is
  recorded, shown and **still counts**, and "we couldn't tell" — too few readings, no
  baseline, the gate switched off — never quarantines anything. A guess about the machine
  must never cost a real measurement.

## 5. Lifecycle

**At capture (a run completes):**
1. Plugins emit raw → stored on `BenchmarkResult.raw`.
2. Derive raw → scalars under the current `derivation_version`.
3. Score under the current methodology → write the `Score` row with
   `is_at_measure = true`, `comparability = "exact"`, and stamp `run.methodology_version`.

**Publishing a new methodology (new weights / thresholds / metric):**
1. Snapshot the live registry → insert a new immutable `Methodology` row; flip `is_current`.
2. For each completed run, compute its `Score` under the new methodology (exact / partial /
   incomparable). This is the batch that was previously `POST /api/score/rescore` — but now
   it **writes new rows**, leaving every score-at-measure intact.
3. Runs that are `incomparable` under the new methodology simply have no at-present row;
   they keep their score-at-measure and say "not comparable — needs metric X."

**Reproducibility:** because `raw + methodology.definition → score` is deterministic, any
score can be recomputed and audited at any time from data alone.

## 6. Surfacing — the Methodology tab

- `GET /api/methodologies` — list every version: created_at, notes, axis/metric set,
  weights, thresholds, and which is current.
- `GET /api/methodologies/{version}` — the full frozen definition.
- `GET /api/methodologies/diff?from=v4&to=v5` — field-level rubric diff (same idea as the
  existing settings-profile diff: metric added/removed, weight ↑/↓, threshold moved).
- **Methodology page** — the published versions, their changelogs, and version-to-version
  diffs. "Here's the methodology used at the time this was collected."
- **Run Detail** — "Scored **88 / 54** under **perceptual-v4** (captured Jun 12)" plus
  "Under current **v6**: 81 / 50 (exact)" *or* "Not comparable under v6 — needs
  `byte_earliness`."

## 7. How future changes ride on this

Once methodology is first-class, the rest of the backlog stops being code surgery and
becomes *publishing a version*:

- **Speed/Smoothness split** (replace the single SOPS headline) → **shipped.** Published as
  `speed-smoothness-v1..v3`; `v4` then re-partitioned the headline into
  **Responsiveness + Smoothness + Speed** (the three temporal phases of a load), moving
  LCP/render/INP into a redefined Speed and leaving Stability = CLS-only. Because each
  metric still maps to one axis and derivation was unchanged, every historical run
  re-scores **exact** straight from raw via `POST /api/score/regrade` — no recollection.
  The new headline framing was a pure re-partition: no engine change.
- **Re-weighting a metric** (calibration) → new version, **exact** everywhere.
- **A genuinely new instrument** → new version; pre-instrument runs are **incomparable**
  for that metric and the UI says so — no silent, misleading "current score."

That modularity — add a metric or re-weight by publishing a version, with automatic,
honest comparability — is the entire point of this layer. Sixteen versions in, here is what
actually rode on it, because the list is the argument for having built it:

- **The crown metric set changed eight times** (v5 → v16) — the set the Overall is computed
  from, and therefore what "best" means. Each was a published version and each re-graded
  history from already-captured raw. Several were *reverts of a previous version's
  reasoning*, which is the property that matters: v9 chose the crown's legs for
  shaper-movability and v10 reverted to first principles; v11 narrowed the smoothness leg's
  window to FCP→LCP and v12 widened it again when measurement showed FCP→LCP is near-instant
  on a fast link. Being able to publish a wrong idea, measure that it was wrong, and publish
  the correction without losing a single run of history is the whole return on this layer.
- **Inert crown legs were caught by measurement, not by review.** Twice a crown leg read
  the same value for every profile on a fast link — `worst_void_fraction` at 0 (its 200 ms
  perceptible-stall floor discarded exactly the sub-perceptible handoff gaps a fiber page
  load is made of) and, earlier, a saturated threshold where every profile already scored
  perfect. A frozen definition per version is what let the field be re-graded under the fix
  rather than re-measured under it.
- **The Overall itself became first-class** (v5) and then changed *method* (v15, corner →
  weighted). A quantity that decides what gets written to a firewall is exactly the one that
  had to stop living in presentation code.
- **What is measured joined what is scored.** The site list and the browser client are now
  part of the version (`collection` / `client_set`), because a mean over different pages, or
  the same page served to a different client, is a different measurement wearing the same
  name. Publishing either forks a version and quarantines the old collection.
- **The scope of measurement followed the rubric** (v16). Once the crown was entirely
  browser-derived, keeping five probe plugins in every run cost about half of each run's wall
  clock to feed a secondary axis. Dropping them from the *rubric* is what let runs stop
  *collecting* them without every new run reading `partial` forever.
- **The score can be re-derived per site, on demand, from the same stored raw** — which is
  how "where does this profile's win actually live?" is answered without a second
  instrument: one `interpret.derive` restricted to one page, priced on the methodology's own
  thresholds and weights.

## 8. Versioning rules

- Bump **`derivation_version`** when the raw→scalar math changes (a metric is computed
  differently, or a new metric is derived). Triggers a re-derive from raw.
- Bump **`rubric_version`** when weights/thresholds/axes/metric-membership change. Triggers
  a re-score from scalars.
- The **`Methodology.version`** is the bundle id that pairs a specific rubric with a specific
  derivation; it's what scores reference and what the tab lists.

## 9. Migration (clean from current version forward)

**Decision: clean-from-current-forward.**

- Snapshot the methodology from the current version (`perceptual-v5` at the time) onward
  into the `Methodology` table; record its full definition. **Done**, and the gap closed as
  predicted: every version from the foundation forward is fully recorded, and the
  `speed-smoothness-*` family — the only one anything is scored under today — is complete.
- Existing `ScoreResult` rows become the at-measure `Score` rows for their runs, carrying
  their stored `rubric_version` string. Pre-foundation versions (v1–v4) are listed in the
  tab as **"definition not recorded"** — their *scores* survive (the values were stored),
  but their full rubric definition can't be faithfully reconstructed because thresholds were
  never persisted per-run. We don't pretend otherwise.
- From the foundation forward, every published version is fully recorded, so this gap closes
  permanently after one version.

## 10. Phased implementation

All five shipped.

1. ✅ **`Methodology` model + registry + snapshot-on-publish**, seeded with the current
   version's full definition. (`GET /api/methodologies*`.)
2. ✅ **`Score` table** (run × methodology); the at-measure row is written at capture;
   existing `ScoreResult` rows migrated. In-place mutation stopped.
3. ✅ **Re-grade writes new rows**, with per-run **comparability** (exact / partial /
   incomparable) — and the re-grade job reports the split so a publish says how much of
   history came with it.
4. ✅ **Methodology page** + Run Detail's at-measure / at-present surfacing, with the
   quarantine tokens spelled out in words rather than left as keys.
5. ✅ **The backlog folded in as versions** — sixteen of them; see §7 for what they were.

Two things were added past the original plan, both because publishing turned out to need
more than a definition:

6. ✅ **Publish from the UI.** A too-lenient threshold can be re-anchored
   (`POST /api/methodologies/reanchor` forks the current version with a tightened `best`
   and re-grades) and the site list + browser client published
   (`POST /api/methodologies/sites`) without a code edit — while every published version
   stays a frozen DB snapshot. The alternative was that noticing a saturated threshold and
   *fixing* it were separated by a deploy, which is how a known-wrong rubric stays live.
7. ✅ **Seed the field from the prior version.** Right after any publish nothing has a
   comparable run, so the pooled crown is empty and every consumer that ranks the field —
   the duel's matchmaking, the challenger race, the heirs card — would order it by nothing.
   The prior version's standings seed the **order** (never the scores; nothing seeded is
   scored) until fresh runs arrive. This is the operational cost of honest quarantining, and
   it has to be paid somewhere: the alternative is either scoring runs under a rubric they
   can't support, or a system that goes blind for a day every time it learns something.
