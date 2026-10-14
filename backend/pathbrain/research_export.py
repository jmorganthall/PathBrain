"""The research export: the whole measured record, in one file, for analysis off the box.

Phase 1 of ``docs/plan-learning-engine.md``. Every later phase — the noise audits, the
simulator, the surrogate model — has to be fitted to *this link's* data, and the two
exports that already existed cannot carry it: ``/history/dump`` builds one JSON document
in memory and caps at 500 runs, and the optimizer export is profile-centric with no
per-iteration readings, no duel round margins and no weather. Both were built to be read
by a page or a prompt; this one is built to be read by a model.

So it is a **background job** that **streams** a ``.zip`` of JSON-lines files — one record
per line, written as it is read, so a 100k-run history never sits in memory and the
request thread never waits on it:

* ``manifest.json`` — what was exported, under which build and methodology, with a count
  per file and the list of what was deliberately left out.
* ``config.json`` — the effective runtime config, scrubbed.
* ``methodologies.jsonl`` — every published version's frozen definition.
* ``profiles.jsonl`` — every profile: call sign, settings (newest run's), first/last seen.
* ``runs.jsonl`` — every run: timing, status, profile, methodology, machine health, the
  site/client stamps, per-plugin success, **each iteration's registry metrics** (from the
  cached ``details['iteration_metrics']`` — the raw blobs are never loaded for this), the
  browser's phase timings, and its scores (full detail under the current methodology,
  comparability + Overall under every other).
* ``duels.jsonl`` / ``lever_campaigns.jsonl`` — every session's matches with their
  per-round margins, leg run ids, weather shifts and endings.
* ``explore_recommendations.jsonl`` / ``crown_events.jsonl`` — the prediction ledger and
  the crown's history.
* ``site_metrics.jsonl`` — **optional**, off by default: the registry's browser metrics per
  run × iteration × site, re-derived from raw through the one ``interpret.derive``. The
  only way to measure whether per-site paired margins cut duel noise; it costs a raw load
  and a derivation per run, so it is priced (``estimate``) before anyone asks for it.

**Left out, always**: API keys and tokens, the firewall's address and credentials (they are
env-only and never in the database, and the scrubber removes any key that looks like one
anyway), the AI config row, and the Away test's runs (device egress addresses and the ISP
looked up from them). The scrubber also redacts any bare IPv4 address in a string value, so
an address that wandered into a free-text field cannot leave the box in an export.

Read-only throughout: rows in, a file out. Nothing here changes a score.
"""
from __future__ import annotations

import json
import re
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from sqlalchemy import func, select

from .config import get_settings
from .logging_config import get_logger
from .metrics import all_metric_sources
from .models import (
    BenchmarkResult,
    CrownEvent,
    Duel,
    ExploreRecommendation,
    LeverCampaign,
    Methodology,
    ProfileName,
    Run,
    Score,
)

log = get_logger("research_export")

#: Bump when a file's record shape changes, so an analysis can tell which reader it needs.
SCHEMA_VERSION = 1
#: Runs read per batch. Bounded memory: one batch of rows (no raw) at a time.
BATCH = 500
#: Exports kept on disk; older ones are pruned when a new one finishes.
KEEP = 3
#: Runs timed to price the per-site pass.
ESTIMATE_SAMPLE = 5

#: What is deliberately never exported — stated in the manifest so the omission is visible.
EXCLUDED = [
    "API keys, tokens, passwords and secrets (any config key that looks like one)",
    "the firewall address and credentials (env-only; never stored in the database)",
    "the AI/OpenRouter config row",
    "Away-test runs (device egress addresses and ISP lookups)",
    "bare IPv4 addresses in any string value (redacted)",
    "raw Resource Timing / LoAF blobs (size); per-site derived values are the optional site_metrics.jsonl",
]

