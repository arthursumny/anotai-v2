"""AnotAI — testes do MCP: cliente JSON-RPC (mock), dispatcher e endpoints."""

import uuid
from types import SimpleNamespace

import httpx
import pytest

from app.core.config import settings
from app.core.user_secret_crypto import encrypt_user_secret
import app.services.dispatcher as dispatcher
import app.services.mcp_client as mcp_client
from app.models.mcp_server import UserMCPServer
from app.models.note import Note, NoteStatus
from app.models.template import Template
from app.models.user_secret import UserSecret
from app.services.dispatcher import dispatch_actions
from app.services.mcp_client import MCPError, call_tool, list_tools, resolve_mcp_auth, resolve_server_url


# ---------------------------------------------------------------- fakes ---
class FakeResponse:
    def __init__(self, payload, status_code=200, content_type="application/json", headers=None):
        self._payload = payload
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self.headers.update(headers or {})
        self.text = payload if isinstance(payload, str) else ""

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(f"HTTP {self.status_code}", request=None, response=self)

    def json(self):
        if isinstance(self._payload, dict):
            return self._payload
        raise ValueError("not json")


class FakeAsyncClient:
    handler = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json=None, headers=None):
        assert FakeAsyncClient.handler is not None
        return FakeAsyncClient.handler(url, json, headers)


@pytest.fixture
def fake_http(monkeypatch):
    monkeypatch.setattr(mcp_client.httpx, "AsyncClient", FakeAsyncClient)
    FakeAsyncClient.handler = None
    return FakeAsyncClient


def _rpc_result(result, req_id=1, headers=None):
    return FakeResponse({"jsonrpc": "2.0", "id": req_id, "result": result}, headers=headers)


