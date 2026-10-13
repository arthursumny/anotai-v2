"""Contextual generation never substitutes raw text for failed AI output."""

import json

import httpx
import pytest

from app.core.config import settings
from app.models.template import Template
from app.services.extractor import ExtractionError, generate_note
from app.services.general_note import GeneralNoteError, generate_general_note


def _result(body="## Contexto\nInformações preservadas.", **overrides):
    return {
        "title": "Anotação", "body": body, "summary": "Informações preservadas.",
        "data": {}, **overrides,
    }


async def test_contextual_note_has_separate_document_and_summary(openai_stub):
    openai_stub(_result())
    result = await generate_general_note("Ideias para organizar a semana.")
    assert result.body == "## Contexto\nInformações preservadas."
    assert result.summary == "Informações preservadas."


async def test_missing_key_raises_instead_of_local_note(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
    with pytest.raises(GeneralNoteError):
        await generate_general_note("Transcrição importante que deve ser preservada.")


@pytest.mark.parametrize("payload", [
    _result(body=""),
    {"title": "Sem documento"},
    httpx.Response(503, json={"error": {"message": "unavailable", "type": "server_error"}}),
])
async def test_incomplete_or_failed_generation_never_returns_success(openai_stub, payload):
    openai_stub(payload)
    with pytest.raises(GeneralNoteError):
        await generate_general_note("Fatos importantes da gravação.")


async def test_short_but_meaningful_audio_is_not_replaced_locally(openai_stub):
    openai_stub(_result(body="Comprar pão."))
    assert (await generate_general_note("Pão")).body == "Comprar pão."


async def test_template_example_and_full_text_reach_generation(openai_stub):
    template = Template(
        title="Ata", purpose="Registrar decisões", semantic_trigger="reunião",
        example_output="## Pauta\n## Decisões",
        output_schema={"type": "object", "required": ["decisao"],
                       "properties": {"decisao": {"type": "string"}}, "additionalProperties": False},
    )
    requests = openai_stub(_result(body="## Pauta\nProjeto\n## Decisões\nAprovado.", data={"decisao": "Aprovado"}))
    result = await generate_note("Discussão do projeto. Decisão aprovada.", template)
    sent = json.loads(requests[0]["messages"][1]["content"])
    assert sent["template"]["example_output"] == template.example_output
    assert sent["transcription"] == "Discussão do projeto. Decisão aprovada."
    assert result.data == {"decisao": "Aprovado"}
    assert result.body.endswith("Aprovado.")


async def test_template_schema_is_validated_before_saving(openai_stub):
    template = Template(
        title="Ata", semantic_trigger="reunião",
        output_schema={"type": "object", "required": ["number"],
                       "properties": {"number": {"type": "integer"}}},
    )
    openai_stub(_result(data={"number": "not an integer"}))
    with pytest.raises(ExtractionError):
        await generate_note("Fato da gravação", template)


async def test_every_long_audio_section_is_kept_in_document(monkeypatch, openai_stub):
    monkeypatch.setattr(settings, "OPENAI_TRANSCRIPT_CHUNK_CHARS", 1000)
    transcript = "A" * 1000 + "B" * 1000 + "FINAL_SENTINEL"
    requests = openai_stub(
        _result(body="Parte A", summary="A"), _result(body="Parte B", summary="B"),
        _result(body="Decisão final", summary="Final"),
    )
    result = await generate_general_note(transcript)
    assert "".join(json.loads(request["messages"][1]["content"])["transcription"] for request in requests) == transcript
    assert result.body == "Parte A\n\nParte B\n\nDecisão final"
    assert result.summary == "A\n\nB\n\nFinal"
