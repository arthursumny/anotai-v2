"""AnotAI — extrator semântico p/ gravação contínua (Always-On).

Chunks de 3–5 min capturam conversa ambiente mista: a LLM identifica apenas
itens acionáveis (tarefas, compromissos, ideias). Ruído/conversa fiada →
lista vazia, sem poluir o banco.
"""

import json
from typing import Any

from openai import AsyncOpenAI

from app.core.config import settings
from app.services.llm import chat_create


class ContinuousExtractionError(Exception):
    """Falha irrecuperável na análise do chunk (uso interno)."""


_ITEM_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "description", "suggested_action"],
    "properties": {
        "title": {"type": "string", "description": "Título curto do item acionável"},
        "description": {"type": "string", "description": "Detalhe do que foi captado"},
        "suggested_due_date": {
            "type": ["string", "null"],
            "description": "Prazo em ISO8601 (com data/hora) ou null se indeterminado",
        },
        "suggested_reminder_at": {
            "type": ["string", "null"],
            "description": "Quando alertar, em ISO8601, ou null",
        },
        "suggested_action": {
            "type": "string",
            "description": "Ex: 'Criar Tarefa no Kanban', 'Agendar Reunião', 'Lembrete Rápido'",
        },
    },
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items"],
    "properties": {"items": {"type": "array", "items": _ITEM_SCHEMA, "maxItems": 10}},
}


async def analyze_continuous_chunk(
    transcription: str, user_context: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Analisa o chunk e retorna itens acionáveis (ou `[]` se só houver ruído).

    Sem `OPENAI_API_KEY` ou com texto muito curto: retorna `[]` graciosamente
    para nunca gerar lixo no banco.
    """
    text = (transcription or "").strip()
    if len(text) < 30:
        return []
    if not settings.OPENAI_API_KEY:
        return []

    context = user_context or {}
    who = f"Usuário: {context.get('user_name', 'desconhecido')}. " if context.get("user_name") else ""
    system = (
        "Você é o extrator Always-On do AnotAI. Analise a transcrição de áudio "
        "ambiente e identifique SOMENTE itens acionáveis: tarefas/pendências, "
        "compromissos com data futura e ideias relevantes que merecem registro. "
        f"{who}Responda apenas o JSON do schema: lista `items` (vazia se for "
        "ruído, conversa fiada sem ações, silêncio ou conteúdo irrelevante). "
        "Datas relativas ('amanhã cedo', 'dia 31 às 12h') devem virar ISO8601 "
        "absoluto; se indeterminado, use null. Máximo 10 itens."
    )
    try:
        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        resp = await chat_create(
            client,
            model=settings.OPENAI_EXTRACTOR_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": f"Transcrição do chunk:\n{text[:8000]}"},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "continuous_items", "schema": _RESPONSE_SCHEMA, "strict": False},
            },
            temperature=0,
        )
        data = json.loads(resp.choices[0].message.content or '{"items": []}')
    except Exception as exc:
        raise ContinuousExtractionError(f"Falha na LLM contínua: {exc}") from exc

    items = data.get("items", []) if isinstance(data, dict) else []
    if not isinstance(items, list):
        return []
    clean: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title", "")).strip()
        if not title:
            continue
        clean.append({
            "title": title[:300],
            "description": str(item.get("description", ""))[:2000],
            "suggested_due_date": item.get("suggested_due_date"),
            "suggested_reminder_at": item.get("suggested_reminder_at"),
            "suggested_action": str(item.get("suggested_action", "Criar Tarefa no Kanban"))[:200],
        })
    return clean
