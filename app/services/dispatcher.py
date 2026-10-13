"""Shared secret and MCP provider adapters; execution is owned by IntegrationService."""

import json
import re
import uuid
from typing import Any

from openai import AsyncOpenAI
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.core.user_secret_crypto import decrypt_user_secret
from app.models.mcp_server import UserMCPServer
from app.models.note import Note
from app.models.user_secret import UserSecret
from app.services import mcp_client, mcp_oauth
from app.services.llm import chat_create

_SECRET_PATTERN = re.compile(r"\$\{([A-Za-z0-9_]+)\}")
_VAR_PATTERN = re.compile(r"\{([^{}]+)\}")


def _lookup(context: dict[str, Any], path: str) -> Any:
    """Resolve `a.b.c` dentro de dicts aninhados; None se qualquer nível faltar."""
    current: Any = context
    for part in path.strip().split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return None
    return current


def _format_value(value: Any) -> str:
    if value is None:
        raise ValueError("Variável de template ausente")
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        return ", ".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    text = str(value).strip()
    return text


def render_template_string(template_str: str, context: dict[str, Any]) -> str:
    """Interpolate fields without silently inventing missing values."""
    if not template_str:
        return template_str

    def _repl(match: re.Match[str]) -> str:
        return _format_value(_lookup(context, match.group(1)))

    return _VAR_PATTERN.sub(_repl, template_str)


def render_template_text(tpl_text: str, data: dict[str, Any]) -> str:
    """Alias legado de `render_template_string`."""
    return render_template_string(tpl_text, data)


async def load_user_secrets(session: AsyncSession, user_id: uuid.UUID) -> dict[str, str]:
    """Mapa key->value dos segredos do dono da nota (vazio se nenhum)."""
    result = await session.exec(select(UserSecret).where(UserSecret.user_id == user_id))
    return {
        secret.key: decrypt_user_secret(
            secret.user_id, secret.id, secret.value, key=settings.USER_SECRET_ENCRYPTION_KEY
        )
        for secret in result.all()
    }


def resolve_secrets_in_config(config: Any, secrets: dict[str, str]) -> tuple[Any, list[str]]:
    """Interpola `${KEY}` recursivamente em dicts/listas/strings.

    Retorna `(config_resolvido, chaves_ausentes)`. Placeholders sem valor
    correspondente são mantidos para que o chamador registre a falha.
    """
    missing: list[str] = []

    def _resolve(value: Any) -> Any:
        if isinstance(value, str):
            def _sub(match: re.Match[str]) -> str:
                key = match.group(1)
                if key in secrets:
                    return secrets[key]
                if key not in missing:
                    missing.append(key)
                return match.group(0)

            return _SECRET_PATTERN.sub(_sub, value)
        if isinstance(value, dict):
            return {k: _resolve(v) for k, v in value.items()}
        if isinstance(value, list):
            return [_resolve(v) for v in value]
        return value

    return _resolve(config), missing


def build_secret_scope(user_secrets: dict[str, str]) -> dict[str, str]:
    """Segredos do usuário com fallback para as envs globais (quando preenchidas).

    Modelo oficial: o envio Telegram usa o bot da plataforma
    (`TELEGRAM_BOT_TOKEN` global). O usuário só conecta o próprio chat
    (`TELEGRAM_CHAT_ID`). Um `TELEGRAM_BOT_TOKEN` por usuário ainda é aceito
    como override legado, mas a UI não deve mais pedi-lo.
    """
    scope: dict[str, str] = {}
    if settings.TELEGRAM_BOT_TOKEN:
        scope["TELEGRAM_BOT_TOKEN"] = settings.TELEGRAM_BOT_TOKEN
    if settings.TELEGRAM_DEFAULT_CHAT_ID:
        scope["TELEGRAM_CHAT_ID"] = settings.TELEGRAM_DEFAULT_CHAT_ID
    if settings.MCP_SERVER_URL:
        scope["MCP_SERVER_URL"] = settings.MCP_SERVER_URL
    scope.update(user_secrets)
    return scope


def _missing_secret_error(missing: list[str]) -> RuntimeError:
    keys = ", ".join(f"${{{k}}}" for k in missing)
    return RuntimeError(f"Segredo {keys} não configurado pelo usuário")


def _render_context(note: Note, structured: dict[str, Any]) -> dict[str, Any]:
    created = note.created_at.isoformat() if hasattr(note.created_at, "isoformat") else str(note.created_at)
    return {
        **structured,
        "transcricao": note.raw_transcription or "",
        "transcription": note.raw_transcription or "",
        "note_id": str(note.id),
        "created_at": created,
    }


def _interpolate_args(mapping: Any, context: dict[str, Any]) -> Any:
    """Interpola `{campo}`/`{obj.sub}` recursivamente no arguments_mapping."""
    if isinstance(mapping, str):
        return render_template_string(mapping, context)
    if isinstance(mapping, dict):
        return {k: _interpolate_args(v, context) for k, v in mapping.items()}
    if isinstance(mapping, list):
        return [_interpolate_args(v, context) for v in mapping]
    return mapping


