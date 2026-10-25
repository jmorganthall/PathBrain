"""Crown agreement: do two methodology versions rank the profiles alike?

A publish that changes the crown — a new leg, a re-weight, a re-anchored threshold — is a
decision about what "best" means, and the one number that says what it *did* is how far the
field's ranking moved. Until this, the only paired-ranking audit on the Methodology page was
`warm_agreement` (cold vs warm instrument under one version); the version-to-version question
had to be answered by remembering the old table. So this is the same audit asked of two
rubrics: every confident profile's **cold medians** on the union of both crowns' metrics,
priced under version ``a`` and under version ``b`` with each version's own thresholds and
weights (`warm_agreement.overall_for`), ranked both ways, and compared — Spearman ρ, the #1
on each, the pooled crown's rank on each.

Both versions may come from the registry (a version that is written but not yet current can
be compared against the one that is — the audit before the flip) or from the database (a
fork published from the page, which the registry never saw). Defaults: ``a`` = the registry
version before the current one (forks skipped), ``b`` = the current version — "what did the
last publish change?" with nothing to type. Read-only; the decision it informs is a publish
or a pin, made elsewhere.
"""
from __future__ import annotations

from collections import defaultdict
from statistics import median

from sqlalchemy import and_, select

from .models import BenchmarkResult, Methodology, Run, RunStatus
from .stats import spearman
from .warm_agreement import AGREE_RHO, LIMIT, MIN_RUNS, crown_sources, overall_for


def registry_versions() -> list[str]:
    """The code-shipped versions, in the order they were published."""
    from .methodology import METHODOLOGY_REGISTRY

    return list(METHODOLOGY_REGISTRY)


def base_version(version: str) -> str:
    """A fork's registry base (``speed-smoothness-v16+sites-abc`` → ``speed-smoothness-v16``)."""
    return version.split("+", 1)[0]


def prior_version(current: str) -> str | None:
    """The registry version published before ``current``'s base, or None at the start."""
    versions = registry_versions()
    base = base_version(current)
    if base not in versions:
        return None
    i = versions.index(base)
    return versions[i - 1] if i > 0 else None


def resolve_definition(session, version: str) -> dict | None:
    """The frozen definition for a version: the stored row when the database has one (a fork
    only ever exists there), else built from the registry, else None."""
    from .methodology import METHODOLOGY_REGISTRY, build_definition_from_spec

    row = session.get(Methodology, version) if session is not None else None
    if row is not None and row.definition:
        return dict(row.definition)
    spec = METHODOLOGY_REGISTRY.get(version)
    if spec is None:
        return None
    return build_definition_from_spec(spec)


def load_profiles(session, sources: dict[str, str], *, limit: int = LIMIT) -> dict[str, list[dict]]:
    """Per profile, the completed browser runs carrying EVERY source in ``sources`` (the union
    of both crowns'), as ``{key: value}`` — cold readings only, scalars through JSON paths."""
    if not sources:
        return {}
    metrics = BenchmarkResult.metrics
    keys = list(sources)
    cols = [Run.id, Run.settings_fingerprint] + [metrics[sources[k]].as_float() for k in keys]
    q = (
        select(*cols)
        .join(BenchmarkResult, and_(BenchmarkResult.run_id == Run.id, BenchmarkResult.plugin == "browser"))
        .where(Run.status == RunStatus.COMPLETE, Run.settings_fingerprint.is_not(None))
        .order_by(Run.id.desc())
        .limit(max(1, int(limit)))
    )
    out: dict[str, list[dict]] = defaultdict(list)
    for row in session.execute(q).all():
        _rid, fp, *vals = row
        if any(v is None for v in vals):
            continue
        out[fp].append(dict(zip(keys, map(float, vals))))
    return dict(out)


