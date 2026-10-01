"""AnotAI — rascunho de template via LLM ("Construir com IA").

O usuário descreve em linguagem natural o que quer automatizar e a LLM
devolve um rascunho (título, gatilho, campos, mensagem) no vocabulário do
construtor visual — nada é salvo sem revisão.
"""

import json
from typing import Any, Literal

from openai import AsyncOpenAI

from app.core.config import settings
from app.services.llm import chat_create

DraftKind = Literal["texto", "número", "data", "lista", "sim/não", "opções"]


class DraftError(Exception):
    """Falha ao gerar o rascunho (config ausente ou LLM)."""


_DRAFT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["title", "purpose", "semantic_trigger", "fields", "suggested_message",
                 "suggested_example"],
    "properties": {
        "title": {"type": "string", "description": "Nome curto do template (máx 60 chars)"},
        "purpose": {"type": "string", "description": "Para que serve: o que entra e o que sai (1-2 frases)"},
        "semantic_trigger": {"type": "string", "description": "Quando usar + quando NÃO usar"},
        "fields": {
            "type": "array",
            "minItems": 1,
            "maxItems": 10,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "kind", "required"],
                "properties": {
                    "name": {"type": "string", "description": "snake_case, ASCII, sem espaços"},
                    "kind": {"type": "string",
                             "enum": ["texto", "número", "data", "lista", "sim/não", "opções"]},
                    "required": {"type": "boolean"},
                    "options": {"type": "array", "items": {"type": "string"},
                                "description": "Só quando kind é 'opções'"},
                },
            },
        },
        "suggested_message": {
            "type": "string",
            "description": "Modelo de mensagem Telegram usando {campos} declarados acima",
        },
        "suggested_example": {
            "type": "string",
            "description": "UM exemplo de como a nota deve sair (mesmo estilo do exemplo do usuário)",
        },
    },
}

_DERIVE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["purpose", "semantic_trigger", "fields", "suggested_message"],
    "properties": {
        "purpose": {"type": "string"},
        "semantic_trigger": {"type": "string"},
        "fields": _DRAFT_SCHEMA["properties"]["fields"],
        "suggested_message": {"type": "string"},
    },
}


async def draft_template(brief: str) -> dict[str, Any]:
    """Gera o rascunho a partir da descrição livre. Retorna dict validado."""
    from app.schemas.template import TemplateDraft  # import tardio: evita ciclo

    text = (brief or "").strip()
    if len(text) < 10:
        raise DraftError("Descreva com mais detalhes o que você quer automatizar.")
    if not settings.OPENAI_API_KEY:
        raise DraftError("IA indisponível: OPENAI_API_KEY não configurada.")

    system = (
        "Você é o construtor de templates do AnotAI. O usuário descreve em "
        "linguagem natural o que quer automatizar a partir de áudios, e você "
        "devolve um rascunho de template em JSON seguindo o schema. Regras: "
        "1) title curto e direto (máx 60 caracteres). "
        "2) purpose diz PARA QUE SERVE em 1-2 frases (o que entra, o que sai). "
        "3) semantic_trigger diz QUANDO usar o template e exemplos de quando NÃO usar. "
        "4) 3 a 8 campos com name em snake_case ASCII sem espaços nem acentos. "
        "5) kind é exatamente um de: texto, número, data, lista, sim/não, opções. "
        "6) Use 'opções' com a lista preenchida quando os valores forem fechados "
        "(ex: espécies, status); use 'lista' para vários itens livres; 'data' para datas. "
        "7) suggested_message é um modelo de Telegram citando SOMENTE campos "
        "declarados, no formato {nome_do_campo}. "
        "8) suggested_example é UM exemplo curto de nota pronta naquele estilo, "
        "com valores fictícios coerentes."
    )
    try:
        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        resp = await chat_create(
            client,
            model=settings.OPENAI_EXTRACTOR_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": f"Quero automatizar: {text[:2000]}"},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "template_draft", "schema": _DRAFT_SCHEMA, "strict": False},
            },
            temperature=0,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        draft = TemplateDraft.model_validate(data)
    except DraftError:
        raise
    except Exception as exc:
        raise DraftError(f"A IA falhou ao montar o rascunho: {exc}") from exc

    return draft.model_dump(mode="json")


async def derive_from_example(example: str) -> dict[str, Any]:
    """Deriva gatilho+campos+mensagem a partir do exemplo (fonte da verdade).

    O usuário mostra COMO a nota deve sair; a IA extrai a estrutura.
    """
    from app.schemas.template import DeriveResponse  # import tardio: evita ciclo

    text = (example or "").strip()
    if len(text) < 20:
        raise DraftError("Cole um exemplo mais completo de como a nota deve sair.")
    if not settings.OPENAI_API_KEY:
        raise DraftError("IA indisponível: OPENAI_API_KEY não configurada.")

    system = (
        "Você analisa UM exemplo de nota pronta e deriva a estrutura do template. "
        "Regras: "
        "1) purpose diz PARA QUE SERVE em 1-2 frases, inferindo pelo exemplo. "
        "2) semantic_trigger descreve em que áudios esse formato se aplica e "
        "quando NÃO usar, inferindo pelo conteúdo do exemplo. "
        "3) fields: cada informação variável do exemplo vira um campo com name "
        "em snake_case ASCII; kind é exatamente um de: texto, número, data, "
        "lista, sim/não, opções (com a lista preenchida quando os valores do "
        "exemplo forem fechados). "
        "4) suggested_message resume o exemplo em modelo Telegram citando "
        "SOMENTE campos declarados, no formato {nome_do_campo}."
    )
    try:
        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        resp = await chat_create(
            client,
            model=settings.OPENAI_EXTRACTOR_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": f"Exemplo de nota:\n{text[:8000]}"},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "template_derive", "schema": _DERIVE_SCHEMA, "strict": False},
            },
            temperature=0,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        derived = DeriveResponse.model_validate(data)
    except DraftError:
        raise
    except Exception as exc:
        raise DraftError(f"A IA falhou ao interpretar o exemplo: {exc}") from exc

    return derived.model_dump(mode="json")