_SECRET_KEY = re.compile(
    r"(^|_)(api_?key|token|secret|password|passwd|credentials?|auth)$|home_ip|egress_ip|ip_lookup",
    re.IGNORECASE,
)
# Not after a "/" or a dot: a user agent's "Chrome/126.0.0.0" is a version, not an address.
_IPV4 = re.compile(r"(?<![\w.])(?<!\w/)(?:\d{1,3}\.){3}\d{1,3}(?![\w.])")

#: Tests point the exports somewhere disposable.
_DIR_OVERRIDE: Path | None = None


def export_dir() -> Path:
    """Beside the database's other runtime data (``./data/exports`` by default)."""
    if _DIR_OVERRIDE is not None:
        return _DIR_OVERRIDE
    return Path(get_settings().artifact_dir).resolve().parent / "exports"


def scrub(value: Any) -> Any:
    """Drop secret-looking keys and redact bare IPv4 addresses, recursively."""
    if isinstance(value, dict):
        return {
            k: scrub(v) for k, v in value.items()
            if not (isinstance(k, str) and _SECRET_KEY.search(k))
        }
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, str):
        return _IPV4.sub("[redacted-ip]", value)
    return value


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)  # stored naive UTC
    return dt.isoformat()


def _line(record: dict) -> bytes:
    return (json.dumps(scrub(record), separators=(",", ":"), default=str) + "\n").encode()


def _metric_maps() -> dict[str, dict[str, str]]:
    """``{plugin: {source_key: metric_key}}`` for every registry metric — the vocabulary
    the per-iteration readings are exported in."""
    by_plugin: dict[str, dict[str, str]] = {}
    for key, (plugin, source) in all_metric_sources().items():
        by_plugin.setdefault(plugin, {})[source] = key
    return by_plugin


def _iteration_metrics(details: dict | None, sources: dict[str, str]) -> list[dict]:
    """One ``{metric_key: value}`` per iteration, registry metrics only."""
    out = []
    for it in (details or {}).get("iteration_metrics") or []:
        it = it or {}
        out.append({key: it[src] for src, key in sources.items() if it.get(src) is not None})
    return out


def _date_filter(stmt, column, since: datetime | None, until: datetime | None):
    if since is not None:
        stmt = stmt.where(column >= since)
    if until is not None:
        stmt = stmt.where(column < until)
    return stmt


def _run_ids(session, since, until) -> list[int]:
    stmt = _date_filter(select(Run.id), Run.created_at, since, until).order_by(Run.id)
    return list(session.scalars(stmt))


def _batches(ids: list[int], size: int = BATCH) -> Iterator[list[int]]:
    for i in range(0, len(ids), size):
        yield ids[i:i + size]


def estimate(session, since: datetime | None = None, until: datetime | None = None) -> dict:
    """How big an export is, and what the optional per-site pass would cost.

    The per-site price is measured, not guessed: a few recent browser raws are loaded and
    derived per page exactly as the export would, and the mean is multiplied out."""
    run_ids = _run_ids(session, since, until)
    browser_ids = list(session.scalars(
        select(BenchmarkResult.id)
        .where(BenchmarkResult.plugin == "browser", BenchmarkResult.run_id.in_(run_ids[-BATCH:]))
        .order_by(BenchmarkResult.id.desc())
        .limit(ESTIMATE_SAMPLE)
    )) if run_ids else []
    per_raw_s = None
    if browser_ids:
        browser_sources = _metric_maps().get("browser", {})
        started = time.monotonic()
        for rid in browser_ids:
            raw = session.scalar(select(BenchmarkResult.raw).where(BenchmarkResult.id == rid))
            for _ in _site_rows(0, raw, browser_sources):
                pass
        per_raw_s = (time.monotonic() - started) / len(browser_ids)
    browser_runs = 0
    for batch in _batches(run_ids):
        browser_runs += session.scalar(
            select(func.count(BenchmarkResult.id)).where(
                BenchmarkResult.plugin == "browser", BenchmarkResult.run_id.in_(batch)
            )
        ) or 0
    return {
        "runs": len(run_ids),
        "browser_results": browser_runs,
        "duels": session.scalar(_date_filter(select(func.count(Duel.id)), Duel.created_at, since, until)) or 0,
        "per_site_seconds": round(per_raw_s * browser_runs, 1) if per_raw_s is not None else None,
        "per_site_basis": f"{len(browser_ids)} raw(s) timed" if browser_ids else "no browser raw to time",
    }


