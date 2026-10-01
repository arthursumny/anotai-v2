"""Focused validation and fail-closed behavior for intelligence classification."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.services.intelligence as intelligence
from app.schemas.intelligence import IntelligenceType


def _response(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))])


def _install_llm(monkeypatch, payload: dict):
    call = AsyncMock(return_value=_response(payload))
    monkeypatch.setattr(intelligence.settings, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(intelligence, "AsyncOpenAI", lambda **kwargs: object())
    monkeypatch.setattr(intelligence, "chat_create", call)
    return call


async def test_classifies_multiple_results_and_passes_local_time_locale(monkeypatch):
    call = _install_llm(monkeypatch, {
        "results": [
            {
                "type": "TASK",
                "title": "Ligar para Ana",
                "summary": "Retornar a ligação combinada.",
                "confidence": 0.91,
                "due_at": "2026-05-06T14:30:00-03:00",
                "structured_data": {"person": "Ana"},
            },
            {
                "type": "INFORMATION",
                "title": "Preferência de horário",
                "summary": "Ana prefere conversas à tarde.",
                "confidence": 0.8,
                "due_at": None,
                "structured_data": {},
            },
        ]
    })

    results = await intelligence.classify_event(
        "Ligar para Ana amanhã. Ela prefere a tarde.",
        occurred_at=datetime(2026, 5, 5, 15, 0, tzinfo=timezone.utc),
        timezone="America/Sao_Paulo",
        locale="pt-BR",
        context={"source": "calendar"},
    )

    assert len(results) == 2
    assert results[0].type is IntelligenceType.TASK
    assert results[0].due_at is not None
    offset = results[0].due_at.utcoffset()
    assert offset is not None and offset.total_seconds() == -10800
    request = call.await_args
    assert request is not None
    request_messages = request.kwargs["messages"]
    user_message = json.loads(request_messages[1]["content"])
    assert user_message["occurred_at"] == "2026-05-05T12:00:00-03:00"
    assert user_message["timezone"] == "America/Sao_Paulo"
    assert user_message["locale"] == "pt-BR"


@pytest.mark.parametrize("bad_result", [
    {"type": "UNKNOWN", "title": "x", "summary": "y", "confidence": 0.9},
    {"type": "TASK", "title": "x", "summary": "y", "confidence": 0.9, "provider_payload": {"x": 1}},
    {"type": "TASK", "title": "x", "summary": "y", "confidence": 0.9, "due_at": "2026-05-06T14:30:00"},
    {"type": "TASK", "title": "x", "summary": "y", "confidence": 0.9, "structured_data": {"workspaceId": "w1"}},
    {"type": "TASK", "title": "x", "summary": "y", "confidence": 0.9, "structured_data": {"link": "https://evil.example"}},
])
async def test_rejects_invalid_schema_or_injected_payload(monkeypatch, bad_result):
    _install_llm(monkeypatch, {"results": [bad_result]})

    results = await intelligence.classify_event(
        "text",
        occurred_at=datetime(2026, 5, 5, tzinfo=timezone.utc),
        timezone="UTC",
        locale="en-US",
    )

    assert results == []


async def test_empty_without_api_key_and_low_confidence_filtered(monkeypatch):
    call = AsyncMock()
    monkeypatch.setattr(intelligence.settings, "OPENAI_API_KEY", "")
    monkeypatch.setattr(intelligence, "chat_create", call)
    args = {
        "occurred_at": datetime(2026, 5, 5, tzinfo=timezone.utc),
        "timezone": "UTC",
        "locale": "en-US",
    }
    assert await intelligence.classify_event("text", **args) == []
    call.assert_not_awaited()

    _install_llm(monkeypatch, {"results": [
        {"type": "NOTE", "title": "Note", "summary": "Summary", "confidence": 0.4}
    ]})
    assert await intelligence.classify_event("text", **args, threshold=0.5) == []


@pytest.mark.parametrize("kwargs", [
    {"occurred_at": datetime(2026, 5, 5), "timezone": "UTC"},
    {"occurred_at": datetime(2026, 5, 5, tzinfo=timezone.utc), "timezone": "Not/AZone"},
])
async def test_invalid_time_inputs_are_rejected(kwargs):
    with pytest.raises(ValueError):
        await intelligence.classify_event("text", **kwargs, locale="en-US")
