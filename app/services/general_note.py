"""AnotAI — nota geral adaptativa (substituta do antigo `no_match`).

Quando nenhum template casa, em vez de guardar só a transcrição bruta, a LLM
gera uma nota útil: título curto + corpo em markdown adaptado ao conteúdo
(resumo, tópicos, listas, datas), mais tarefas extraídas, prazo/lembrete
quando mencionados e sugestão de Kanban.
Sem chave de API ou em falha da LLM: retorna um fallback local (título a
partir do texto + corpo com a transcrição) para nunca deixar a nota vazia.
Só levanta `GeneralNoteError` se a transcrição for curta demais.
"""

import json
from typing import Any

from openai import AsyncOpenAI

from app.core.config import settings
from app.services.llm import chat_create


class GeneralNoteError(Exception):
    """Falha ao gerar a nota geral (texto curto/inválido)."""


_TASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title"],
    "properties": {
        "title": {"type": "string", "description": "Título curto da tarefa"},
        "due_date": {
            "type": ["string", "null"],
            "description": "Prazo em ISO8601 ou null",
        },
        "reminder_at": {
            "type": ["string", "null"],
            "description": "Quando alertar em ISO8601 ou null",
        },
    },
}

_RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "body"],
    "properties": {
        "title": {"type": "string", "description": "Título curto (máx 80 chars)"},
        "body": {"type": "string", "description": "Nota em markdown: resumo/tópicos/listas/datas, conforme o conteúdo"},
        "tasks": {"type": "array", "items": _TASK_SCHEMA, "maxItems": 10},
        "due_date": {"type": ["string", "null"], "description": "Prazo geral em ISO8601 ou null"},
        "reminder_at": {"type": ["string", "null"], "description": "Lembrete geral em ISO8601 ou null"},
        "send_to_kanban": {"type": "boolean", "description": "True se a nota é acionável e merece ir ao Kanban"},
    },
}


def _local_fallback(transcription: str) -> dict[str, Any]:
    """Nota genérica sem LLM: nunca deixa a nota vazia/sem título."""
    text = (transcription or "").strip()
    # Título: primeira frase ou primeiros ~60 chars.
    first = text.split("\n")[0].strip()
    for sep in (". ", "! ", "? "):
        if sep in first:
            first = first.split(sep)[0].strip()
            break
    title = (first[:80] or "Nota de voz").strip()
    if len(title) < 3:
        title = (text[:80] or "Nota de voz").strip()
    # Corpo: transcrição + lista simples quando houver várias sentenças.
    sentences = [s.strip(" .") for s in text.replace("\n", " ").split(". ") if s.strip()]
    if len(sentences) >= 2:
        bullets = "\n".join(f"- {s.strip()}" for s in sentences if s.strip())
        body = f"## Nota\n\n{bullets}"
    else:
        body = f"## Nota\n\n{text}"
    return {
        "title": title[:300],
        "body": body,
        "tasks": [],
        "due_date": None,
        "reminder_at": None,
        "send_to_kanban": False,
    }


def _clean_task(item: Any) -> dict[str, Any] | None:
    if isinstance(item, str):
        title = item.strip()
        if not title:
            return None
        return {"title": title[:300], "due_date": None, "reminder_at": None}
    if not isinstance(item, dict):
        return None
    title = str(item.get("title", "")).strip()
    if not title:
        return None
    due = item.get("due_date")
    rem = item.get("reminder_at")
    return {
        "title": title[:300],
        "due_date": due if isinstance(due, str) and due.strip() else None,
        "reminder_at": rem if isinstance(rem, str) and rem.strip() else None,
    }


async def generate_general_note(
    transcription: str, user_context: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Gera nota genérica rica: `{title, body, tasks, due_date, reminder_at, send_to_kanban}`.

    Sempre retorna algo útil (fallback local sem LLM). Só levanta
    `GeneralNoteError` se o texto for curto demais para interpretar.
    """
    text = (transcription or "").strip()
    if len(text) < 10:
        raise GeneralNoteError("Transcrição curta demais para gerar nota.")
    if not settings.OPENAI_API_KEY:
        return _local_fallback(text)

    context = user_context or {}
    who = f"Usuário: {context.get('user_name', 'desconhecido')}. " if context.get("user_name") else ""
    system = (
        "Você transforma a transcrição de um áudio em uma anotação útil e legível. "
        f"{who}Analise o conteúdo e produza o formato que fizer mais sentido ali: "
        "resumo corrido para relatos, tópicos para vários assuntos, lista de itens "
        "para pendências, com datas destacadas quando mencionadas. "
        "Responda apenas o JSON com: `title` (curto, máx 80 caracteres), `body` "
        "(markdown com resumo/listas), `tasks` (lista de {title, due_date, reminder_at} "
        "com as pendências explícitas ou implícitas, máx 10, [] se não houver), "
        "`due_date`/`reminder_at` (ISO8601 do compromisso principal ou null), "
        "`send_to_kanban` (true se for tarefa/compromisso/follow-up acionável). "
        "Tarefas com prazo também devem incluir `due_date`; para compromissos com "
        "data e hora, proponha `reminder_at` 30 minutos antes. Quando a pessoa disser "
        "'me lembra', respeite o horário pedido. Datas relativas devem virar ISO8601 "
        "com fuso local -03:00 quando possível; para um prazo sem horário, use 09:00. "
        "Não invente fatos nem horários quando a data não estiver clara."
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
                "json_schema": {"name": "general_note", "schema": _RESPONSE_SCHEMA, "strict": False},
            },
            temperature=0,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
    except Exception:
        # LLM falhou: fallback local em vez de deixar a nota vazia.
        return _local_fallback(text)

    title = str(data.get("title", "")).strip()[:300]
    body = str(data.get("body", "")).strip()
    if not title or not body:
        return _local_fallback(text)
    tasks_raw = data.get("tasks", [])
    tasks: list[dict[str, Any]] = []
    if isinstance(tasks_raw, list):
        for item in tasks_raw[:10]:
            clean = _clean_task(item)
            if clean:
                tasks.append(clean)
    due = data.get("due_date")
    rem = data.get("reminder_at")
    return {
        "title": title,
        "body": body,
        "tasks": tasks,
        "due_date": due if isinstance(due, str) and due.strip() else None,
        "reminder_at": rem if isinstance(rem, str) and rem.strip() else None,
        "send_to_kanban": bool(data.get("send_to_kanban", bool(tasks))),
    }
