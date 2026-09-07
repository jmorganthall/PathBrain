"""Cold vs warm crown: do the first-visit and the repeat-visit instruments rank the profiles
alike?

The crown grades a **first visit**: a fresh browser context per page, every handshake paid.
Most of a person's clicks are not that — a site's next page reuses its connections, a
resumed TLS or QUIC session skips the round trips — and the mission is how the Internet
*feels* on those clicks. So the browser plugin also loads each page a second time in the
same context with the HTTP cache disabled (``browser.warm_loads``): warm sockets, every
byte still fetched, read with the same derivations and filed as ``warm_*`` beside the cold
reading. Nothing graded moves. This module asks the only question that decides whether the
crown should ever read them: **ranked by a warm Overall, do the profiles come out in the
same order as ranked by the cold one?**

Assumption, stated so it can be tested rather than believed: a cold-only crown over-weights
the handshakes (which fq_codel does move) and under-weights the in-connection pacing a warm
browser lives in. If the two rankings agree, the cold crown stands for the warm case too and
that assumption was harmless. If they disagree, the crown is optimising something the repeat
visit does not feel — a methodology decision, made from a measured number rather than a
hunch, which is the whole point of measuring both.

The warm Overall is built from the methodology's **own** crown metrics, thresholds and
weights (``overall_from_definition``), with each crown leg's warm median substituted for its
cold one — the same yardstick, so the two rankings differ only in what was measured. Each
profile's medians are taken over the **same runs** (only runs carrying both readings), so
the comparison is paired. Read-only; bounded by a run cap and JSON-path scalar reads.
"""
from __future__ import annotations

from collections import defaultdict
from statistics import median

from sqlalchemy import and_, select

from .models import BenchmarkResult, Run, RunStatus
from .stats import spearman

MIN_RUNS = 5
LIMIT = 20000
WARM_PREFIX = "warm_"
# Below this rank correlation the two instruments are said to order the field differently
# even when they agree on the #1.
AGREE_RHO = 0.8
_CHUNK = 500


def crown_sources(definition: dict) -> dict[str, str]:
    """``{crown key: browser source key}`` for the methodology's crown metrics."""
    from .methodology import overall_metrics

    keys, _required = overall_metrics(definition or {})
    by_key = {m["key"]: m for m in (definition or {}).get("metrics", []) if isinstance(m, dict)}
    return {k: by_key[k]["source_key"] for k in keys if k in by_key and by_key[k].get("plugin") == "browser"}


def load_profiles(session, sources: dict[str, str], *, limit: int = LIMIT) -> dict[str, list[dict]]:
    """Per profile, the completed runs carrying BOTH a cold and a warm reading of every crown
    metric, as ``{"cold": {key: v}, "warm": {key: v}}`` — scalars through JSON paths."""
    if not sources:
        return {}
    metrics = BenchmarkResult.metrics
    keys = list(sources)
    first_warm = metrics[WARM_PREFIX + sources[keys[0]]]
    cols = [Run.id, Run.settings_fingerprint]
    cols += [metrics[sources[k]].as_float() for k in keys]
    cols += [metrics[WARM_PREFIX + sources[k]].as_float() for k in keys]
    q = (
        select(*cols)
        .join(BenchmarkResult, and_(BenchmarkResult.run_id == Run.id, BenchmarkResult.plugin == "browser"))
        .where(
            Run.status == RunStatus.COMPLETE,
            Run.settings_fingerprint.is_not(None),
            first_warm.is_not(None),
        )
        .order_by(Run.id.desc())
        .limit(max(1, int(limit)))
    )
    out: dict[str, list[dict]] = defaultdict(list)
    n = len(keys)
    for row in session.execute(q).all():
        _rid, fp, *vals = row
        cold, warm = vals[:n], vals[n:]
        if any(v is None for v in cold) or any(v is None for v in warm):
            continue
        out[fp].append({"cold": dict(zip(keys, map(float, cold))), "warm": dict(zip(keys, map(float, warm)))})
    return dict(out)


def _overall(definition: dict, sources: dict[str, str], values: dict[str, float]) -> tuple[float | None, dict]:
    """Score one set of crown-metric values on the methodology's own thresholds and fold them
    into its Overall — the yardstick the crown is ranked on."""
    from .methodology import overall_from_definition
    from .scoring.engine import compute_score

    by_key = {m["key"]: m for m in (definition or {}).get("metrics", []) if isinstance(m, dict)}
    thresholds = {k: {"best": by_key[k]["best"], "worst": by_key[k]["worst"]} for k in sources if k in by_key}
    metric_sources = {k: ("browser", src) for k, src in sources.items()}
    breakdown = compute_score(
        {"browser": {src: values[k] for k, src in sources.items() if k in values}},
        weights={k: 1.0 for k in sources},
        thresholds=thresholds,
        metric_sources=metric_sources,
    )
    subs = dict(breakdown.subscores or {})
    return overall_from_definition(definition, subs), subs


