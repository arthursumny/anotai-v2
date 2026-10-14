"""Orquestrador dinâmico de tool-calling para ações de integração de templates.

Depois que a nota é gerada, este módulo descobre quais integrações o template
habilitou E para as quais o usuário tem uma conexão ativa, oferece APENAS essas
ferramentas ao modelo e executa, em ordem, as chamadas devolvidas. Toda falha de
uma chamada é isolada e nunca interrompe as demais.
"""

import json
import logging
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from openai import AsyncOpenAI
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.models.integration_connection import IntegrationConnection
from app.models.template import Template
from app.models.user import User
from app.services.llm import chat_create
from app.services.nango_client import NangoClient, NangoClientError
from app.services.tool_registry import all_tools, llm_tools

logger = logging.getLogger(__name__)

_DEFAULT_NANGO_BASE_URL = "https://api.nango.dev"
_SAFE_IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,128}")

_SYSTEM_PROMPT = (
    "Você recebe a transcrição de um áudio e a nota gerada. Chame as tools de "
    "integração SOMENTE quando a conversa indicar uma ação concreta para aquela "
    "ferramenta. Nunca invente argumentos; datas/horas em ISO 8601 com fuso "
    "horário. Se nada se aplicar, não chame nenhuma tool."
)


@dataclass(frozen=True)
class ToolCallResult:
    """Resultado sanitizado de uma chamada de ferramenta."""

    tool: str
    status: str  # "success" | "error"
    detail: str  # sem credenciais nem corpo do provedor


@dataclass(frozen=True)
class _Candidate:
    """Uma ferramenta disponível amarrada à conexão real do usuário."""

    tool: Any
    row: IntegrationConnection


def _provider_from_action(raw: Any) -> str | None:
    """Lê o provider de uma entrada de ação, aceitando top-level ou config."""
    if not isinstance(raw, dict):
        return None
    provider = raw.get("provider")
    if provider is None and isinstance(raw.get("config"), dict):
        provider = raw["config"].get("provider")
    return provider if isinstance(provider, str) and provider else None


def _call_fields(call: Any) -> tuple[str | None, Any]:
    """Aceita tanto objetos `.function.name/.arguments` quanto dicts."""
    if isinstance(call, dict):
        return call.get("name"), call.get("arguments")
    function = getattr(call, "function", None)
    return getattr(function, "name", None), getattr(function, "arguments", None)


def _parse_arguments(arguments: Any) -> dict[str, Any]:
    """Converte os argumentos da chamada em um dict; lança em conteúdo inválido."""
    if isinstance(arguments, dict):
        return arguments
    if arguments is None:
        return {}
    if not isinstance(arguments, str):
        raise TypeError("argumentos inválidos")
    parsed = json.loads(arguments)
    if not isinstance(parsed, dict):
        raise TypeError("argumentos inválidos")
    return parsed


def _error_detail(exc: Exception) -> str:
    """Detalhe sanitizado: nunca vaza corpo do provedor nem credenciais."""
    if isinstance(exc, NangoClientError):
        return str(exc)
    if isinstance(exc, json.JSONDecodeError):
        return "argumentos_json_invalidos"
    if isinstance(exc, TypeError):
        return "argumentos_invalidos"
    return "falha_na_execucao"


def _success_detail(name: str, result: Any) -> str:
    """Resumo seguro; inclui um id do resultado apenas se ele for inócuo."""
    if isinstance(result, dict):
        identifier = result.get("id")
    else:
        identifier = getattr(result, "id", None)
    if isinstance(identifier, str) and _SAFE_IDENTIFIER.fullmatch(identifier):
        return f"{name}:{identifier}"
    return name


async def _default_llm_call(
    messages: list[dict[str, Any]], tools: list[dict[str, Any]],
) -> list[Any]:
    """Chamada OpenAI padrão; substituível por llm_call nos testes."""
    async with AsyncOpenAI(api_key=settings.OPENAI_API_KEY) as client:
        response = await chat_create(
            client,
            model=settings.OPENAI_ROUTER_MODEL,
            messages=messages,
            tools=tools,
        )
    return list(response.choices[0].message.tool_calls or [])


