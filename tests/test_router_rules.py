"""OpenAI decisions, complete transcripts, and explicit routing failures."""

import json
import uuid

import httpx
import pytest

from app.models.template import Template
from app.services.router import RouterError, route_transcription
from app.core.config import settings


def _template() -> Template:
    return Template(
        user_id=uuid.uuid4(), title="Reunião", purpose="Registrar decisões",
        semantic_trigger="reunião", example_output="## Decisões\n- Exemplo",
        output_schema={"type": "object"}, actions=[],
    )


async def test_openai_selects_owned_catalog_entry(openai_stub):
    template = _template()
    openai_stub({"template_id": str(template.id), "confidence": 0.95, "reason": "matched"})
    result = await route_transcription("reunião da equipe", [template])
    assert result.template_id == template.id


async def test_no_templates_returns_no_match_without_api_key(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
    assert (await route_transcription("gravação sem modelos", [])).template_id is None


@pytest.mark.parametrize("payload", [
    {"template_id": None, "confidence": 0.99, "reason": "no match"},
    {"template_id": "SELECTED", "confidence": 0.1, "reason": "uncertain"},
])
async def test_only_explicit_or_low_confidence_decisions_become_no_match(openai_stub, payload):
    template = _template()
    if payload["template_id"] == "SELECTED":
        payload = {**payload, "template_id": str(template.id)}
    openai_stub(payload)
    assert (await route_transcription("conteúdo", [template])).template_id is None


async def test_missing_key_does_not_use_word_overlap(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
    with pytest.raises(RouterError):
        await route_transcription("reunião", [_template()])


@pytest.mark.parametrize("payload", [
    {"template_id": str(uuid.uuid4()), "confidence": 1, "reason": "outside"},
    {"template_id": None, "confidence": "invalid", "reason": "invalid"},
    httpx.Response(503, json={"error": {"message": "unavailable", "type": "server_error"}}),
])
async def test_api_failure_or_invalid_decision_is_not_no_match(openai_stub, payload):
    openai_stub(payload)
    with pytest.raises(RouterError):
        await route_transcription("reunião", [_template()])


async def test_long_transcript_is_analyzed_in_full_without_action_secrets(monkeypatch, openai_stub):
    monkeypatch.setattr(settings, "OPENAI_TRANSCRIPT_CHUNK_CHARS", 1000)
    template = _template()
    template.actions = [{"type": "webhook", "config": {"token": "PRIVATE_TOKEN"}}]
    decision = {"template_id": str(template.id), "confidence": 0.9, "reason": "matched"}
    requests = openai_stub(decision, decision, decision, decision)
    transcript = "A" * 1000 + "B" * 1000 + "TAIL_123"
    result = await route_transcription(transcript, [template])
    sections = [json.loads(request["messages"][1]["content"]) for request in requests[:-1]]
    assert "".join(section["transcription"] for section in sections) == transcript
    assert sections[0]["catalog"][0]["example"] == template.example_output
    assert "PRIVATE_TOKEN" not in json.dumps(requests)
    assert result.template_id == template.id