def assess(
    profiles: dict[str, list[dict]],
    *,
    definition: dict,
    sources: dict[str, str],
    min_runs: int = MIN_RUNS,
    crown_fp: str | None = None,
    names: dict[str, str] | None = None,
) -> dict:
    """The pure core: rank the profiles by their cold and their warm Overall and compare."""
    names = names or {}
    rows: list[dict] = []
    thin = 0
    for fp, runs in profiles.items():
        if len(runs) < min_runs:
            thin += 1
            continue
        cold_med = {k: round(median(r["cold"][k] for r in runs), 3) for k in sources}
        warm_med = {k: round(median(r["warm"][k] for r in runs), 3) for k in sources}
        cold_overall, cold_sub = _overall(definition, sources, cold_med)
        warm_overall, warm_sub = _overall(definition, sources, warm_med)
        if cold_overall is None or warm_overall is None:
            continue
        rows.append({
            "fingerprint": fp,
            "name": names.get(fp),
            "runs": len(runs),
            "is_crown": fp == crown_fp,
            "cold": {"overall": round(cold_overall, 2), "medians": cold_med, "subscores": {k: round(v, 1) for k, v in cold_sub.items()}},
            "warm": {"overall": round(warm_overall, 2), "medians": warm_med, "subscores": {k: round(v, 1) for k, v in warm_sub.items()}},
            "warm_minus_cold": round(warm_overall - cold_overall, 2),
        })
    for side in ("cold", "warm"):
        for i, r in enumerate(sorted(rows, key=lambda r: -r[side]["overall"]), start=1):
            r[f"{side}_rank"] = i
    rows.sort(key=lambda r: r["cold_rank"])
    rho = spearman([r["cold"]["overall"] for r in rows], [r["warm"]["overall"] for r in rows]) if len(rows) >= 3 else None
    top_cold = next((r for r in rows if r["cold_rank"] == 1), None)
    top_warm = next((r for r in rows if r["warm_rank"] == 1), None)
    agree_top = bool(top_cold and top_warm and top_cold["fingerprint"] == top_warm["fingerprint"])
    crown_row = next((r for r in rows if r["is_crown"]), None)

    def _nm(r: dict | None) -> str:
        return (r.get("name") or r["fingerprint"][:8]) if r else "—"

    if len(rows) < 3:
        verdict = "insufficient"
        text = (
            f"Only {len(rows)} profile(s) have {min_runs}+ runs carrying both readings"
            + (f" ({thin} more are thinner)" if thin else "")
            + "; the comparison needs three. It fills in as runs with warm loads accrue."
        )
    elif agree_top and (rho is None or rho >= AGREE_RHO):
        verdict = "agree"
        text = (
            f"The first-visit and repeat-visit instruments rank the profiles alike (ρ = {rho:+.2f} over "
            f"{len(rows)} profiles, same #1: {_nm(top_cold)}). The cold crown stands for the warm case too; "
            "no methodology change is indicated."
        )
    elif agree_top:
        verdict = "same_top"
        text = (
            f"Same #1 ({_nm(top_cold)}) on both instruments, but the order beneath it differs (ρ = {rho:+.2f} "
            f"over {len(rows)} profiles). The crown holds for the repeat visit; the ladder under it does not, "
            "which matters for who gets raced next, not for what is on the firewall."
        )
    else:
        verdict = "disagree"
        text = (
            f"They disagree: by first visit {_nm(top_cold)} ranks first, by repeat visit {_nm(top_warm)} does "
            f"(ρ = {rho:+.2f} over {len(rows)} profiles). On the repeat visit — most clicks — the cold crown is "
            "not the best-feeling profile. That is a methodology decision: a version can adopt the warm legs "
            "(warm_fcp / warm_lcp / warm_network_stall_all) in its Overall, and this card is the measured "
            "reason to make it."
        )
    if crown_row and crown_row["cold_rank"] != crown_row["warm_rank"]:
        text += f" The pooled crown ({_nm(crown_row)}) sits #{crown_row['cold_rank']} cold and #{crown_row['warm_rank']} warm."
    return {
        "verdict": verdict,
        "text": text,
        "rho": round(rho, 3) if rho is not None else None,
        "profiles": len(rows),
        "thin_profiles": thin,
        "min_runs": min_runs,
        "agree_top": agree_top,
        "top_cold": {"fingerprint": top_cold["fingerprint"], "name": top_cold.get("name")} if top_cold else None,
        "top_warm": {"fingerprint": top_warm["fingerprint"], "name": top_warm.get("name")} if top_warm else None,
        "crown": {"fingerprint": crown_fp, "cold_rank": crown_row["cold_rank"], "warm_rank": crown_row["warm_rank"]} if crown_row else ({"fingerprint": crown_fp} if crown_fp else None),
        "rows": rows,
        "crown_metrics": list(sources),
    }


def warm_agreement(session, cfg: dict | None, *, min_runs: int = MIN_RUNS, limit: int = LIMIT) -> dict:
    """The audit off the database: the current methodology's crown metrics, the runs carrying
    both readings, call signs, the pooled crown, assess."""
    from .methodology import ensure_current_methodology

    methodology = ensure_current_methodology(session, cfg or {})
    definition = methodology.definition or {}
    sources = crown_sources(definition)
    profiles = load_profiles(session, sources, limit=limit)
    names: dict[str, str] = {}
    crown_fp = None
    try:
        from .profile_names import names_for

        names = names_for(session, sorted(profiles)) if profiles else {}
    except Exception:  # noqa: BLE001 — naming is never why the audit fails
        names = {}
    try:
        from . import crown_follower

        crown_fp = ((crown_follower.current_crown(session) or {}).get("fingerprint"))
    except Exception:  # noqa: BLE001
        crown_fp = None
    out = assess(profiles, definition=definition, sources=sources, min_runs=max(2, int(min_runs)), crown_fp=crown_fp, names=names)
    out["methodology"] = methodology.version
    out["runs_with_warm_reading"] = sum(len(v) for v in profiles.values())
    return out