async def _candidates(
    session: AsyncSession, user_id: uuid.UUID, template_id: uuid.UUID | None,
) -> list[_Candidate]:
    """Ferramentas do template habilitadas para as conexões ativas do usuário."""
    user = await session.get(User, user_id)
    if user is None or user.workspace_id is None:
        return []
    connections = (await session.exec(select(IntegrationConnection).where(
        IntegrationConnection.workspace_id == user.workspace_id,
        IntegrationConnection.user_id == user_id,
        IntegrationConnection.status == "active",
    ))).all()
    by_provider = {row.provider: row for row in connections}

    if template_id is None:
        return []
    template = await session.get(Template, template_id)
    if template is None or template.user_id != user_id:
        return []
    enabled: set[str] = set()
    for raw in template.actions or []:
        if not isinstance(raw, dict) or raw.get("enabled") is not True or raw.get("type") != "integration":
            continue
        provider = _provider_from_action(raw)
        if provider is not None:
            enabled.add(provider)
    if not enabled:
        return []
    return [
        _Candidate(tool=tool, row=by_provider[tool.provider])
        for tool in all_tools()
        if tool.provider in enabled and tool.provider in by_provider
    ]


async def execute_template_tools(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    template_id: uuid.UUID | None,
    transcription: str,
    note_body: str,
    llm_call: Callable[[list[dict[str, Any]], list[dict[str, Any]]], Awaitable[list[dict[str, Any]]]] | None = None,
) -> list[ToolCallResult]:
    """Descobre, planeja e executa as ferramentas de integração de um template.

    Fail-closed: sem transcrição, sem candidatos ou sem chave OpenAI configurada
    retorna uma lista vazia sem tocar na rede.
    """
    if not transcription or not transcription.strip():
        return []

    candidates = await _candidates(session, user_id, template_id)
    if not candidates:
        return []

    if llm_call is None:
        if not settings.OPENAI_API_KEY:
            return []
        llm_call = _default_llm_call

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(
            {"transcricao": transcription, "nota": note_body}, ensure_ascii=False,
        )},
    ]
    tool_schemas = llm_tools([candidate.tool for candidate in candidates])
    try:
        calls = await llm_call(messages, tool_schemas)
    except Exception:
        logger.exception("Falha ao planejar as ferramentas de integração")
        return []

    available = {candidate.tool.name: candidate for candidate in candidates}
    results: list[ToolCallResult] = []
    for call in calls or []:
        name, arguments = _call_fields(call)
        tool_name = name if isinstance(name, str) and name else "<unknown>"
        candidate = available.get(name) if isinstance(name, str) else None
        if candidate is None:
            results.append(ToolCallResult(tool=tool_name, status="error", detail="tool não disponível"))
            continue
        try:
            kwargs = _parse_arguments(arguments)
            nango_client = NangoClient(
                api_key=settings.NANGO_ENV_KEY,
                base_url=settings.NANGO_BASE_URL or _DEFAULT_NANGO_BASE_URL,
            )
            result = await candidate.tool.handler(
                nango_client,
                candidate.row.nango_connection_id,
                provider_config_key=candidate.row.provider_config_key,
                **kwargs,
            )
        except (NangoClientError, ValueError, TypeError, json.JSONDecodeError) as exc:
            logger.warning("Ferramenta %s falhou: %s", tool_name, _error_detail(exc))
            results.append(ToolCallResult(tool=tool_name, status="error", detail=_error_detail(exc)))
            continue
        results.append(ToolCallResult(
            tool=tool_name, status="success", detail=_success_detail(tool_name, result),
        ))
    logger.info("Ferramentas de integração executadas: %s", [
        (item.tool, item.status) for item in results
    ])
    return results
