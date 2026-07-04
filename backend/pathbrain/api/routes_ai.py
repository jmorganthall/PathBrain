"""AI endpoints: configure OpenRouter, list models, and get profile suggestions.

The API key lives in its own ``AppConfig`` row and is only ever returned masked
(``ai.public_config``). ``/ai/suggest`` builds the same optimizer export the Data Dump
page offers, sends it to the chosen model, and returns the parsed profile suggestions.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
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
    import json

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
    return result