def _site_rows(
    run_id: int, raw: dict | None, browser_sources: dict[str, str], skipped: list | None = None
) -> Iterator[dict]:
    """The registry's browser metrics for each iteration × site of one run, derived from raw
    through the one derivation (a one-page browser raw derives that page's own values).

    A page whose stored raw the derivation cannot read is skipped and counted, never fatal:
    one malformed observation from years ago must not cost the whole export."""
    from .interpret.derive import derive
    from .raw_access import browser_url_observations

    for i, url, obs in browser_url_observations(raw):
        if "nav" not in obs:
            continue
        try:
            derived = derive("browser", {"urls": {url: obs}})
        except Exception:  # noqa: BLE001 — counted in the manifest, never fatal
            if skipped is not None:
                skipped.append({"run_id": run_id, "iteration": i, "url": url})
            continue
        metrics = {key: derived[src] for src, key in browser_sources.items() if derived.get(src) is not None}
        if metrics:
            yield {"run_id": run_id, "iteration": i, "url": url, "metrics": metrics}


def _write_jsonl(zf: zipfile.ZipFile, name: str, records: Iterable[dict]) -> int:
    n = 0
    with zf.open(name, "w", force_zip64=True) as fh:
        for rec in records:
            fh.write(_line(rec))
            n += 1
    return n


def _profiles(session, fingerprints: set[str]) -> Iterator[dict]:
    names = dict(session.execute(select(ProfileName.fingerprint, ProfileName.name)).all())
    stats = {
        fp: (first, last, n, newest)
        for fp, first, last, n, newest in session.execute(
            select(
                Run.settings_fingerprint,
                func.min(Run.created_at),
                func.max(Run.created_at),
                func.count(Run.id),
                func.max(Run.id),
            ).where(Run.settings_fingerprint.is_not(None)).group_by(Run.settings_fingerprint)
        ).all()
        if fp in fingerprints
    }
    for fp in sorted(stats):
        first, last, n, newest = stats[fp]
        settings = session.scalar(select(Run.settings).where(Run.id == newest))
        yield {
            "fingerprint": fp,
            "name": names.get(fp),
            "settings": settings,
            "first_seen": _iso(first),
            "last_seen": _iso(last),
            "runs": n,
        }