async def _resolve_mcp_server(
    session: AsyncSession, server_id: Any, user_id: uuid.UUID
) -> UserMCPServer:
    try:
        sid = uuid.UUID(str(server_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise RuntimeError("mcp_tool: server_id inválido") from exc
    server = await session.get(UserMCPServer, sid)
    if server is None or server.user_id != user_id:
        raise RuntimeError("Servidor MCP não encontrado")
    if not server.is_active:
        raise RuntimeError(f"Servidor MCP '{server.name}' está inativo")
    return server


async def _call_mcp_tool_server(
    config: dict[str, Any], structured: dict[str, Any], note: Note, session: AsyncSession,
    *, rendered_arguments: bool = False,
) -> dict[str, Any]:
    server = await _resolve_mcp_server(session, config.get("server_id"), note.user_id)
    secrets = build_secret_scope(await load_user_secrets(session, note.user_id))
    access_token = await mcp_oauth.get_access_token(session, server, note.user_id)
    if access_token:
        headers = {"Authorization": f"Bearer {access_token}"}
        missing: list[str] = []
    else:
        headers, missing = mcp_client.resolve_mcp_auth(server.auth_header_key, server.auth_secret_name, secrets)
        if missing:
            raise _missing_secret_error(missing)
    server_url, missing_url = mcp_client.resolve_server_url(server.server_url, secrets)
    if missing_url:
        raise _missing_secret_error(missing_url)
    context = _render_context(note, structured)
    if bool(config.get("dynamic_llm_call", False)):
        return await _mcp_dynamic_call(server, server_url, headers, structured, note)
    tool_name = str(config.get("tool_name") or "")
    if not tool_name:
        raise RuntimeError("mcp_tool: tool_name ausente (ou use dynamic_llm_call=true)")
    mapping = config.get("arguments_mapping") or {}
    if not isinstance(mapping, dict):
        raise RuntimeError("mcp_tool: arguments_mapping deve ser um objeto")
    arguments = mapping if rendered_arguments else _interpolate_args(mapping, context)
    if not isinstance(arguments, dict):
        raise RuntimeError("mcp_tool: arguments_mapping deve resultar em objeto")
    result = await mcp_client.call_tool(server_url, tool_name, arguments, headers)
    return {"server": server.name, "tool": tool_name, "arguments": arguments, "result": result}


def _mcp_tools_to_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    openai_tools: list[dict[str, Any]] = []
    for tool in tools:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        # Automações no modo simples não ganham poderes para apagar ou destruir
        # conteúdo. Essas ações seguem fora da seleção automática da IA.
        identity = f"{tool.get('name', '')} {tool.get('description', '')}".casefold()
        if any(word in identity for word in (
            "delete", "remove", "trash", "destroy", "revoke", "apagar", "excluir", "remover", "lixeira",
            "send", "publish", "invite", "enviar", "publicar", "convidar",
        )):
            continue
        params = tool.get("inputSchema") or tool.get("input_schema") or {"type": "object"}
        if not isinstance(params, dict):
            params = {"type": "object"}
        openai_tools.append({
            "type": "function",
            "function": {
                "name": str(tool["name"]),
                "description": str(tool.get("description") or ""),
                "parameters": params,
            },
        })
    return openai_tools


async def _mcp_dynamic_call(
    server: UserMCPServer, server_url: str, headers: dict[str, str], structured: dict[str, Any], note: Note
) -> dict[str, Any]:
    """LLM escolhe a tool e preenche os argumentos via OpenAI Tool Calling."""
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY não configurada para chamada dinâmica MCP")
    tools = await mcp_client.list_tools(server_url, headers)
    openai_tools = _mcp_tools_to_openai(tools)
    if not openai_tools:
        raise RuntimeError(f"Servidor MCP '{server.name}' não tem ações seguras disponíveis para automação")
    system = (
        "Você é o orquestrador do AnotAI. Escolha a ferramenta MCP adequada e "
        "preencha TODOS os argumentos obrigatórios usando a transcrição e os "
        "dados estruturados. Não invente credenciais, não envie mensagens/e-mails "
        "nem faça publicações; use somente ferramentas de leitura, criação ou "
        "atualização que estejam disponíveis. Não chame sem argumentos exigidos."
    )
    user_msg = (
        f"Transcrição:\n{note.raw_transcription or ''}\n\n"
        f"Dados estruturados:\n{json.dumps(structured, ensure_ascii=False)}"
    )
    try:
        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        resp = await chat_create(
            client,
            model=settings.OPENAI_EXTRACTOR_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            tools=openai_tools,  # type: ignore[arg-type]
            tool_choice="auto",
            temperature=0,
        )
        calls = resp.choices[0].message.tool_calls or []
    except mcp_client.MCPError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Falha na LLM (MCP dinâmico): {exc}") from exc
    if not calls:
        raise RuntimeError("LLM não escolheu nenhuma ferramenta MCP")
    fn = calls[0].function
    try:
        args = json.loads(fn.arguments or "{}")
    except ValueError as exc:
        raise RuntimeError("LLM retornou argumentos inválidos para a ferramenta MCP") from exc
    if not isinstance(args, dict):
        raise RuntimeError("LLM retornou argumentos inválidos para a ferramenta MCP")
    result = await mcp_client.call_tool(server_url, fn.name, args, headers)
    return {"server": server.name, "tool": fn.name, "arguments": args, "result": result, "via": "dynamic_llm_call"}
