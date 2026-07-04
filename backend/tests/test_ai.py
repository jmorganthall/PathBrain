"""Tests for the AI (OpenRouter) config + suggestion flow. The OpenRouter HTTP call is
monkeypatched, so no network is used."""
from __future__ import annotations

import json

from pathbrain import ai


def _sse_events(text: str) -> list[dict]:
    """Parse the ``data: {json}`` events out of an SSE response body."""
    return [json.loads(ln[5:].strip()) for ln in text.splitlines() if ln.startswith("data:")]


def test_ai_config_masks_the_key(client):
    # Save a key + model + prompt, then read it back — the raw key must never come back.
    resp = client.put("/api/ai/config", json={
        "api_key": "sk-or-v1-secret1234", "model": "anthropic/claude-sonnet-4", "prompt": "hi",
    })
    assert resp.status_code == 200
    body = client.get("/api/ai/config").json()
    assert body["configured"] is True
    assert body["key_hint"] == "…1234"          # only a 4-char hint
    assert body["model"] == "anthropic/claude-sonnet-4"
    assert body["prompt"] == "hi"
    assert "api_key" not in body                 # the secret never leaves the backend
    assert body["default_prompt"]                # the editable default is offered


def test_ai_config_blank_key_preserves_existing(client):
    client.put("/api/ai/config", json={"api_key": "sk-or-keepme7890"})
    # Saving other fields with no key must NOT wipe the stored key.
    client.put("/api/ai/config", json={"model": "openai/gpt-5", "api_key": ""})
    body = client.get("/api/ai/config").json()
    assert body["configured"] is True and body["key_hint"] == "…7890"
    assert body["model"] == "openai/gpt-5"


def test_ai_clear_key(client):
    client.put("/api/ai/config", json={"api_key": "sk-or-tossme0000"})
    body = client.delete("/api/ai/config/key").json()
    assert body["configured"] is False and body["key_hint"] == ""


def test_ai_suggest_requires_a_key(client):
    client.delete("/api/ai/config/key")
    resp = client.post("/api/ai/suggest", json={"model": "x/y"})
    assert resp.status_code == 400
    assert "key" in resp.json()["detail"].lower()


def test_ai_suggest_parses_suggestions(client, monkeypatch):
    client.put("/api/ai/config", json={"api_key": "sk-or-good1111", "model": "test/model"})

    def _fake_request(url, api_key, payload, timeout):
        # It's the chat-completions call; return a reply wrapping a JSON suggestion.
        assert "chat/completions" in url and payload["model"] == "test/model"
        content = (
            'Here you go:\n```json\n'
            '{"suggestions": [{"settings": {"quantum": 3000, "target": "5ms"}, '
            '"rationale": "bigger quantum, tighter target"}]}\n```'
        )
        return {"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 42}}

    monkeypatch.setattr(ai, "_request", _fake_request)

    resp = client.post("/api/ai/suggest", json={"runs_per_profile": 10})
    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] == "test/model"
    assert len(body["suggestions"]) == 1
    assert body["suggestions"][0]["settings"]["quantum"] == 3000
    assert body["usage"]["total_tokens"] == 42
    assert "profiles_sent" in body


def test_ai_suggest_stream_emits_reasoning_content_and_done(client, monkeypatch):
    client.put("/api/ai/config", json={"api_key": "sk-or-good1111", "model": "test/model"})

    def _fake_stream(api_key, payload, timeout):
        assert payload["stream"] is True and payload["model"] == "test/model"
        yield {"choices": [{"delta": {"reasoning": "let me think… "}}]}
        yield {"choices": [{"delta": {"reasoning": "bigger quantum."}}]}
        yield {"choices": [{"delta": {"content": '{"suggestions": [{"settings": '}}]}
        yield {"choices": [{"delta": {"content": '{"quantum": 3000}, "rationale": "x"}]}'}}]}
        yield {"usage": {"total_tokens": 21}}

    monkeypatch.setattr(ai, "_stream_chat", _fake_stream)

    resp = client.post("/api/ai/suggest/stream", json={"runs_per_profile": 10})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(resp.text)

    assert events[0]["type"] == "meta" and "profiles_sent" in events[0]
    types = [e["type"] for e in events]
    assert "reasoning" in types and "content" in types
    done = next(e for e in events if e["type"] == "done")
    assert done["reasoning"] == "let me think… bigger quantum."
    assert done["suggestions"][0]["settings"]["quantum"] == 3000
    assert done["usage"]["total_tokens"] == 21


def test_ai_suggest_stream_reports_error_as_event(client, monkeypatch):
    client.put("/api/ai/config", json={"api_key": "sk-or-bad2222", "model": "test/model"})

    def _boom(api_key, payload, timeout):
        raise ai.AIError("OpenRouter returned HTTP 401: invalid key")
        yield  # noqa — makes this a generator so the raise fires on iteration

    monkeypatch.setattr(ai, "_stream_chat", _boom)

    resp = client.post("/api/ai/suggest/stream", json={})
    assert resp.status_code == 200  # the stream opened; the failure rides inside it
    err = [e for e in _sse_events(resp.text) if e["type"] == "error"]
    assert err and "401" in err[0]["error"]


