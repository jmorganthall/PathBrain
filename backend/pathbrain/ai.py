"""OpenRouter-backed profile suggestion.

Feed the optimizer export (settings → runs → raw scoring metrics + objective + shaper model)
to an LLM via OpenRouter and get back proposed shaper profiles likely to score faster than
anything measured.

The AI settings (API key, model, editable prompt) live in their **own** ``AppConfig`` row
(``"ai"``), isolated from the benchmark config — so the key never leaks into per-run config
snapshots, ``/api/config``, or the data dump. The key is masked whenever it leaves the backend
(only ``public_config`` is ever returned to the UI).
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.request

from sqlalchemy.orm import Session

from .logging_config import get_logger
from .models import AppConfig

log = get_logger("ai")

AI_CONFIG_KEY = "ai"
OPENROUTER_BASE = "https://openrouter.ai/api/v1"

DEFAULT_PROMPT = """You are a network QoS / SQM tuning expert optimizing FQ-CoDel shaper settings for human-perceived web responsiveness.

You will be given JSON with:
- `methodology`: the objective — which metrics are the "crown" (what we optimize), that lower is better (times in ms), and the best value achieved so far per crown metric.
- `shaper_model`: the tunable parameters. You may ONLY change fields listed in `writable_fields`; leave the others exactly as they are. Respect each field's kind/unit and stay within its suggested range. CRITICAL — return every value in the EXACT format the firewall expects, shown per field as `value_format` with a real `example`: `target`/`interval` are strings like `"5ms"` (NOT the number `5`), `quantum`/`limit`/`flows` are plain integers like `3000` (NOT strings), `ecn` is a boolean, bandwidth is a string like `"100Mbit"`. Copy the format of each field's `example` verbatim.
- `profiles`: every settings profile we have tested, with its full shaper `settings`, the raw per-run measurements (`run_samples`, the latest runs), and `metric_distribution` — the spread of each metric over ALL of that profile's runs (n/min/p25/median/p75/max). Prefer a profile that is reliably fast (low median AND tight p25–p75) over one that is only occasionally fast (low min but wide spread); a wide distribution means high variance, not a dependable win.

IMPORTANT — the shaper has SEPARATE pipes per direction. Each profile's `settings` is a list of pipes, one per direction, each identified by its `label` (typically a "Download" pipe and an "Upload" pipe). Every pipe has its OWN independently-tunable quantum / target / interval / ecn / limit / flows AND its own bandwidth (stored in the pipe's `download_bandwidth` field — that field is simply "this pipe's bandwidth" regardless of direction; `upload_bandwidth` is unused/null). **Upload shaping matters as much as download** — bufferbloat and latency under upload load hurt responsiveness — so tune BOTH pipes, not just the download one.

Study how the settings on BOTH pipes correlate with the measured crown metrics. Then propose 3-5 NEW shaper profiles (settings combinations we have NOT tested) likely to reduce the crown metrics below the best observed so far.

Respond with ONLY a JSON object of exactly this shape (no prose outside the JSON):
{"suggestions": [
  {
    "settings": [
      {"label": "<the exact Download pipe label from a profile>", "quantum": 3000, "target": "5ms", "interval": "60ms", "ecn": true},
      {"label": "<the exact Upload pipe label from a profile>", "quantum": 600, "target": "5ms", "interval": "60ms", "ecn": true}
    ],
    "displacement_likelihood": 72,
    "rationale": "why this should beat the current best — cover both directions"
  }
]}

