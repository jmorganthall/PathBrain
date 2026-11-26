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
    """The registry version published before ``current``'s base, or None at the start. A
    version on another's scale (``same_scale_as``, v19 over v17) is compared with that one:
    it is the version it was written against, whatever candidates sit between them."""
    from .methodology import METHODOLOGY_REGISTRY

    same = (METHODOLOGY_REGISTRY.get(base_version(current)) or {}).get("same_scale_as")
    if same:
        return str(same)
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
    from .settings_overrides import effective_fingerprint

    cols = [Run.id, effective_fingerprint()] + [metrics[sources[k]].as_float() for k in keys]
    q = (
        select(*cols)
        .join(BenchmarkResult, and_(BenchmarkResult.run_id == Run.id, BenchmarkResult.plugin == "browser"))
        .where(Run.status == RunStatus.COMPLETE, effective_fingerprint().is_not(None))
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
        from . import verdict

        crown_fp = verdict.crown_fingerprint(session)
    except Exception:  # noqa: BLE001
        crown_fp = None
    out = assess(profiles, a=a, b=b, a_definition=a_def, b_definition=b_def,
                 min_runs=max(2, int(min_runs)), crown_fp=crown_fp, names=names)
    out["methodology"] = current.version
    out["read"] = None
    from .methodology import crown_within_speed_budget, pooled_read, same_scale

    read_a, read_b = pooled_read(a_def), pooled_read(b_def)
    budget_a, budget_b = crown_within_speed_budget(a_def), crown_within_speed_budget(b_def)
    if read_a != read_b or budget_a != budget_b:
        out["read"] = read_comparison(session, a, b, read_a, read_b, rubric_alike=same_scale(a, b),
                                      budget_a=budget_a, budget_b=budget_b)
        if same_scale(a, b):
            # The cold medians are priced identically under both, so their audit can only say
            # "alike": the difference is in how the ranking reads the record, so that is the verdict.
            out["verdict"] = out["read"]["verdict"]
            out["text"] = out["read"]["text"]
    out["runs_compared"] = sum(len(v) for v in profiles.values())
    out["versions"] = registry_versions()
    return out


_READ_WORDS = {
    "all": "one median per profile over the whole record",
    "epochs": "each profile's median per instrument era, on today's scale",
}


def _rule_words(read: str, budget: bool) -> str:
    return _READ_WORDS[read] + ("; the crown kept within the speed budget" if budget else "")


def read_comparison(session, a: str, b: str, read_a: str, read_b: str, *, rubric_alike: bool,
                    budget_a: bool = False, budget_b: bool = False) -> dict:
    """Two versions that read the pooled record differently (v17's whole-record median, v19's
    per-era one), or that differ in whether the crown is kept within the speed budget (v20):
    the fused ranking under each, from today's scores and ring, compared the way `assess`
    compares two rubrics. Read-only (`overall_ranking.ranking` previews)."""
    from . import overall_ranking

    ra = overall_ranking.ranking(session, backtest=False, live=False, read=read_a, budget=budget_a)
    rb = overall_ranking.ranking(session, backtest=False, live=False, read=read_b, budget=budget_b)

    def order(r: dict) -> dict[str, int]:
        rows = [p for p in r.get("profiles") or [] if p.get("eligible")]
        return {p["fingerprint"]: i for i, p in enumerate(rows, start=1)}

    oa, ob = order(ra), order(rb)
    shared = [fp for fp in oa if fp in ob]
    rho = spearman([oa[f] for f in shared], [ob[f] for f in shared]) if len(shared) >= 3 else None
    best_a, best_b = ra.get("best") or {}, rb.get("best") or {}
    fa, fb = best_a.get("fingerprint"), best_b.get("fingerprint")

    def nm(p: dict) -> str:
        return p.get("name") or (p.get("fingerprint") or "—")[:8]

    sa, sb = _short(a), _short(b)
    prefix = (
        f"{sa} and {sb} grade every run alike; they differ in how the ranking reads them "
        f"({sa}: {_rule_words(read_a, budget_a)}; {sb}: {_rule_words(read_b, budget_b)}). "
        if rubric_alike else
        f"{sa} and {sb} also rank differently ({sa}: {_rule_words(read_a, budget_a)}; "
        f"{sb}: {_rule_words(read_b, budget_b)}). "
    )
    if not fa or not fb:
        verdict = "insufficient"
        body = "One of the two reads names no crown yet, so there is nothing to compare."
    elif fa == fb:
        verdict = "agree" if rho is None or rho >= AGREE_RHO else "same_top"
        body = (
            f"Both crown {nm(best_a)}"
            + (f" (ρ = {rho:+.2f} over {len(shared)} eligible profiles)." if rho is not None else ".")
            + f" Under {sb} it leads by {rb.get('lead')} against a noise bar of {rb.get('noise_bar')}"
            + f", with {rb.get('tied_count') or 0} profile(s) tied with it."
        )
    else:
        verdict = "disagree"
        rank_a_in_b = ob.get(fa)
        body = (
            f"Under {sa} {nm(best_a)} is the crown; under {sb} {nm(best_b)} is"
            + (f", and {nm(best_a)} drops to #{rank_a_in_b}" if rank_a_in_b else "")
            + (f" (ρ = {rho:+.2f} over {len(shared)} eligible profiles)." if rho is not None else ".")
            + f" {sb}'s lead is {rb.get('lead')} against a noise bar of {rb.get('noise_bar')}"
            + f", with {rb.get('tied_count') or 0} profile(s) tied with its #1. Switching to {sb} "
            "changes what Auto-apply follows."
        )
    eras = rb.get("eras") or ra.get("eras") or {}
    return {
        "a_read": read_a, "b_read": read_b, "rubric_alike": rubric_alike,
        "a_crown_within_budget": budget_a, "b_crown_within_budget": budget_b,
        "verdict": verdict, "text": prefix + body,
        "rho": round(rho, 3) if rho is not None else None, "profiles": len(shared),
        "top_a": {"fingerprint": fa, "name": best_a.get("name"), "fused": best_a.get("fused")} if fa else None,
        "top_b": {"fingerprint": fb, "name": best_b.get("name"), "fused": best_b.get("fused")} if fb else None,
        "a_rank_of_b_top": oa.get(fb) if fb else None,
        "b_rank_of_a_top": ob.get(fa) if fa else None,
        "b_tied": rb.get("tied_count"), "b_lead": rb.get("lead"), "b_noise_bar": rb.get("noise_bar"),
        "eras": {"boundaries": eras.get("boundaries"), "offsets": eras.get("offsets")},
    }


__all__ = ["UnknownVersion", "read_comparison", "assess", "crown_agreement", "load_profiles", "prior_version", "resolve_definition"]
