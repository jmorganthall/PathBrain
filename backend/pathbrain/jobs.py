"""In-process background-job registry with progress, for the "running jobs" feed.

Long operations (re-grade / re-score / re-derive history) used to run synchronously
in the request, so the UI got no feedback and a slow pass looked like a failure. This
registry runs such work on a daemon thread and tracks its status + progress so the
top-right jobs dropdown can show it live (``GET /api/jobs``).

Deliberately in-memory and lightweight: durable operations (benchmark runs, sweeps,
profile tests, experiments) already persist in their own DB tables and are surfaced in
the same feed via read-only adapters (see ``api/routes_jobs.py``), so the only thing
lost on restart is the *history* of finished score jobs — which is fine.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from .logging_config import get_logger

log = get_logger("jobs")

# Keep at most this many jobs (running + recently finished), newest first.
_MAX_JOBS = 25
# Prune finished jobs older than this many seconds on each list().
_FINISHED_TTL_S = 3600.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Job:
    """One tracked background operation."""

    id: int
    kind: str                       # "regrade" | "rescore" | "rederive" | …
    label: str                      # human label for the dropdown
    status: str = "running"         # running | succeeded | failed
    current: int | None = None      # progress numerator (optional)
    total: int | None = None        # progress denominator (optional)
    message: str | None = None      # latest progress / final summary line
    error: str | None = None
    href: str | None = None         # optional deep-link (e.g. "/methodology")
    started_at: datetime = field(default_factory=_now)
    finished_at: datetime | None = None

    def set_progress(self, current: int, total: int | None = None, message: str | None = None) -> None:
        """Worker-side progress update (thread-safe enough for simple counters)."""
        self.current = current
        if total is not None:
            self.total = total
        if message is not None:
            self.message = message

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "kind": self.kind,
            "label": self.label,
            "status": self.status,
            "current": self.current,
            "total": self.total,
            "message": self.message,
            "error": self.error,
            "href": self.href,
            "started_at": self.started_at.isoformat(),
            "finished_at": self.finished_at.isoformat() if self.finished_at else None,
        }


_jobs: dict[int, Job] = {}
_lock = threading.Lock()
_next_id = 1


def _running_of_kind(kind: str) -> Job | None:
    return next((j for j in _jobs.values() if j.kind == kind and j.status == "running"), None)


def start(kind: str, label: str, fn: Callable[[Job], object], *, href: str | None = None) -> str:
    """Register a job and run ``fn(job)`` on a daemon thread.

    ``fn`` may call ``job.set_progress(...)`` and may return a short summary, which is
    stored as the job's final ``message``. If a job of the same ``kind`` is already
    running, no new one starts — the existing job's id is returned (so the same heavy
    pass can't be kicked off twice concurrently).
    """
    global _next_id
    with _lock:
        existing = _running_of_kind(kind)
        if existing is not None:
            log.info("Job kind %s already running (#%s); not starting another", kind, existing.id)
            return str(existing.id)
        job = Job(id=_next_id, kind=kind, label=label, href=href)
        _jobs[job.id] = job
        _next_id += 1
        _prune_locked()

    def _run() -> None:
        log.info("Job %s (%s) started", job.id, kind)
        try:
            result = fn(job)
            job.status = "succeeded"
            # On completion, surface the final outcome (overriding any in-progress
            # "scored 42/120" chatter with the summary).
            if isinstance(result, str):
                job.message = result
            elif isinstance(result, dict):
                job.message = _summarize(result)
            log.info("Job %s (%s) succeeded", job.id, kind)
        except Exception as exc:  # noqa: BLE001 — record on the job, never crash the thread
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            log.exception("Job %s (%s) failed", job.id, kind)
        finally:
            job.finished_at = _now()

    threading.Thread(target=_run, name=f"pathbrain-job-{kind}-{job.id}", daemon=True).start()
    return str(job.id)


def _summarize(result: dict) -> str:
    """Compact one-line summary of a worker's returned dict (best-effort)."""
    keys = ("scored", "partial", "incomparable", "rescored", "rederived", "skipped", "errors")
    parts = [f"{k} {result[k]}" for k in keys if isinstance(result.get(k), int)]
    return " · ".join(parts) if parts else "done"


def _prune_locked() -> None:
    """Drop old finished jobs (caller holds ``_lock``)."""
    now = _now()
    finished = [
        j for j in _jobs.values()
        if j.finished_at is not None and (now - j.finished_at).total_seconds() > _FINISHED_TTL_S
    ]
    for j in finished:
        _jobs.pop(j.id, None)
    # Cap total: keep newest, prefer dropping finished ones first.
    if len(_jobs) > _MAX_JOBS:
        ordered = sorted(
            _jobs.values(),
            key=lambda j: (j.status == "running", j.started_at),  # running last → kept
        )
        for j in ordered[: len(_jobs) - _MAX_JOBS]:
            if j.status != "running":
                _jobs.pop(j.id, None)


def list_jobs() -> list[dict]:
    """All tracked jobs, newest first (running + recently finished)."""
    with _lock:
        _prune_locked()
        return [j.to_dict() for j in sorted(_jobs.values(), key=lambda j: j.id, reverse=True)]


def get(job_id: int) -> Job | None:
    return _jobs.get(job_id)


def running_count() -> int:
    return sum(1 for j in _jobs.values() if j.status == "running")