def _runs(session, run_ids: list[int], current: str, by_plugin, progress: Callable[[int], None],
          fingerprints: set[str]) -> Iterator[dict]:
    from .methodology import client_set_from_config, site_set_from_config

    done = 0
    for batch in _batches(run_ids):
        runs = session.execute(
            select(
                Run.id, Run.created_at, Run.started_at, Run.finished_at, Run.status, Run.label,
                Run.error, Run.iterations, Run.iterations_completed, Run.per_iteration_ms,
                Run.job_group, Run.settings_fingerprint, Run.methodology_version,
                Run.instrument_health, Run.host_pressure, Run.config_used,
            ).where(Run.id.in_(batch)).order_by(Run.id)
        ).all()
        results: dict[int, list] = {}
        for row in session.execute(
            select(
                BenchmarkResult.run_id, BenchmarkResult.plugin, BenchmarkResult.success,
                BenchmarkResult.error, BenchmarkResult.duration_ms, BenchmarkResult.details,
            ).where(BenchmarkResult.run_id.in_(batch))
        ).all():
            results.setdefault(row.run_id, []).append(row)
        # Every version's verdict, but the heavy per-metric blobs only for the current one:
        # a long history carries a score row per published version per run.
        scores: dict[int, list] = {}
        for row in session.execute(
            select(
                Score.run_id, Score.methodology_version, Score.is_at_measure, Score.comparability,
                Score.missing_metrics, Score.axis_scores,
            ).where(Score.run_id.in_(batch))
        ).all():
            scores.setdefault(row.run_id, []).append(row)
        detail = {
            row.run_id: row
            for row in session.execute(
                select(Score.run_id, Score.subscores, Score.metric_values).where(
                    Score.run_id.in_(batch), Score.methodology_version == current
                )
            ).all()
        }

        for r in runs:
            if r.settings_fingerprint:
                fingerprints.add(r.settings_fingerprint)
            plugins = {}
            for res in results.get(r.id, []):
                details = res.details or {}
                entry = {
                    "success": res.success,
                    "error": res.error,
                    "duration_ms": res.duration_ms,
                    "iterations": _iteration_metrics(details, by_plugin.get(res.plugin, {})),
                }
                if res.plugin == "browser":
                    entry["phases"] = details.get("phases")
                    entry["client"] = details.get("client")
                plugins[res.plugin] = entry
            score_out = {}
            for s in scores.get(r.id, []):
                rec = {
                    "at_measure": s.is_at_measure,
                    "comparability": s.comparability,
                    "missing": s.missing_metrics,
                    "overall": (s.axis_scores or {}).get("overall"),
                }
                if s.methodology_version == current and r.id in detail:
                    d = detail[r.id]
                    rec.update(axis_scores=s.axis_scores, subscores=d.subscores, metric_values=d.metric_values)
                score_out[s.methodology_version] = rec
            yield {
                "id": r.id,
                "created_at": _iso(r.created_at),
                "started_at": _iso(r.started_at),
                "finished_at": _iso(r.finished_at),
                "status": getattr(r.status, "value", r.status),
                "label": r.label,
                "error": r.error,
                "iterations": r.iterations,
                "iterations_completed": r.iterations_completed,
                "per_iteration_ms": r.per_iteration_ms,
                "job_group": r.job_group,
                "fingerprint": r.settings_fingerprint,
                "methodology_version": r.methodology_version,
                "instrument_health": r.instrument_health,
                "host_pressure": r.host_pressure,
                "site_set": site_set_from_config(r.config_used),
                "client_set": client_set_from_config(r.config_used),
                "plugins": plugins,
                "scores": score_out,
            }
        done += len(batch)
        progress(done)


def _site_metrics(
    session, run_ids: list[int], browser_sources, progress: Callable[[int], None], skipped: list
) -> Iterator[dict]:
    done = 0
    for batch in _batches(run_ids):
        ids = session.execute(
            select(BenchmarkResult.id, BenchmarkResult.run_id).where(
                BenchmarkResult.plugin == "browser", BenchmarkResult.run_id.in_(batch)
            ).order_by(BenchmarkResult.run_id)
        ).all()
        for res_id, run_id in ids:
            # One raw per statement, dropped before the next is loaded: peak memory is one raw.
            raw = session.scalar(select(BenchmarkResult.raw).where(BenchmarkResult.id == res_id))
            yield from _site_rows(run_id, raw, browser_sources, skipped)
            del raw
        session.expire_all()
        done += len(batch)
        progress(done)


def _rows(session, model, since, until, order) -> Iterator[dict]:
    stmt = _date_filter(select(model), model.created_at, since, until).order_by(order)
    for obj in session.scalars(stmt):
        rec = {}
        for col in model.__table__.columns:
            v = getattr(obj, col.key, None)
            rec[col.key] = _iso(v) if isinstance(v, datetime) else getattr(v, "value", v)
        yield rec