def _rpc_error(code, message, req_id=2):
    return FakeResponse({"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}})


def _mcp_response(payload, headers, on_request):
    """Handshake Streamable HTTP e valida a sessão antes de despachar a chamada."""
    method = payload["method"]
    if method == "initialize":
        return _rpc_result(
            {"protocolVersion": "2025-11-25", "capabilities": {}},
            req_id=payload["id"],
            headers={"Mcp-Session-Id": "session-test"},
        )
    if method == "notifications/initialized":
        assert headers["Mcp-Session-Id"] == "session-test"
        assert headers["MCP-Protocol-Version"] == "2025-11-25"
        return FakeResponse("", status_code=202, content_type="")
    assert headers["Mcp-Session-Id"] == "session-test"
    assert headers["MCP-Protocol-Version"] == "2025-11-25"
    return on_request(payload)


CALENDAR_TOOLS = [
    {"name": "create_event", "description": "Cria evento",
     "inputSchema": {"type": "object", "required": ["title"],
                     "properties": {"title": {"type": "string"}, "date": {"type": "string"}}}},
]


# ------------------------------------------------------- cliente: units ---
async def test_list_tools_success(fake_http):
    def handler(url, payload, headers):
        return _mcp_response(payload, headers, lambda request: _rpc_result(
            {"tools": CALENDAR_TOOLS}, req_id=request["id"]
        ))
    FakeAsyncClient.handler = handler
    tools = await list_tools("https://mcp.test/", {"Authorization": "Bearer x"})
    assert [t["name"] for t in tools] == ["create_event"]
    assert tools[0]["inputSchema"]["required"] == ["title"]


async def test_list_tools_rpc_error(fake_http):
    FakeAsyncClient.handler = lambda url, payload, headers: _mcp_response(
        payload, headers, lambda request: _rpc_error(-32601, "Method not found", request["id"])
    )
    with pytest.raises(MCPError, match="Method not found"):
        await list_tools("https://mcp.test/")


async def test_list_tools_sse_envelope(fake_http):
    def handler(url, payload, headers):
        def dispatch(request):
            sse = f'event: message\ndata: {{"jsonrpc": "2.0", "id": {request["id"]}, "result": {{"tools": []}}}}\n\n'
            return FakeResponse(sse, content_type="text/event-stream")
        return _mcp_response(payload, headers, dispatch)
    FakeAsyncClient.handler = handler
    assert await list_tools("https://mcp.test/") == []


async def test_call_tool_success(fake_http):
    def handler(url, payload, headers):
        def dispatch(request):
            assert request["method"] == "tools/call"
            assert request["params"]["name"] == "create_event"
            return _rpc_result({"content": [{"type": "text", "text": "evento 42"}], "isError": False}, req_id=request["id"])
        return _mcp_response(payload, headers, dispatch)
    FakeAsyncClient.handler = handler
    result = await call_tool("https://mcp.test/", "create_event", {"title": "X"})
    assert result["content"][0]["text"] == "evento 42"


async def test_call_tool_is_error(fake_http):
    def handler(url, payload, headers):
        return _mcp_response(payload, headers, lambda request: _rpc_result(
            {"content": [{"type": "text", "text": "data inválida"}], "isError": True}, req_id=request["id"]
        ))
    FakeAsyncClient.handler = handler
    with pytest.raises(MCPError, match="data inválida"):
        await call_tool("https://mcp.test/", "create_event", {})


async def test_network_failures_become_mcperror(fake_http):
    def refused(url, payload, headers):
        raise httpx.ConnectError("refused")
    FakeAsyncClient.handler = refused
    with pytest.raises(MCPError, match="conectar"):
        await list_tools("https://mcp.test/")

    FakeAsyncClient.handler = lambda url, payload, headers: FakeResponse({}, status_code=500)
    with pytest.raises(MCPError, match="HTTP 500"):
        await call_tool("https://mcp.test/", "t", {})


def test_resolve_mcp_auth():
    headers, missing = resolve_mcp_auth("Authorization", "NOTION_MCP_TOKEN", {"NOTION_MCP_TOKEN": "ntn-x"})
    assert headers == {"Authorization": "ntn-x"} and missing == []
    headers, missing = resolve_mcp_auth("Authorization", "NOTION_MCP_TOKEN", {})
    assert headers == {} and missing == ["NOTION_MCP_TOKEN"]
    assert resolve_mcp_auth(None, None, {}) == ({}, [])


def test_resolve_server_url():
    url, missing = resolve_server_url("https://mcp.exemplo.com/acme/mcp", {})
    assert (url, missing) == ("https://mcp.exemplo.com/acme/mcp", [])
    url, missing = resolve_server_url("https://mcp.exemplo.com/${TENANT}/mcp", {"TENANT": "acme"})
    assert (url, missing) == ("https://mcp.exemplo.com/acme/mcp", [])
    url, missing = resolve_server_url("https://${TENANT}.exemplo.com/${OTHER}/mcp", {"TENANT": "acme"})
    assert url == "https://acme.exemplo.com/${OTHER}/mcp" and missing == ["OTHER"]


def test_dynamic_mcp_hides_irreversible_and_outbound_actions():
    tools = dispatcher._mcp_tools_to_openai([
        {"name": "create_draft", "description": "Create an email draft", "inputSchema": {"type": "object"}},
        {"name": "send_email", "description": "Send an email", "inputSchema": {"type": "object"}},
        {"name": "delete_page", "description": "Delete a page", "inputSchema": {"type": "object"}},
        {"name": "create_event", "description": "Create a calendar event", "inputSchema": {"type": "object"}},
    ])
    assert [item["function"]["name"] for item in tools] == ["create_draft", "create_event"]


# ------------------------------------------------- dispatcher: integração ---
def _note_tpl(uid: uuid.UUID, actions: list[dict]):
    note = Note(
        user_id=uid,
        raw_transcription="Retorno do Thor dia 2026-10-01",
        structured_data={"animal_nome": "Thor", "retorno_em": "2026-10-01"},
        status=NoteStatus.PROCESSED,
    )
    tpl = Template(
        user_id=uid, title="Vet", semantic_trigger="x",
        output_schema={"type": "object"}, actions=actions, is_public=False,
    )
    return note, tpl


async def _mcp_server(db_session, uid: uuid.UUID, **over) -> UserMCPServer:
    server = UserMCPServer(
        user_id=uid, name="Agenda", server_url="https://mcp.test/",
        auth_header_key="Authorization", auth_secret_name="AGENDA_TOKEN",
    )
    for k, v in over.items():
        setattr(server, k, v)
    db_session.add(server)
    await db_session.commit()
    await db_session.refresh(server)
    return server


async def test_dispatch_mcp_static_mapping_with_secret(fake_http, db_session):
    captured = {}

    def handler(url, payload, headers):
        def dispatch(request):
            captured["headers"] = dict(headers or {})
            captured["payload"] = request
            if request["method"] == "tools/list":
                return _rpc_result({"tools": CALENDAR_TOOLS}, req_id=request["id"])
            return _rpc_result({"content": [{"type": "text", "text": "ok"}], "isError": False}, req_id=request["id"])
        return _mcp_response(payload, headers, dispatch)

    FakeAsyncClient.handler = handler
    uid = uuid.uuid4()
    secret = UserSecret(user_id=uid, key="AGENDA_TOKEN", value="")
    secret.value = encrypt_user_secret(
        uid, secret.id, "tok-agenda-1", key=settings.USER_SECRET_ENCRYPTION_KEY,
    )
    db_session.add(secret)
    server = await _mcp_server(db_session, uid)

    note, tpl = _note_tpl(uid, [{
        "type": "mcp_tool",
        "config": {
            "server_id": str(server.id),
            "tool_name": "create_event",
            "arguments_mapping": {"title": "{animal_nome} - Retorno", "date": "{retorno_em}"},
        },
    }])
    logs = await dispatch_actions(note, tpl, db_session)
    assert len(logs) == 1 and logs[0].status.value == "success"
    # segredo resolvido no header, interpolação nos argumentos
    assert captured["headers"].get("Authorization") == "tok-agenda-1"
    assert captured["payload"]["params"]["arguments"] == {"title": "Thor - Retorno", "date": "2026-10-01"}
    result = logs[0].payload["result"]
    assert result["server"] == "Agenda" and result["tool"] == "create_event"
    assert "tok-agenda-1" not in str(logs[0].payload)  # segredo não vaza no log


async def test_dispatch_mcp_missing_secret_fails_gracefully(db_session):
    uid = uuid.uuid4()
    server = await _mcp_server(db_session, uid)
    note, tpl = _note_tpl(uid, [
        {"type": "mcp_tool", "config": {"server_id": str(server.id), "tool_name": "create_event"}},
        {"type": "fax", "config": {}},
    ])
    logs = await dispatch_actions(note, tpl, db_session)  # nunca levanta
    assert len(logs) == 2 and all(lg.status.value == "failed" for lg in logs)
    assert "Segredo ${AGENDA_TOKEN} não configurado pelo usuário" in (logs[0].error_message or "")


async def test_dispatch_mcp_url_with_secret_placeholder(fake_http, db_session):
    captured = {}

    def handler(url, payload, headers):
        def dispatch(request):
            captured["url"] = url
            return _rpc_result({"content": [{"type": "text", "text": "ok"}], "isError": False}, req_id=request["id"])
        return _mcp_response(payload, headers, dispatch)

    FakeAsyncClient.handler = handler
    uid = uuid.uuid4()
    server = await _mcp_server(
        db_session, uid,
        server_url="https://mcp.exemplo.com/${TENANT}/mcp",
        auth_header_key=None, auth_secret_name=None,
    )
    secret = UserSecret(user_id=uid, key="TENANT", value="")
    secret.value = encrypt_user_secret(
        uid, secret.id, "acme", key=settings.USER_SECRET_ENCRYPTION_KEY,
    )
    db_session.add(secret)
    await db_session.commit()

    note, tpl = _note_tpl(uid, [{
        "type": "mcp_tool",
        "config": {"server_id": str(server.id), "tool_name": "create_event", "arguments_mapping": {}},
    }])
    logs = await dispatch_actions(note, tpl, db_session)
    assert len(logs) == 1 and logs[0].status.value == "success", logs[0].error_message
    assert captured["url"] == "https://mcp.exemplo.com/acme/mcp"


async def test_dispatch_mcp_url_missing_secret_fails(db_session):
    uid = uuid.uuid4()
    server = await _mcp_server(
        db_session, uid,
        server_url="https://mcp.exemplo.com/${TENANT}/mcp",
        auth_header_key=None, auth_secret_name=None,
    )
    note, tpl = _note_tpl(uid, [{
        "type": "mcp_tool",
        "config": {"server_id": str(server.id), "tool_name": "create_event"},
    }])
    logs = await dispatch_actions(note, tpl, db_session)
    assert len(logs) == 1 and logs[0].status.value == "failed"
    assert "Segredo ${TENANT} não configurado pelo usuário" in (logs[0].error_message or "")


async def test_dispatch_mcp_dynamic_llm_call(fake_http, db_session, monkeypatch):
    monkeypatch.setattr(dispatcher.settings, "OPENAI_API_KEY", "test-key")

    calls = {}

    def handler(url, payload, headers):
        def dispatch(request):
            if request["method"] == "tools/list":
                return _rpc_result({"tools": CALENDAR_TOOLS}, req_id=request["id"])
            calls["call"] = request
            return _rpc_result({"content": [{"type": "text", "text": "criado"}], "isError": False}, req_id=request["id"])
        return _mcp_response(payload, headers, dispatch)

    FakeAsyncClient.handler = handler

    class FakeCompletions:
        async def create(self, **kwargs):
            calls["openai_tools"] = kwargs.get("tools")
            fn = SimpleNamespace(name="create_event", arguments='{"title": "Thor - Retorno"}')
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(tool_calls=[SimpleNamespace(function=fn)]))])

    class FakeOpenAI:
        def __init__(self, api_key=None):
            self.chat = SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setattr(dispatcher, "AsyncOpenAI", FakeOpenAI)

    uid = uuid.uuid4()
    server = await _mcp_server(db_session, uid, auth_header_key=None, auth_secret_name=None)
    note, tpl = _note_tpl(uid, [{
        "type": "mcp_tool",
        "config": {"server_id": str(server.id), "dynamic_llm_call": True},
    }])
    logs = await dispatch_actions(note, tpl, db_session)
    assert len(logs) == 1 and logs[0].status.value == "success", logs[0].error_message
    # LLM recebeu as tools do servidor e a escolha dela foi executada
    assert calls["openai_tools"][0]["function"]["name"] == "create_event"
    assert calls["call"]["params"]["name"] == "create_event"
    assert calls["call"]["params"]["arguments"] == {"title": "Thor - Retorno"}
    assert logs[0].payload["result"]["via"] == "dynamic_llm_call"


