"""Config endpoints: benchmark config + firewall discovery/snapshots."""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config_store import default_rubric, get_config, reset_config, save_config
from ..database import get_session
from ..logging_config import get_logger
from ..models import ConfigSnapshot
from ..providers import get_provider
from ..schemas import ConfigSnapshotOut, ConfigUpdate, DiscoverOut

router = APIRouter()
log = get_logger("api.config")


@router.get("/config")
def read_config(session: Session = Depends(get_session)) -> dict:
    """Effective benchmark configuration (targets, weights, thresholds)."""
    return get_config(session)


@router.put("/config")
def update_config(
    payload: ConfigUpdate, session: Session = Depends(get_session)
) -> dict:
    """Update (merge) benchmark configuration."""
    new_config = payload.model_dump(exclude_unset=True)
    return save_config(session, new_config)


@router.post("/config/reset")
def reset(session: Session = Depends(get_session)) -> dict:
    """Reset benchmark configuration to defaults."""
    return reset_config(session)


@router.post("/config/adopt-rubric")
def adopt_rubric(session: Session = Depends(get_session)) -> dict:
    """Adopt the latest default scoring rubric (perception-calibrated weights +
    thresholds + version), leaving targets/monitoring untouched."""
    return save_config(session, default_rubric())


@router.get("/config/provider")
def provider_health() -> dict:
    """Connectivity/health of the configured discovery provider."""
    provider = get_provider()
    return provider.health()


def _discover_target(provider, uuid_hint: str | None):
    """Discover and return (configs, the pipe to test). Prefers ``uuid_hint``,
    else the first pipe that exposes a numeric quantum."""
    configs = provider.discover()
    target = None
    if uuid_hint:
        target = next((c for c in configs if (c.extra or {}).get("uuid") == uuid_hint), None)
    if target is None:
        target = next((c for c in configs if c.quantum is not None), None)
    return configs, target


def _write_roundtrip(provider, session: Session, uuid_hint: str | None) -> dict:
    """Prove the firewall *write* path works, reversibly: nudge quantum by +1 then set it
    straight back, verifying the change took effect at each step. Snapshots the baseline
    first and *always* attempts to restore. Raises ``HTTPException`` on the hard
    pre-conditions (discovery failure / no numeric-quantum pipe) so callers can surface
    them; otherwise returns the detailed result dict. Shared by ``test_apply`` (the button)
    and the access check (its write probe)."""
    steps: list[dict] = []

    try:
        _, target = _discover_target(provider, uuid_hint)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"{provider.name} discovery failed: {type(exc).__name__}: {exc}",
        ) from exc
    if target is None or target.quantum is None:
        raise HTTPException(
            status_code=400, detail="No shaper pipe with a numeric quantum to test against."
        )

    uuid = (target.extra or {}).get("uuid") or uuid_hint
    label = (target.extra or {}).get("description") or (target.extra or {}).get("pipe") or uuid
    original = int(target.quantum)
    test_value = original + 1
    steps.append({"step": "discover", "ok": True, "detail": f"{label}: quantum = {original}"})

    result = {
        "provider": provider.name,
        "pipe_uuid": uuid,
        "pipe_label": label,
        "param": "quantum",
        "original": original,
        "test_value": test_value,
        "changed": False,
        "restored": False,
        "ok": False,
        "error": None,
        "steps": steps,
    }

    # Snapshot the baseline for safety (best-effort).
    try:
        session.add(
            ConfigSnapshot(provider=provider.name, label="test-apply baseline", data=provider.snapshot())
        )
        session.commit()
    except Exception:  # noqa: BLE001
        log.warning("test-apply: baseline snapshot failed", exc_info=True)

    def _change(value: int, step: str) -> bool:
        try:
            provider.apply({"pipe_uuid": uuid, "param": "quantum", "value": value})
            steps.append({"step": step, "ok": True, "detail": f"set quantum = {value}"})
            return True
        except NotImplementedError:
            steps.append({"step": step, "ok": False, "detail": f"{provider.name} cannot apply changes"})
            result["error"] = f"The {provider.name} provider cannot apply changes."
            return False
        except Exception as exc:  # noqa: BLE001
            steps.append({"step": step, "ok": False, "detail": f"{type(exc).__name__}: {exc}"})
            result["error"] = f"Apply failed: {type(exc).__name__}: {exc}"
            return False

    def _read() -> int | None:
        _, t = _discover_target(provider, uuid)
        return int(t.quantum) if t and t.quantum is not None else None

    # 1) Nudge +1.
    if not _change(test_value, "apply +1"):
        return result  # nothing applied — nothing to restore
    # 2) Verify the change landed.
    try:
        got = _read()
        result["changed"] = got == test_value
        steps.append({"step": "verify change", "ok": result["changed"], "detail": f"read back {got}"})
    except Exception as exc:  # noqa: BLE001
        steps.append({"step": "verify change", "ok": False, "detail": f"{type(exc).__name__}: {exc}"})
    # 3) Restore — always attempt, even if verification failed.
    if not _change(original, "restore"):
        result["error"] = (
            f"RESTORE FAILED — quantum may still be {test_value}. Set it back to {original} "
            f"manually. ({result['error']})"
        )
        return result
    # 4) Verify restore.
    try:
        got = _read()
        result["restored"] = got == original
        steps.append({"step": "verify restore", "ok": result["restored"], "detail": f"read back {got}"})
    except Exception as exc:  # noqa: BLE001
        steps.append({"step": "verify restore", "ok": False, "detail": f"{type(exc).__name__}: {exc}"})

    result["ok"] = bool(result["changed"] and result["restored"])
    log.info("test-apply via %s: ok=%s (quantum %s↔%s)", provider.name, result["ok"], original, test_value)
    return result


