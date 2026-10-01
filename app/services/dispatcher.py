"""AnotAI — dispatcher: variáveis dinâmicas, segredos do usuário e ExecutionLogs."""

import json
import re
import uuid
from typing import Any

import httpx
from openai import AsyncOpenAI
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from telegram import Bot
from telegram.error import TelegramError

from app.core.config import settings
from app.core.user_secret_crypto import decrypt_user_secret
from app.models.execution_log import ExecutionLog, ExecutionStatus
from app.models.mcp_server import UserMCPServer
from app.models.note import Note
from app.models.template import Template
from app.models.user_secret import UserSecret
from app.services import mcp_client
from app.services import mcp_oauth
from app.services.llm import chat_create

MISSING_PLACEHOLDER = "N/A"

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
        return MISSING_PLACEHOLDER
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) if value else MISSING_PLACEHOLDER
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False) if value else MISSING_PLACEHOLDER
    text = str(value).strip()
    return text if text else MISSING_PLACEHOLDER


def render_template_string(template_str: str, context: dict[str, Any]) -> str:
    """Interpola `{campo}` e `{objeto.subcampo}` com o contexto.

    Variáveis ausentes viram `"N/A"` (fallback gracioso, nunca quebra).
    """
    if not template_str:
        return template_str

    def _repl(match: re.Match[str]) -> str:
        return _format_value(_lookup(context, match.group(1)))

    try:
        return _VAR_PATTERN.sub(_repl, template_str)
    except Exception:
        return template_str


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


async def _send_telegram(
    config: dict[str, Any],
    structured: dict[str, Any],
    note: Note,
    secrets: dict[str, str],
) -> dict[str, Any]:
    token = str(config.get("bot_token") or secrets.get("TELEGRAM_BOT_TOKEN") or "")
    chat_id = str(config.get("chat_id") or secrets.get("TELEGRAM_CHAT_ID") or "")
    if not token or "${" in token or not chat_id or "${" in chat_id:
        raise RuntimeError(
            "Telegram não conectado: conecte seu chat em Segredos "
            "(só o ID do chat — o bot é o oficial da plataforma)"
        )
    tpl_text = str(config.get("template") or config.get("message") or "{resumo}")
    text = render_template_string(tpl_text, _render_context(note, structured))
    bot = Bot(token=token)
    try:
        msg = await bot.send_message(chat_id=chat_id, text=text[:4000])
        return {"message_id": msg.message_id, "chat_id": chat_id}
    except TelegramError as exc:
        raise RuntimeError(f"Telegram API: {exc}") from exc


async def _post_webhook(
    config: dict[str, Any], note: Note, structured: dict[str, Any]
) -> dict[str, Any]:
    url = str(config.get("url") or "")
    if not url or "${" in url or url.startswith("https://example.com"):
        raise RuntimeError("Webhook não configurado (url ausente ou de exemplo)")
    method = str(config.get("method", "POST")).upper()
    headers = dict(config.get("headers") or {})
    body = {
        "note_id": str(note.id),
        "template_id": str(note.template_id),
        "transcription": note.raw_transcription or "",
        "structured_data": structured,
    }
    timeout = settings.WEBHOOK_TIMEOUT_SECONDS
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.request(method, url, json=body, headers=headers)
        resp.raise_for_status()
        return {"status_code": resp.status_code, "url": url}


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


async def _call_mcp_tool(
    config: dict[str, Any], structured: dict[str, Any], note: Note, session: AsyncSession
) -> dict[str, Any]:
    """Executa ação `mcp_tool`.

    Formato novo: `{server_id, tool_name?, arguments_mapping?, dynamic_llm_call?}`.
    Formato legado: `{server_url, tool/tool_name, arguments?}` (mantido p/ compat).
    """
    if config.get("server_id"):
        return await _call_mcp_tool_server(config, structured, note, session)
    # --- legado: URL direta, sem catálogo de servidores ---
    server_url = str(config.get("server_url") or "")
    tool = str(config.get("tool") or config.get("tool_name") or "")
    if not server_url or "${" in server_url or not tool:
        raise RuntimeError("MCP não configurado (server_url/tool ausentes)")
    arguments = dict(config.get("arguments") or {})
    arguments.setdefault("structured_data", structured)
    result = await mcp_client.call_tool(server_url, tool, arguments, None)
    return {"tool": tool, "result": result}


async def _call_mcp_tool_server(
    config: dict[str, Any], structured: dict[str, Any], note: Note, session: AsyncSession
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
    arguments = _interpolate_args(mapping, context)
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


async def dispatch_actions(note: Note, template: Template, session: AsyncSession) -> list[ExecutionLog]:
    """Itera `template.actions`, despacha cada uma isoladamente e salva logs. Nunca levanta.

    Antes de cada ação, `${SEGREDO}` é resolvido com os `UserSecret` do dono da
    nota (+ envs globais como fallback). Segredo ausente → log `failed` com
    mensagem clara, sem abortar as demais ações. O payload do log guarda a
    config original (sem valores de segredo resolvidos).
    """
    # Public/system templates (user_id=None) and other users' templates must
    # never execute in the context of this note, regardless of enabled flags.
    if template.user_id != note.user_id:
        return []

    structured: dict[str, Any] = note.structured_data or {}
    scope = build_secret_scope(await load_user_secrets(session, note.user_id))
    logs: list[ExecutionLog] = []

    for action in template.actions or []:
        if isinstance(action, dict) and action.get("enabled") is False:
            continue
        action_type = str((action or {}).get("type", "unknown"))
        original_config = dict((action or {}).get("config", {}))
        try:
            resolved_config, missing = resolve_secrets_in_config(original_config, scope)
            if not isinstance(resolved_config, dict):
                raise RuntimeError("Config da ação inválida (esperado objeto)")
            if missing:
                raise _missing_secret_error(missing)
            if action_type == "telegram":
                result = await _send_telegram(resolved_config, structured, note, scope)
            elif action_type == "webhook":
                result = await _post_webhook(resolved_config, note, structured)
            elif action_type == "mcp_tool":
                result = await _call_mcp_tool(resolved_config, structured, note, session)
            elif action_type in {"whatsapp", "email"}:
                raise RuntimeError(f"Ação '{action_type}' ainda não implementada (esqueleto)")
            else:
                raise RuntimeError(f"Tipo de ação desconhecido: {action_type}")
            logs.append(
                ExecutionLog(
                    note_id=note.id,
                    action_type=action_type,
                    status=ExecutionStatus.SUCCESS,
                    payload={"config": original_config, "result": result},
                )
            )
        except Exception as exc:  # noqa: BLE001 — cada ação é isolada
            logs.append(
                ExecutionLog(
                    note_id=note.id,
                    action_type=action_type,
                    status=ExecutionStatus.FAILED,
                    payload={"config": original_config},
                    error_message=str(exc)[:4000],
                )
            )

    for log in logs:
        session.add(log)
    await session.commit()
    return logs
