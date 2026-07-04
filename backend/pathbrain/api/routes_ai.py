"""AI endpoints: configure OpenRouter, list models, and get profile suggestions.

The API key lives in its own ``AppConfig`` row and is only ever returned masked
(``ai.public_config``). ``/ai/suggest`` builds the same optimizer export the Data Dump
page offers, sends it to the chosen model, and returns the parsed profile suggestions.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from .. import ai
from ..database import get_session
from ..logging_config import get_logger
from ..schemas import AiConfigUpdate, AiSuggest
from .routes_settings import build_optimizer_export

router = APIRouter()
log = get_logger("api.ai")


@router.get("/ai/config")
def get_ai_config(session: Session = Depends(get_session)) -> dict:
    """The AI settings for the UI — API key masked to a hint, plus model, prompt, default prompt."""
    return ai.public_config(session)


@router.put("/ai/config")
def update_ai_config(payload: AiConfigUpdate, session: Session = Depends(get_session)) -> dict:
    """Save the API key / model / prompt. A blank/absent key leaves the stored one untouched."""
    return ai.save_ai_config(session, payload.model_dump(exclude_none=True))


@router.delete("/ai/config/key")
def clear_ai_key(session: Session = Depends(get_session)) -> dict:
    """Forget the stored OpenRouter API key."""
    return ai.clear_api_key(session)


@router.get("/ai/models")
def list_models(session: Session = Depends(get_session)) -> dict:
    """The OpenRouter model catalog for the picker (502 if OpenRouter can't be reached)."""
    try:
        return {"models": ai.list_models(session)}
    except ai.AIError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/ai/suggest")
def suggest(payload: AiSuggest, session: Session = Depends(get_session)) -> dict:
    """Build the optimizer export, send it to the model, and return parsed profile suggestions."""
    export = build_optimizer_export(session, payload.runs_per_profile, payload.profile_limit)
    # Log the payload size so an oversized-request ceiling (a model's context limit, an upstream
    # body cap) is diagnosable rather than surfacing to the UI as an opaque failure.
    payload_bytes = len(json.dumps(export))
    log.info(
        "AI suggest: %s profile(s), %s runs/profile, ~%s KB payload, model=%s",
        export.get("profile_count"),
        payload.runs_per_profile,
        round(payload_bytes / 1024),
        payload.model or "(saved)",
    )
    try:
        result = ai.suggest(session, export, model=payload.model, prompt=payload.prompt)
    except ai.AIError as exc:
        # Config problems (no key/model) → 400; upstream/network failures → 502.
        msg = str(exc)
        code = 400 if "configured" in msg or "selected" in msg else 502
        raise HTTPException(status_code=code, detail=msg) from exc
    # Echo how many profiles were sent + the payload size, so the UI can show what the model saw
    # (and hint when a big selection is the reason a request is slow/oversized).
    result["profiles_sent"] = export.get("profile_count")
    result["payload_bytes"] = payload_bytes
    # The deterministic settings→outcome relationships we computed and sent — surfaced so the UI
    # can show them regardless of what the model returns.
    analysis = export.get("analysis") or {}
    result["field_sensitivity"] = analysis.get("field_sensitivity") or []
    result["top_profile_signature"] = analysis.get("top_profile_signature") or {}
    result["coverage_gaps"] = analysis.get("coverage_gaps") or []
    return result


@router.post("/ai/suggest/stream")
def stream_suggest(payload: AiSuggest, session: Session = Depends(get_session)) -> StreamingResponse:
    """Stream the suggestion request as Server-Sent Events — the model's reasoning trace and
    answer arrive token-by-token, so a long request keeps the connection alive (no opaque
    timeout) and the UI shows progress live.

    Emits ``data: {json}`` events: a first ``{"type":"meta", …}`` (profiles/size), then
    ``reasoning``/``content`` deltas, then a terminal ``done`` (parsed suggestions) or ``error``.
    Config secrets are resolved here while the DB session is live; the generator itself is
    session-free so it can run after the request scope closes."""
    cfg = ai.get_ai_config(session)
    api_key = cfg["api_key"]
    model = payload.model or cfg["model"] or ""
    prompt = payload.prompt if (payload.prompt and payload.prompt.strip()) else cfg["prompt"]
    export = build_optimizer_export(session, payload.runs_per_profile, payload.profile_limit)
    profiles_sent = export.get("profile_count")
    payload_bytes = len(json.dumps(export))
    log.info(
        "AI suggest (stream): %s profile(s), ~%s KB payload, model=%s",
        profiles_sent, round(payload_bytes / 1024), model or "(saved)",
    )

    def _sse(obj: dict) -> str:
        return f"data: {json.dumps(obj)}\n\n"

    def gen():
        yield _sse({
            "type": "meta",
            "profiles_sent": profiles_sent,
            "payload_bytes": payload_bytes,
            "model": model,
            # The deterministic settings→outcome map + top-profile signature we computed and sent,
            # so the UI can show them immediately (before the model finishes reasoning).
            "field_sensitivity": (export.get("analysis") or {}).get("field_sensitivity") or [],
            "top_profile_signature": (export.get("analysis") or {}).get("top_profile_signature") or {},
            "coverage_gaps": (export.get("analysis") or {}).get("coverage_gaps") or [],
        })
        for evt in ai.suggest_stream(export, api_key, model, prompt):
            yield _sse(evt)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        # Defeat proxy/browser buffering so events flush as they're produced.
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
