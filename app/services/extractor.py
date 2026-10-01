"""AnotAI — extração estruturada via LLM + JSON Schema do template."""

import json
from typing import Any

import jsonschema
from jsonschema import Draft202012Validator
from openai import AsyncOpenAI

from app.core.config import settings
from app.models.template import Template
from app.services.llm import chat_create


class ExtractionError(Exception):
    """Falha ao extrair payload estruturado."""


async def extract_structured(template: Template, transcription: str) -> dict[str, Any]:
    """Chama a LLM com o `output_schema` do template e retorna o payload validado."""
    text = (transcription or "").strip()
    if not text:
        raise ExtractionError("Transcrição vazia")
    if not settings.OPENAI_API_KEY:
        raise ExtractionError("OPENAI_API_KEY não configurada para extração")

    schema = template.output_schema
    try:
        Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:
        raise ExtractionError(f"output_schema inválido no template: {exc.message}") from exc

    system = (
        "Você é o extrator do AnotAI. Extraia da transcrição SOMENTE os dados exigidos "
        "pelo JSON Schema informado. Responda apenas JSON válido. "
        "Não invente valores; use null quando a informação não estiver presente e o schema permitir."
    )
    try:
        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        resp = await chat_create(
            client,
            model=settings.OPENAI_EXTRACTOR_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": f"Transcrição:\n{text[:8000]}"},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "template_payload", "schema": schema, "strict": False},
            },
            temperature=0,
        )
        raw = resp.choices[0].message.content or "{}"
        payload: dict[str, Any] = json.loads(raw)
    except ExtractionError:
        raise
    except Exception as exc:
        raise ExtractionError(f"Falha na LLM de extração: {exc}") from exc

    try:
        Draft202012Validator(schema).validate(payload)
    except jsonschema.ValidationError as exc:
        raise ExtractionError(f"Payload fora do schema: {exc.message}") from exc
    return payload
