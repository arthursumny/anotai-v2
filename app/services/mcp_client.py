"""AnotAI — cliente MCP sobre HTTP/JSON-RPC (Streamable HTTP POST).

Compatível com servidores MCP que expõem `tools/list` e `tools/call` via
POST JSON-RPC (resposta JSON direta ou embrulhada em SSE `text/event-stream`).
Toda falha de rede, timeout ou erro JSON-RPC vira `MCPError` com mensagem
clara (o dispatcher registra como `failed` sem quebrar as demais ações).
"""
import json
import re
from typing import Any

import httpx

from app.core.config import settings


class MCPError(Exception):
    """Falha ao falar com o servidor MCP."""


_SECRET_REF = re.compile(r"\$\{([A-Za-z0-9_]+)\}")


def resolve_mcp_auth(
    auth_header_key: str | None,
    auth_secret_name: str | None,
    secrets: dict[str, str],
) -> tuple[dict[str, str], list[str]]:
    """Monta os headers de auth a partir dos `UserSecret` do usuário.

    Não há padrão de auth no MCP: cada servidor define o seu. Por isso os
    campos são livres — `auth_header_key` é qualquer header (`Authorization`,
    `X-API-Key`, ...) e o segredo guarda o **valor completo** do header
    (ex: `Bearer abc123` ou só a chave, conforme o servidor exigir).
    Sem `auth_header_key`, nenhuma auth é enviada (servidor local/aberto).

    Retorna `(headers, chaves_ausentes)`. Não expõe valores em mensagens.
    """
    if not auth_header_key:
        return {}, []
    secret_name = (auth_secret_name or "").strip().upper()
    if secret_name and secret_name in secrets:
        return {auth_header_key: secrets[secret_name]}, []
    return {}, [secret_name or auth_header_key]


def resolve_server_url(server_url: str, secrets: dict[str, str]) -> tuple[str, list[str]]:
    """Interpola `${SECRETO}` na URL (ex: `https://mcp.exemplo.com/${TENANT}/mcp`).

    Útil quando cada fluxo/conta usa um subdomínio, tenant ou token na URL.
    Retorna `(url_resolvida, chaves_ausentes)`.
    """
    missing: list[str] = []

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in secrets:
            return secrets[key]
        if key not in missing:
            missing.append(key)
        return match.group(0)

    return _SECRET_REF.sub(_sub, server_url or ""), missing


async def _send_post(
    server_url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    extra_headers: dict[str, str] | None = None,
) -> httpx.Response:
    timeout = float(settings.WEBHOOK_TIMEOUT_SECONDS)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(
                server_url,
                json=payload,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json, text/event-stream",
                    **headers,
                    **(extra_headers or {}),
                },
            )
    except httpx.TimeoutException as exc:
        raise MCPError(f"Timeout após {timeout:g}s falando com o servidor MCP") from exc
    except httpx.ConnectError as exc:
        raise MCPError(f"Não foi possível conectar ao servidor MCP ({server_url})") from exc
    except httpx.HTTPError as exc:
        raise MCPError(f"Falha de rede ao chamar o servidor MCP: {exc}") from exc

    try:
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise MCPError(f"Servidor MCP respondeu HTTP {resp.status_code}") from exc

    return resp


async def _post_json_rpc(
    server_url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    extra_headers: dict[str, str] | None = None,
) -> tuple[dict[str, Any], str | None]:
    resp = await _send_post(server_url, headers, payload, extra_headers)

    body = _decode_body(resp, server_url)
    if not isinstance(body, dict) or body.get("jsonrpc") != "2.0":
        raise MCPError("Resposta inválida do servidor MCP (esperado objeto JSON-RPC 2.0)")
    if "error" in body and body["error"] is not None:
        err = body["error"] if isinstance(body["error"], dict) else {"message": str(body["error"])}
        raise MCPError(f"MCP error {err.get('code', '?')}: {err.get('message', 'desconhecido')}")
    result = body.get("result", {})
    if not isinstance(result, dict):
        raise MCPError("Resposta inválida do servidor MCP (result não é objeto)")
    return result, resp.headers.get("Mcp-Session-Id")


async def _post_notification(
    server_url: str, headers: dict[str, str], payload: dict[str, Any], extra_headers: dict[str, str]
) -> None:
    """Notificações JSON-RPC não têm resposta; Streamable HTTP pode responder 202."""
    await _send_post(server_url, headers, payload, extra_headers)


def _decode_body(resp: httpx.Response, server_url: str) -> Any:
    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        # Streamable HTTP: uma ou mais linhas `data: {...}`; vale a última.
        last: str | None = None
        for line in resp.text.splitlines():
            line = line.strip()
            if line.startswith("data:"):
                data = line[5:].strip()
                if data and data != "[DONE]":
                    last = data
        if last is None:
            raise MCPError(f"Servidor MCP ({server_url}) retornou SSE sem eventos data")
        try:
            return json.loads(last)
        except ValueError as exc:
            raise MCPError("Evento SSE do servidor MCP não é JSON válido") from exc
    try:
        return resp.json()
    except ValueError as exc:
        raise MCPError(f"Servidor MCP ({server_url}) não retornou JSON") from exc


async def list_tools(server_url: str, headers: dict[str, str] | None = None) -> list[dict[str, Any]]:
    """Inicializa a sessão MCP e lista tools por Streamable HTTP."""
    auth_headers = headers or {}
    initialized, session_id = await _post_json_rpc(
        server_url,
        auth_headers,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "anotai", "version": "1.0.0"},
            },
        },
    )
    protocol_version = str(initialized.get("protocolVersion") or "2025-11-25")
    negotiated_headers = {"MCP-Protocol-Version": protocol_version}
    if session_id:
        negotiated_headers["Mcp-Session-Id"] = session_id
    await _post_notification(
        server_url,
        auth_headers,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        negotiated_headers,
    )
    result, _ = await _post_json_rpc(
        server_url,
        auth_headers,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        negotiated_headers,
    )
    tools = result.get("tools", [])
    if not isinstance(tools, list):
        raise MCPError("Resposta inválida do servidor MCP (tools não é lista)")
    normalized: list[dict[str, Any]] = []
    for tool in tools:
        if isinstance(tool, dict) and tool.get("name"):
            normalized.append(tool)
    return normalized


async def call_tool(
    server_url: str,
    tool_name: str,
    arguments: dict[str, Any] | None,
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    """`tools/call` → result da ferramenta. `isError=True` vira `MCPError`."""
    if not tool_name:
        raise MCPError("Nome da ferramenta MCP ausente")
    auth_headers = headers or {}
    initialized, session_id = await _post_json_rpc(
        server_url,
        auth_headers,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "anotai", "version": "1.0.0"},
            },
        },
    )
    negotiated_headers = {"MCP-Protocol-Version": str(initialized.get("protocolVersion") or "2025-11-25")}
    if session_id:
        negotiated_headers["Mcp-Session-Id"] = session_id
    await _post_notification(
        server_url,
        auth_headers,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        negotiated_headers,
    )
    result, _ = await _post_json_rpc(
        server_url,
        auth_headers,
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
         "params": {"name": tool_name, "arguments": arguments or {}}},
        negotiated_headers,
    )
    if result.get("isError") is True:
        raise MCPError(f"Ferramenta '{tool_name}' retornou erro: {_content_text(result)}")
    return result


def _content_text(result: dict[str, Any]) -> str:
    parts: list[str] = []
    content = result.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and isinstance(block.get("text"), str):
                parts.append(block["text"])
    text = " ".join(parts).strip()
    return text[:500] if text else "sem detalhes"