# ------------------------------------------------------------- endpoints ---
async def test_mcp_crud_and_test_connection(client, auth_headers, fake_http):
    headers, _uid = auth_headers

    r = await client.post("/api/v1/mcp", headers=headers, json={"name": "x", "server_url": "sem-esquema"})
    assert r.status_code == 422

    r = await client.post("/api/v1/mcp", headers=headers, json={
        "name": "Agenda", "server_url": "https://mcp.test/",
        "auth_header_key": "Authorization", "auth_secret_name": "agenda_token",
    })
    assert r.status_code == 201, r.text
    server = r.json()
    assert server["auth_secret_name"] == "AGENDA_TOKEN"  # normalizado

    r = await client.get("/api/v1/mcp", headers=headers)
    assert r.status_code == 200 and len(r.json()) == 1

    # sem segredo cadastrado → 400 claro
    r = await client.post(f"/api/v1/mcp/{server['id']}/test", headers=headers)
    assert r.status_code == 400 and "AGENDA_TOKEN" in r.json()["detail"]

    await client.post("/api/v1/secrets", headers=headers, json={"key": "AGENDA_TOKEN", "value": "tok-1"})
    FakeAsyncClient.handler = lambda url, payload, headers: _mcp_response(
        payload, headers, lambda request: _rpc_result({"tools": CALENDAR_TOOLS}, req_id=request["id"])
    )
    r = await client.post(f"/api/v1/mcp/{server['id']}/test", headers=headers)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and [t["name"] for t in body["tools"]] == ["create_event"]

    # servidor fora do ar → 502
    FakeAsyncClient.handler = lambda url, payload, headers: FakeResponse({}, status_code=502)
    r = await client.post(f"/api/v1/mcp/{server['id']}/test", headers=headers)
    assert r.status_code == 502

    r = await client.delete(f"/api/v1/mcp/{server['id']}", headers=headers)
    assert r.status_code == 204
    r = await client.get("/api/v1/mcp", headers=headers)
    assert r.json() == []


async def test_mcp_test_connection_with_url_placeholder(client, auth_headers, fake_http):
    headers, _uid = auth_headers
    r = await client.post("/api/v1/mcp", headers=headers, json={
        "name": "Multi", "server_url": "https://mcp.exemplo.com/${TENANT}/mcp"})
    assert r.status_code == 201, r.text
    sid = r.json()["id"]

    r = await client.post(f"/api/v1/mcp/{sid}/test", headers=headers)
    assert r.status_code == 400 and "TENANT" in r.json()["detail"]

    await client.post("/api/v1/secrets", headers=headers, json={"key": "TENANT", "value": "acme"})
    seen = {}

    def handler(url, payload, headers):
        def dispatch(request):
            seen["url"] = url
            return _rpc_result({"tools": []}, req_id=request["id"])
        return _mcp_response(payload, headers, dispatch)

    FakeAsyncClient.handler = handler
    r = await client.post(f"/api/v1/mcp/{sid}/test", headers=headers)
    assert r.status_code == 200, r.text
    assert seen["url"] == "https://mcp.exemplo.com/acme/mcp"
