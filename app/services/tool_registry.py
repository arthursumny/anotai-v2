"""Catálogo declarativo das ferramentas que a LLM pode chamar (tool calling).

Este módulo é a fronteira de segurança do tool calling: os ENDPOINTS do provedor
e as CHAVES de configuração do Nango ficam fixos aqui, em código, e nunca são
decididos pela conversa. A LLM só escolhe o NOME de uma ferramenta e preenche os
argumentos definidos pelo JSON Schema (`schema`); o orquestrador injeta
`connection_id` e `provider_config_key` a partir da conexão do usuário. Assim,
nenhum conteúdo gerado a partir do áudio vira URL, host ou escopo OAuth.

Contrato dos handlers: ``async def handler(nango, connection_id, *,
provider_config_key, <args...>) -> dict``. Eles recebem os argumentos extraídos
pela LLM como kwargs e chamam
``await nango.proxy_request(method, provider_config_key, connection_id, endpoint,
params=..., json=...)``. O retorno é sempre um dict pequeno e higienizado (por
exemplo ``{"id": ...}``), sem ecoar credenciais.

Este módulo importa apenas a stdlib e ``app.core.config.settings``: nenhum
acesso a banco ou rede acontece aqui.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from app.core.config import settings

ToolHandler = Callable[..., Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class ToolDefinition:
    """Ferramenta chamável pela LLM, com endpoint e config fixos em código."""

    # Nome exposto à LLM (ex.: "create_google_task").
    name: str
    # Valor de IntegrationConnection.provider (ex.: "google_tasks").
    provider: str
    # NOME do atributo de settings com a config key do Nango para o provedor.
    config_key_setting: str
    # Quando a LLM deve chamar esta ferramenta (instrução, pt-BR).
    description: str
    # JSON Schema dos argumentos extraídos da conversa.
    schema: dict[str, Any]
    # async handler(nango, connection_id, *, provider_config_key, **kwargs) -> dict
    handler: ToolHandler


# Ids e nomes que entram em URL: validados ANTES de montar o endpoint, para que
# um valor malicioso não escape pela path do proxy.
_SPREADSHEET_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SHEET_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_ .-]{0,99}$")


def _id_from_body(body: Any) -> dict[str, Any]:
    """Extrai somente ``{"id": ...}`` de um corpo de resposta dict."""
    if isinstance(body, dict):
        return {"id": body.get("id")}
    return {}


async def _create_google_task(
    nango: Any,
    connection_id: str,
    *,
    provider_config_key: str,
    title: str,
    notes: str | None = None,
    due: str | None = None,
) -> dict[str, Any]:
    """Cria uma tarefa na lista padrão (`@default`) da conta conectada."""
    payload: dict[str, Any] = {"title": title}
    if notes is not None:
        payload["notes"] = notes
    if due is not None:
        payload["due"] = due
    # Endpoint fixo na lista @default — NUNCA uma variante /users/@me.
    response = await nango.proxy_request(
        "POST",
        provider_config_key,
        connection_id,
        "tasks/v1/lists/@default/tasks",
        json=payload,
    )
    return _id_from_body(response.body)


async def _create_google_drive_file(
    nango: Any,
    connection_id: str,
    *,
    provider_config_key: str,
    name: str,
    mimeType: str = "text/markdown",
    description: str | None = None,
) -> dict[str, Any]:
    """Cria um arquivo no Drive da conta conectada."""
    payload: dict[str, Any] = {"name": name, "mimeType": mimeType}
    if description is not None:
        payload["description"] = description
    response = await nango.proxy_request(
        "POST",
        provider_config_key,
        connection_id,
        "drive/v3/files",
        json=payload,
    )
    return _id_from_body(response.body)


async def _append_google_sheet_row(
    nango: Any,
    connection_id: str,
    *,
    provider_config_key: str,
    spreadsheet_id: str,
    values: list[Any],
    sheet_name: str = "Sheet1",
) -> dict[str, Any]:
    """Anexa uma linha a uma aba da planilha conectada."""
    # Estes dois valores entram na URL: valida o formato antes de montar o path.
    if not isinstance(spreadsheet_id, str) or not _SPREADSHEET_ID_RE.fullmatch(spreadsheet_id):
        raise ValueError("spreadsheet_id inválido")
    if not isinstance(sheet_name, str) or not _SHEET_NAME_RE.fullmatch(sheet_name):
        raise ValueError("sheet_name inválido")
    endpoint = f"sheets/v4/spreadsheets/{spreadsheet_id}/values/{sheet_name}!A1:append"
    response = await nango.proxy_request(
        "POST",
        provider_config_key,
        connection_id,
        endpoint,
        params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
        json={
            "range": f"{sheet_name}!A1",
            "majorDimension": "ROWS",
            "values": [values],
        },
    )
    body = response.body
    if not isinstance(body, dict):
        return {}
    sanitized: dict[str, Any] = {}
    table_range = body.get("tableRange")
    if isinstance(table_range, str):
        sanitized["tableRange"] = table_range
    updates = body.get("updates")
    if isinstance(updates, dict):
        sanitized["updates"] = {
            key: updates[key]
            for key in ("updatedRange", "updatedRows", "updatedColumns", "updatedCells")
            if key in updates
        }
    return sanitized


async def _create_notion_page(
    nango: Any,
    connection_id: str,
    *,
    provider_config_key: str,
    title: str,
    content: str | None = None,
    database_id: str | None = None,
    title_property: str = "Name",
) -> dict[str, Any]:
    """Cria uma página no database do Notion configurado no servidor."""
    # O database de destino e o nome da propriedade de título são configuração
    # do servidor (por template), nunca conteúdo gerado pela LLM.
    database = database_id or settings.NANGO_NOTION_DEFAULT_DATABASE_ID
    if not database:
        raise ValueError("Notion database não configurado")
    payload: dict[str, Any] = {
        "parent": {"database_id": database},
        "properties": {
            title_property: {"title": [{"text": {"content": title}}]},
        },
    }
    if content:
        payload["children"] = [{
            "object": "block",
            "type": "paragraph",
            "paragraph": {"rich_text": [{"text": {"content": content}}]},
        }]
    response = await nango.proxy_request(
        "POST",
        provider_config_key,
        connection_id,
        "v1/pages",
        json=payload,
    )
    return _id_from_body(response.body)


_CREATE_GOOGLE_TASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {
            "type": "string",
            "description": "Título curto da tarefa a ser criada no Google Tasks.",
        },
        "notes": {
            "type": "string",
            "description": "Detalhes adicionais da tarefa (opcional).",
        },
        "due": {
            "type": "string",
            "description": (
                "data/hora limite em ISO 8601 com fuso, ex.: "
                "2026-10-14T16:00:00-03:00"
            ),
        },
    },
    "required": ["title"],
}

_CREATE_GOOGLE_DRIVE_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "name": {
            "type": "string",
            "description": "Nome do arquivo a ser criado no Google Drive.",
        },
        "mimeType": {
            "type": "string",
            "default": "text/markdown",
            "description": "Tipo MIME do arquivo (padrão text/markdown).",
        },
        "description": {
            "type": "string",
            "description": "Descrição opcional do arquivo.",
        },
    },
    "required": ["name"],
}

_APPEND_GOOGLE_SHEET_ROW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "spreadsheet_id": {
            "type": "string",
            "pattern": "^[A-Za-z0-9_-]{1,128}$",
            "description": "ID da planilha do Google Sheets onde a linha será anexada.",
        },
        "sheet_name": {
            "type": "string",
            "default": "Sheet1",
            "pattern": "^[A-Za-z0-9_][A-Za-z0-9_ .-]{0,99}$",
            "description": "Nome da aba da planilha (padrão Sheet1).",
        },
        "values": {
            "type": "array",
            "items": {"type": ["string", "number", "boolean"]},
            "minItems": 1,
            "maxItems": 50,
            "description": "Valores da linha, na ordem das colunas (1 a 50 itens).",
        },
    },
    "required": ["spreadsheet_id", "values"],
}

_CREATE_NOTION_PAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "title": {
            "type": "string",
            "description": "Título da página a ser criada no Notion.",
        },
        "content": {
            "type": "string",
            "description": "Conteúdo da página em texto simples (opcional).",
        },
    },
    "required": ["title"],
}

_CREATE_GOOGLE_TASK_TOOL = ToolDefinition(
    name="create_google_task",
    provider="google_tasks",
    config_key_setting="NANGO_GOOGLE_TASKS_PROVIDER_CONFIG_KEY",
    description=(
        "Cria uma tarefa no Google Tasks quando o usuário pede para lembrar, "
        "anotar ou agendar algo a fazer."
    ),
    schema=_CREATE_GOOGLE_TASK_SCHEMA,
    handler=_create_google_task,
)

_CREATE_GOOGLE_DRIVE_FILE_TOOL = ToolDefinition(
    name="create_google_drive_file",
    provider="google_drive",
    config_key_setting="NANGO_GOOGLE_DRIVE_PROVIDER_CONFIG_KEY",
    description=(
        "Cria um arquivo no Google Drive quando o usuário pede para salvar, "
        "guardar ou documentar um texto."
    ),
    schema=_CREATE_GOOGLE_DRIVE_FILE_SCHEMA,
    handler=_create_google_drive_file,
)

_APPEND_GOOGLE_SHEET_ROW_TOOL = ToolDefinition(
    name="append_google_sheet_row",
    provider="google_sheets",
    config_key_setting="NANGO_GOOGLE_SHEETS_PROVIDER_CONFIG_KEY",
    description=(
        "Anexa uma nova linha a uma planilha do Google Sheets quando o usuário "
        "pede para registrar dados estruturados em uma planilha."
    ),
    schema=_APPEND_GOOGLE_SHEET_ROW_SCHEMA,
    handler=_append_google_sheet_row,
)

_CREATE_NOTION_PAGE_TOOL = ToolDefinition(
    name="create_notion_page",
    provider="notion",
    config_key_setting="NANGO_NOTION_PROVIDER_CONFIG_KEY",
    description=(
        "Cria uma página no Notion quando o usuário pede para salvar a nota ou "
        "o conteúdo no espaço de trabalho dele."
    ),
    schema=_CREATE_NOTION_PAGE_SCHEMA,
    handler=_create_notion_page,
)


def notion_page_tool(database_id: str, title_property: str = "Name") -> ToolDefinition:
    """Ferramenta Notion com database/propriedade fixados por template.

    O destino é resolvido no servidor (binding por template), nunca pela LLM.
    """
    async def _handler(
        nango: Any,
        connection_id: str,
        *,
        provider_config_key: str,
        title: str,
        content: str | None = None,
    ) -> dict[str, Any]:
        return await _create_notion_page(
            nango,
            connection_id,
            provider_config_key=provider_config_key,
            title=title,
            content=content,
            database_id=database_id,
            title_property=title_property,
        )

    return ToolDefinition(
        name=_CREATE_NOTION_PAGE_TOOL.name,
        provider=_CREATE_NOTION_PAGE_TOOL.provider,
        config_key_setting=_CREATE_NOTION_PAGE_TOOL.config_key_setting,
        description=_CREATE_NOTION_PAGE_TOOL.description,
        schema=_CREATE_NOTION_PAGE_SCHEMA,
        handler=_handler,
    )


TOOL_REGISTRY: Mapping[str, ToolDefinition] = MappingProxyType({
    tool.name: tool for tool in (
        _CREATE_GOOGLE_TASK_TOOL,
        _CREATE_GOOGLE_DRIVE_FILE_TOOL,
        _APPEND_GOOGLE_SHEET_ROW_TOOL,
        _CREATE_NOTION_PAGE_TOOL,
    )
})


def get_tool(name: str) -> ToolDefinition | None:
    """Retorna a ferramenta pelo nome, ou None se não estiver no catálogo."""
    return TOOL_REGISTRY.get(name)


def all_tools() -> list[ToolDefinition]:
    """Todas as ferramentas na ordem do registro."""
    return list(TOOL_REGISTRY.values())


def llm_tools(tools: Iterable[ToolDefinition]) -> list[dict[str, Any]]:
    """Serializa ferramentas no formato de tool calling da OpenAI chat."""
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.schema,
            },
        }
        for tool in tools
    ]
