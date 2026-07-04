"""Tests for the AI (OpenRouter) config + suggestion flow. The OpenRouter HTTP call is
monkeypatched, so no network is used."""
from __future__ import annotations

from pathbrain import ai


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