@router.post("/config/test-apply")
def test_apply(
    body: dict | None = Body(default=None), session: Session = Depends(get_session)
) -> dict:
    """Verify the firewall *write* path, reversibly (see ``_write_roundtrip``).

    This is the only safe way to confirm ``provider.apply()`` round-trips before arming an
    experiment. Optional body: ``{"pipe_uuid": str}`` to target a specific pipe.
    """
    return _write_roundtrip(get_provider(), session, (body or {}).get("pipe_uuid"))


@router.post("/config/access-check")
def access_check(
    body: dict | None = Body(default=None), session: Session = Depends(get_session)
) -> dict:
    """Report exactly what the configured firewall credential can *do* — a permissions
    self-test that answers "does this API key have read-only access to performance data,
    or only what it needs to write the shaper?".

    Runs the provider's read-only capability probes (``access_checks()`` — config reads
    plus, for OPNsense, the CPU / interface-throughput / system-resource diagnostics
    endpoints) and, unless ``{"include_write": false}``, the same reversible +1/−1 write
    round-trip as ``test-apply`` folded in as a ``write_shaper`` capability. Optional body:
    ``{"pipe_uuid": str, "include_write": bool}``.

    Returns ``{provider, wrote, checks: [{key, label, category, ok, detail, ...}]}`` so the
    UI can render a pass/fail breakdown per capability.
    """
    provider = get_provider()
    body = body or {}
    include_write = body.get("include_write", True)
    uuid_hint = body.get("pipe_uuid")

    checks = list(provider.access_checks())

    if include_write:
        try:
            wr = _write_roundtrip(provider, session, uuid_hint)
            checks.append(
                {
                    "key": "write_shaper",
                    "label": "Write shaper config",
                    "category": "write",
                    "ok": bool(wr.get("ok")),
                    "detail": (
                        f"reversible round-trip {wr.get('param')} "
                        f"{wr.get('original')}→{wr.get('test_value')}→{wr.get('original')}"
                        + ("" if wr.get("restored") else " — ⚠ original may not be restored")
                    ),
                    "write_result": wr,
                }
            )
        except HTTPException as exc:
            # Couldn't run the write probe (discovery failed / no numeric-quantum pipe).
            # That's a reportable capability result, not a failure of the whole check.
            checks.append(
                {
                    "key": "write_shaper",
                    "label": "Write shaper config",
                    "category": "write",
                    "ok": None,
                    "detail": f"could not test: {exc.detail}",
                }
            )
    else:
        writable = provider.writable_fields()
        checks.append(
            {
                "key": "write_shaper",
                "label": "Write shaper config",
                "category": "write",
                "ok": bool(writable) or None,
                "detail": (
                    f"provider declares {len(writable)} writable field(s): "
                    f"{', '.join(writable)} — run with the write test to verify the credential"
                    if writable
                    else "this provider cannot apply changes"
                ),
            }
        )

    ok_count = sum(1 for c in checks if c.get("ok") is True)
    log.info(
        "access-check via %s: %d/%d capabilities ok (write tested=%s)",
        provider.name, ok_count, len(checks), include_write,
    )
    return {"provider": provider.name, "wrote": bool(include_write), "checks": checks}


@router.post("/config/discover", response_model=DiscoverOut)
def discover(session: Session = Depends(get_session)) -> DiscoverOut:
    """Discover FQ-CoDel settings from the firewall and store a snapshot."""
    provider = get_provider()
    try:
        configs = provider.discover()
        snapshot_data = provider.snapshot()
    except Exception as exc:  # noqa: BLE001 — surface provider failures clearly
        log.exception("Discovery failed via provider '%s'", provider.name)
        raise HTTPException(
            status_code=502,
            detail=f"{provider.name} discovery failed: {type(exc).__name__}: {exc}",
        ) from exc

    snapshot = ConfigSnapshot(
        provider=provider.name,
        label="discovery",
        data=snapshot_data,
    )
    session.add(snapshot)
    session.commit()
    log.info("Stored config snapshot %s from provider '%s'", snapshot.id, provider.name)

    return DiscoverOut(
        provider=provider.name,
        pipes=[c.to_dict() for c in configs],
        snapshot_id=snapshot.id,
    )


@router.get("/config/snapshots", response_model=list[ConfigSnapshotOut])
def list_snapshots(session: Session = Depends(get_session)) -> list[ConfigSnapshot]:
    return list(
        session.scalars(
            select(ConfigSnapshot).order_by(ConfigSnapshot.created_at.desc()).limit(50)
        ).all()
    )