def assess(
    profiles: dict[str, list[dict]],
    *,
    a: str,
    b: str,
    a_definition: dict,
    b_definition: dict,
    min_runs: int = MIN_RUNS,
    crown_fp: str | None = None,
    names: dict[str, str] | None = None,
) -> dict:
    """The pure core: price every profile's medians under both versions and compare the orders."""
    names = names or {}
    a_sources, b_sources = crown_sources(a_definition), crown_sources(b_definition)
    all_keys = list(dict.fromkeys([*a_sources, *b_sources]))
    rows: list[dict] = []
    thin = 0
    for fp, runs in profiles.items():
        if len(runs) < min_runs:
            thin += 1
            continue
        med = {k: round(median(r[k] for r in runs), 3) for k in all_keys if all(k in r for r in runs)}
        a_overall, a_sub = overall_for(a_definition, a_sources, med)
        b_overall, b_sub = overall_for(b_definition, b_sources, med)
        if a_overall is None or b_overall is None:
            continue
        rows.append({
            "fingerprint": fp,
            "name": names.get(fp),
            "runs": len(runs),
            "is_crown": fp == crown_fp,
            "medians": med,
            "a": {"overall": round(a_overall, 2), "subscores": {k: round(v, 1) for k, v in a_sub.items()}},
            "b": {"overall": round(b_overall, 2), "subscores": {k: round(v, 1) for k, v in b_sub.items()}},
            "b_minus_a": round(b_overall - a_overall, 2),
        })
    for side in ("a", "b"):
        for i, r in enumerate(sorted(rows, key=lambda r: -r[side]["overall"]), start=1):
            r[f"{side}_rank"] = i
    rows.sort(key=lambda r: r["b_rank"])
    rho = spearman([r["a"]["overall"] for r in rows], [r["b"]["overall"] for r in rows]) if len(rows) >= 3 else None
    top_a = next((r for r in rows if r["a_rank"] == 1), None)
    top_b = next((r for r in rows if r["b_rank"] == 1), None)
    agree_top = bool(top_a and top_b and top_a["fingerprint"] == top_b["fingerprint"])
    crown_row = next((r for r in rows if r["is_crown"]), None)
    moved = sorted(rows, key=lambda r: -abs(r["a_rank"] - r["b_rank"]))
    movers = [
        {"fingerprint": r["fingerprint"], "name": r.get("name"), "a_rank": r["a_rank"], "b_rank": r["b_rank"]}
        for r in moved[:5] if r["a_rank"] != r["b_rank"]
    ]

    def _nm(r: dict | None) -> str:
        return (r.get("name") or r["fingerprint"][:8]) if r else "—"

    short_a, short_b = _short(a), _short(b)
    if len(rows) < 3:
        verdict = "insufficient"
        text = (
            f"Only {len(rows)} profile(s) have {min_runs}+ runs carrying every leg of both crowns"
            + (f" ({thin} more are thinner)" if thin else "")
            + "; the comparison needs three."
        )
    elif agree_top and (rho is None or rho >= AGREE_RHO):
        verdict = "agree"
        text = (
            f"{short_a} and {short_b} rank the profiles alike (ρ = {rho:+.2f} over {len(rows)} profiles, "
            f"same #1: {_nm(top_a)}). The publish changed what the crown measures, not who wins."
        )
    elif agree_top:
        verdict = "same_top"
        text = (
            f"Same #1 ({_nm(top_a)}) under both, but the order beneath it differs (ρ = {rho:+.2f} over "
            f"{len(rows)} profiles). The crown holds; the ladder under it — who gets raced next — moved."
        )
    else:
        verdict = "disagree"
        text = (
            f"They disagree: under {short_a} {_nm(top_a)} ranks first, under {short_b} {_nm(top_b)} does "
            f"(ρ = {rho:+.2f} over {len(rows)} profiles). The publish changed who the best profile is — "
            "which is what a new crown leg is for, and the reason to look at what the new #1 does differently."
        )
    if crown_row and crown_row["a_rank"] != crown_row["b_rank"]:
        text += f" The pooled crown ({_nm(crown_row)}) sits #{crown_row['a_rank']} under {short_a} and #{crown_row['b_rank']} under {short_b}."
    return {
        "verdict": verdict,
        "text": text,
        "rho": round(rho, 3) if rho is not None else None,
        "profiles": len(rows),
        "thin_profiles": thin,
        "min_runs": min_runs,
        "agree_top": agree_top,
        "a": a,
        "b": b,
        "a_crown_metrics": list(a_sources),
        "b_crown_metrics": list(b_sources),
        "top_a": {"fingerprint": top_a["fingerprint"], "name": top_a.get("name")} if top_a else None,
        "top_b": {"fingerprint": top_b["fingerprint"], "name": top_b.get("name")} if top_b else None,
        "crown": (
            {"fingerprint": crown_fp, "a_rank": crown_row["a_rank"], "b_rank": crown_row["b_rank"]}
            if crown_row else ({"fingerprint": crown_fp} if crown_fp else None)
        ),
        "movers": movers,
        "rows": rows,
    }


def _short(version: str) -> str:
    return version.replace("speed-smoothness-", "")


class UnknownVersion(KeyError):
    """Neither the database nor the registry knows this version."""


def crown_agreement(session, cfg: dict | None, *, a: str | None = None, b: str | None = None,
                    min_runs: int = MIN_RUNS, limit: int = LIMIT) -> dict:
    """The audit off the database. ``b`` defaults to the current version, ``a`` to the
    registry version before it; either may name any registry or stored version."""
    from .methodology import ensure_current_methodology

    current = ensure_current_methodology(session, cfg or {})
    b = b or current.version
    a = a or prior_version(b) or b
    a_def = resolve_definition(session, a)
    b_def = resolve_definition(session, b)
    if a_def is None:
        raise UnknownVersion(a)
    if b_def is None:
        raise UnknownVersion(b)
    sources = {**crown_sources(a_def), **crown_sources(b_def)}
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
    out = assess(profiles, a=a, b=b, a_definition=a_def, b_definition=b_def,
                 min_runs=max(2, int(min_runs)), crown_fp=crown_fp, names=names)
    out["methodology"] = current.version
    out["runs_compared"] = sum(len(v) for v in profiles.values())
    out["versions"] = registry_versions()
    return out


__all__ = ["UnknownVersion", "assess", "crown_agreement", "load_profiles", "prior_version", "resolve_definition"]