def build(
    session,
    *,
    since: datetime | None = None,
    until: datetime | None = None,
    per_site: bool = False,
    progress: Callable[[int, int | None, str | None], None] | None = None,
) -> Path:
    """Write one export and return its path. ``progress(current, total, message)``."""
    from .config_store import get_config
    from .methodology import current_version

    report = progress or (lambda *_a, **_k: None)
    config = get_config(session)
    current = current_version(config)
    by_plugin = _metric_maps()
    browser_sources = by_plugin.get("browser", {})
    run_ids = _run_ids(session, since, until)
    total = len(run_ids) * (2 if per_site else 1)

    out_dir = export_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    final = out_dir / f"pathbrain-research-{stamp}.zip"
    partial = final.with_suffix(".zip.partial")
    counts: dict[str, int] = {}
    fingerprints: set[str] = set()
    site_skipped: list[dict] = []

    with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        counts["runs.jsonl"] = _write_jsonl(zf, "runs.jsonl", _runs(
            session, run_ids, current, by_plugin,
            lambda n: report(n, total, f"runs {n}/{len(run_ids)}"), fingerprints,
        ))
        if per_site:
            counts["site_metrics.jsonl"] = _write_jsonl(zf, "site_metrics.jsonl", _site_metrics(
                session, run_ids, browser_sources,
                lambda n: report(len(run_ids) + n, total, f"per-site {n}/{len(run_ids)}"),
                site_skipped,
            ))
        counts["profiles.jsonl"] = _write_jsonl(zf, "profiles.jsonl", _profiles(session, fingerprints))
        counts["methodologies.jsonl"] = _write_jsonl(zf, "methodologies.jsonl", (
            {
                "version": m.version, "rubric_version": m.rubric_version,
                "derivation_version": m.derivation_version, "created_at": _iso(m.created_at),
                "is_current": m.is_current, "notes": m.notes, "definition": m.definition,
            }
            for m in session.scalars(select(Methodology).order_by(Methodology.created_at))
        ))
        for name, model in (
            ("duels.jsonl", Duel),
            ("lever_campaigns.jsonl", LeverCampaign),
            ("explore_recommendations.jsonl", ExploreRecommendation),
            ("crown_events.jsonl", CrownEvent),
        ):
            counts[name] = _write_jsonl(zf, name, _rows(session, model, since, until, model.id))
        with zf.open("config.json", "w") as fh:
            fh.write(json.dumps(scrub(config), indent=2, default=str).encode())
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "git_sha": get_settings().git_sha or None,
            "current_methodology": current,
            "filters": {"since": _iso(since), "until": _iso(until), "per_site": per_site},
            "counts": counts,
            # Pages whose stored raw could not be derived (per-site pass only): first 100 named.
            "site_metrics_skipped": len(site_skipped),
            "site_metrics_skipped_sample": site_skipped[:100],
            "excluded": EXCLUDED,
            "notes": {
                "runs.jsonl": "plugins.<plugin>.iterations = one {metric_key: value} per iteration, "
                "registry metrics only; scores keyed by methodology version (full detail only "
                "for current_methodology).",
                "duels.jsonl": "matchups[].deltas = per-round margins (challenger minus "
                "reference, Overall points); run_ids in leg order.",
            },
        }
        with zf.open("manifest.json", "w") as fh:
            fh.write(json.dumps(manifest, indent=2).encode())
    partial.replace(final)
    _prune(out_dir)
    log.info("Research export written: %s (%s)", final.name, counts)
    return final


def _prune(out_dir: Path) -> None:
    for old in sorted(out_dir.glob("pathbrain-research-*.zip"), reverse=True)[KEEP:]:
        try:
            old.unlink()
        except OSError:
            log.warning("Could not prune old export %s", old)


def list_exports() -> list[dict]:
    out_dir = export_dir()
    if not out_dir.exists():
        return []
    return [
        {
            "name": p.name,
            "bytes": p.stat().st_size,
            "created_at": datetime.fromtimestamp(p.stat().st_mtime, timezone.utc).isoformat(),
        }
        for p in sorted(out_dir.glob("pathbrain-research-*.zip"), reverse=True)
    ]


def export_path(name: str) -> Path | None:
    """The file for a listed export name — never a path a caller composed."""
    if not re.fullmatch(r"pathbrain-research-\d{8}T\d{6}Z\.zip", name or ""):
        return None
    p = export_dir() / name
    return p if p.is_file() else None