Rules:
- Each suggestion's `settings` is a LIST with one object PER PIPE. Include BOTH the download and the upload pipe (referenced by their exact `label` from the profiles' `settings`) whenever a profile has both — a suggestion that only tunes one direction is incomplete.
- Set ONLY fields listed in `shaper_model.writable_fields`; leave every non-writable field alone. Use the same value formats you see (e.g. `target` like "5ms", `quantum` an integer).
- `displacement_likelihood` is your 0-100 estimate of the chance this profile beats the current crown.
- Order the suggestions by `displacement_likelihood`, highest first."""


def _row(session: Session) -> AppConfig | None:
    return session.get(AppConfig, AI_CONFIG_KEY)


def get_ai_config(session: Session) -> dict:
    """Full AI config **including the raw key** — internal use only (never returned to a client)."""
    row = _row(session)
    cfg = dict(row.value) if row and row.value else {}
    return {
        "api_key": cfg.get("api_key", "") or "",
        "model": cfg.get("model", "") or "",
        "prompt": cfg.get("prompt") or DEFAULT_PROMPT,
    }


def public_config(session: Session) -> dict:
    """AI config safe to return to the UI: the key is masked to a hint, never the raw value."""
    cfg = get_ai_config(session)
    key = cfg["api_key"]
    return {
        "configured": bool(key),
        "key_hint": (f"…{key[-4:]}" if len(key) >= 4 else ("set" if key else "")),
        "model": cfg["model"],
        "prompt": cfg["prompt"],
        "default_prompt": DEFAULT_PROMPT,
    }


def save_ai_config(session: Session, partial: dict) -> dict:
    """Persist a partial AI config (``api_key`` / ``model`` / ``prompt``). A blank/absent
    ``api_key`` leaves the stored key untouched (so the UI needn't round-trip the secret)."""
    row = _row(session)
    cur = dict(row.value) if row and row.value else {}
    if partial.get("api_key"):  # only overwrite when a non-empty key is supplied
        cur["api_key"] = partial["api_key"]
    if partial.get("model") is not None:
        cur["model"] = partial["model"]
    if partial.get("prompt") is not None:
        cur["prompt"] = partial["prompt"]
    if row is None:
        session.add(AppConfig(key=AI_CONFIG_KEY, value=cur))
    else:
        row.value = cur
    session.commit()
    return public_config(session)


def clear_api_key(session: Session) -> dict:
    row = _row(session)
    if row and row.value:
        cur = dict(row.value)
        cur.pop("api_key", None)
        row.value = cur
        session.commit()
    return public_config(session)


class AIError(RuntimeError):
    """A user-facing AI/OpenRouter failure (bad key, model error, network)."""


def _headers(api_key: str | None) -> dict:
    h = {
        "Content-Type": "application/json",
        # OpenRouter attribution headers (optional but recommended).
        "HTTP-Referer": "https://github.com/jmorganthall/pathbrain",
        "X-Title": "PathBrain",
    }
    if api_key:
        h["Authorization"] = f"Bearer {api_key}"
    return h


def _request(url: str, api_key: str | None, payload: dict | None, timeout: int) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    method = "POST" if payload is not None else "GET"
    req = urllib.request.Request(url, data=data, method=method)  # noqa: S310 — fixed https host
    for k, v in _headers(api_key).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode()[:500]
        except Exception:  # noqa: BLE001
            pass
        log.warning("OpenRouter HTTP %s for %s: %s", exc.code, url, body)
        raise AIError(f"OpenRouter returned HTTP {exc.code}: {body or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise AIError(f"Could not reach OpenRouter: {exc.reason}") from exc
    except Exception as exc:  # noqa: BLE001
        raise AIError(f"OpenRouter request failed: {exc}") from exc


def list_models(session: Session) -> list[dict]:
    """The OpenRouter model catalog (id/name/context/pricing), for the model picker."""
    cfg = get_ai_config(session)
    data = _request(f"{OPENROUTER_BASE}/models", cfg["api_key"] or None, None, timeout=20)
    out = []
    for m in data.get("data", []) or []:
        mid = m.get("id")
        if not mid:
            continue
        out.append({
            "id": mid,
            "name": m.get("name") or mid,
            "context_length": m.get("context_length"),
            "prompt_price": (m.get("pricing") or {}).get("prompt"),
        })
    out.sort(key=lambda x: x["id"])
    return out


def suggest(session: Session, export: dict, model: str | None = None, prompt: str | None = None) -> dict:
    """Send the optimizer export to the model and return ``{model, raw, suggestions, usage}``.

    ``suggestions`` is best-effort JSON parsed from the reply (``[{settings, rationale}, …]``);
    ``raw`` is always the model's full text so nothing is lost if parsing fails."""
    cfg = get_ai_config(session)
    api_key = cfg["api_key"]
    if not api_key:
        raise AIError("No OpenRouter API key configured — add one on the AI page first.")
    model = (model or cfg["model"] or "").strip()
    if not model:
        raise AIError("No model selected — pick one on the AI page first.")
    instructions = prompt if (prompt is not None and prompt.strip()) else cfg["prompt"]
    content = f"{instructions}\n\n=== MEASURED DATA (JSON) ===\n{json.dumps(export)}"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "temperature": 0.4,
    }
    resp = _request(f"{OPENROUTER_BASE}/chat/completions", api_key, payload, timeout=180)
    choice = (resp.get("choices") or [{}])[0]
    raw = ((choice.get("message") or {}).get("content")) or ""
    suggestions = _parse_suggestions(raw)
    # Rank by the model's own crown-displacement estimate (highest first).
    suggestions.sort(key=lambda s: -_as_float(s.get("displacement_likelihood")))
    return {
        "model": model,
        "raw": raw,
        "suggestions": suggestions,
        "usage": resp.get("usage") or {},
    }


def _as_float(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _parse_suggestions(text: str) -> list[dict]:
    """Best-effort extract ``suggestions`` (a list of ``{settings, rationale}``) from the model's
    reply — tolerant of ```json fences and surrounding prose. Returns [] if nothing parses."""
    if not text:
        return []
    candidates: list[str] = []
    candidates += re.findall(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
    m = re.search(r"\{.*\}", text, re.DOTALL)  # first/outermost object
    if m:
        candidates.append(m.group(0))
    candidates.append(text)
    for c in candidates:
        try:
            obj = json.loads(c)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(obj, dict) and isinstance(obj.get("suggestions"), list):
            return [s for s in obj["suggestions"] if isinstance(s, dict)]
        if isinstance(obj, list):
            return [s for s in obj if isinstance(s, dict)]
    return []


__all__ = [
    "AIError",
    "DEFAULT_PROMPT",
    "get_ai_config",
    "public_config",
    "save_ai_config",
    "clear_api_key",
    "list_models",
    "suggest",
]
