# PathBrain Measurement & Methodology Architecture

**Status:** Design (approved direction; implementation phased below)
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

> The axes above are `speed-smoothness-v4` (the published-now version): the three
> temporal phases of a load — **Responsiveness** (time-to-first: TTFB/FCP/byte-
> earliness), **Smoothness** (the steady fill), and **Speed** (time-to-last +
> interactive: LCP/render/INP) — plus secondary **Stability** (CLS) and
> **Completion** (infra). Each metric maps to exactly one axis, so a new headline
> framing is just a re-partition published as a new version. A single higher-is-
> better **Overall** (closeness to the perfect 100/100/100 corner) is computed in
> the API as a *derived presentation roll-up* — deliberately **not** a methodology
> axis (no weights/thresholds of its own, never persisted).

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
- **Re-weighting `perceived_time`** (calibration) → new version, **exact** everywhere.
- **A genuinely new instrument** (e.g. bufferbloat/latency-under-load) → new version;
  pre-instrument runs are **incomparable** for that metric and the UI says so — no silent,
  misleading "current score."

That modularity — add a metric or re-weight by publishing a version, with automatic,
honest comparability — is the entire point of this layer.

## 8. Versioning rules

- Bump **`derivation_version`** when the raw→scalar math changes (a metric is computed
  differently, or a new metric is derived). Triggers a re-derive from raw.
- Bump **`rubric_version`** when weights/thresholds/axes/metric-membership change. Triggers
  a re-score from scalars.
- The **`Methodology.version`** is the bundle id that pairs a specific rubric with a specific
  derivation; it's what scores reference and what the tab lists.

## 9. Migration (clean from current version forward)

**Decision: clean-from-current-forward.**

- Snapshot the methodology from the current version (`perceptual-v5`) onward into the
  `Methodology` table; record its full definition.
- Existing `ScoreResult` rows become the at-measure `Score` rows for their runs, carrying
  their stored `rubric_version` string. Pre-foundation versions (v1–v4) are listed in the
  tab as **"definition not recorded"** — their *scores* survive (the values were stored),
  but their full rubric definition can't be faithfully reconstructed because thresholds were
  never persisted per-run. We don't pretend otherwise.
- From the foundation forward, every published version is fully recorded, so this gap closes
  permanently after one version.

## 10. Phased implementation

1. **`Methodology` model + registry + snapshot-on-publish**, seeded with the current
   version's full definition. (`GET /api/methodologies*`.)
2. **`Score` table** (run × methodology); write the at-measure row at capture; migrate
   existing `ScoreResult` → at-measure rows. Stop in-place mutation.
3. **Re-grade = write new rows** (replace the mutating `rescore`/`rederive` semantics) with
   per-run **comparability** (exact / partial / incomparable).
4. **Methodology tab** + Run Detail "at-measure vs at-present" surfacing.
5. **Fold the paused backlog in as methodology versions**: Speed/Smoothness axes (Change 2),
   perceived-time calibration (Change 3), p75/p95 bands (Change 5), config-tag splitting
   (Change 6).

Each phase is independently shippable and verifiable.