def test_ai_suggest_stream_needs_a_key(client):
    ai_row_clear = client.delete("/api/ai/config/key")
    assert ai_row_clear.status_code == 200
    resp = client.post("/api/ai/suggest/stream", json={})
    assert resp.status_code == 200
    err = [e for e in _sse_events(resp.text) if e["type"] == "error"]
    assert err and "API key" in err[0]["error"]


def test_ai_suggest_surfaces_openrouter_error(client, monkeypatch):
    client.put("/api/ai/config", json={"api_key": "sk-or-bad2222", "model": "test/model"})

    def _boom(url, api_key, payload, timeout):
        raise ai.AIError("OpenRouter returned HTTP 401: invalid key")

    monkeypatch.setattr(ai, "_request", _boom)
    resp = client.post("/api/ai/suggest", json={})
    assert resp.status_code == 502
    assert "401" in resp.json()["detail"]


def test_ai_models_lists_catalog(client, monkeypatch):
    def _fake_request(url, api_key, payload, timeout):
        assert url.endswith("/models") and payload is None
        return {"data": [
            {"id": "openai/gpt-5", "name": "GPT-5", "context_length": 400000,
             "pricing": {"prompt": "0.000005"}},
            {"id": "anthropic/claude-sonnet-4", "name": "Claude Sonnet 4"},
        ]}

    monkeypatch.setattr(ai, "_request", _fake_request)
    body = client.get("/api/ai/models").json()
    ids = [m["id"] for m in body["models"]]
    assert "openai/gpt-5" in ids and "anthropic/claude-sonnet-4" in ids
    gpt = next(m for m in body["models"] if m["id"] == "openai/gpt-5")
    assert gpt["context_length"] == 400000 and gpt["prompt_price"] == "0.000005"


def test_parse_suggestions_tolerates_prose_and_fences():
    fenced = '```json\n{"suggestions":[{"settings":{"quantum":1}}]}\n```'
    assert ai._parse_suggestions(fenced)[0]["settings"]["quantum"] == 1
    prose = 'Sure! {"suggestions":[{"settings":{"target":"5ms"}}]} hope that helps'
    assert ai._parse_suggestions(prose)[0]["settings"]["target"] == "5ms"
    assert ai._parse_suggestions("no json here") == []


def test_parse_relationships_extracts_the_interpretation_block():
    reply = (
        'Here is my read:\n```json\n'
        '{"relationships":[{"pipe":"Download","field":"quantum","metric":"fcp",'
        '"direction":"inverse","confidence":"high"}],'
        '"suggestions":[{"settings":[]}]}\n```'
    )
    rels = ai._parse_relationships(reply)
    assert len(rels) == 1 and rels[0]["field"] == "quantum" and rels[0]["direction"] == "inverse"
    # Suggestions still parse from the same reply, independently.
    assert ai._parse_suggestions(reply)[0]["settings"] == []
    # No relationships block → empty, but the suggestions still stand.
    assert ai._parse_relationships('{"suggestions":[{"settings":{}}]}') == []


def test_ai_suggest_returns_relationships_and_field_sensitivity(client, monkeypatch):
    client.put("/api/ai/config", json={"api_key": "sk-or-rel11111", "model": "test/model"})

    def _fake(url, api_key, payload, timeout):
        content = (
            '{"relationships":[{"pipe":"Download","field":"quantum","metric":"fcp",'
            '"direction":"inverse"}],"suggestions":[{"settings":[],"displacement_likelihood":50}]}'
        )
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(ai, "_request", _fake)
    body = client.post("/api/ai/suggest", json={}).json()
    # The model's interpretation is parsed through.
    assert body["relationships"][0]["field"] == "quantum"
    # The deterministic sensitivity map we computed is echoed back for the UI (list, possibly
    # empty depending on how much the fixture profiles vary).
    assert isinstance(body["field_sensitivity"], list)


def test_ai_suggest_returns_data_requests_and_coverage_gaps(client, monkeypatch):
    client.put("/api/ai/config", json={"api_key": "sk-or-dr111111", "model": "test/model"})

    def _fake(url, api_key, payload, timeout):
        content = (
            '{"data_requests":[{"pipe":"Download","field":"interval",'
            '"suggested_values":[20,30],"reason":"lower looks better but only 40/60 measured"}],'
            '"suggestions":[]}'
        )
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(ai, "_request", _fake)
    body = client.post("/api/ai/suggest", json={}).json()
    # The model's data request (kick-back for more measurement) is parsed through.
    assert body["data_requests"][0]["field"] == "interval"
    # The deterministic coverage-gap map we computed is echoed back for the UI.
    assert isinstance(body["coverage_gaps"], list)


def test_ai_suggest_ranks_by_displacement_likelihood(client, monkeypatch):
    client.put("/api/ai/config", json={"api_key": "sk-or-rank9999", "model": "test/model"})

    def _fake(url, api_key, payload, timeout):
        content = (
            '{"suggestions": ['
            '{"settings": [], "displacement_likelihood": 30},'
            '{"settings": [], "displacement_likelihood": 90},'
            '{"settings": [], "displacement_likelihood": 60}]}'
        )
        return {"choices": [{"message": {"content": content}}]}

    monkeypatch.setattr(ai, "_request", _fake)
    body = client.post("/api/ai/suggest", json={}).json()
    assert [s["displacement_likelihood"] for s in body["suggestions"]] == [90, 60, 30]
