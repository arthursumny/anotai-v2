"""End-to-end tests for dynamic AI tool calling over the generic Nango proxy.

Covers the three layers together: the orchestrator offers the LLM ONLY the tools
that are (a) enabled by the template and (b) backed by an active user
connection, then executes the returned ``tool_calls`` through
``tool_registry`` handlers, which must hit
``NangoClient.proxy_request(method, provider_config_key, connection_id,
endpoint, ...)`` with the user's own ``connection_id`` and config key.
"""

import json
import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlmodel import select

import app.services.note_pipeline as note_pipeline
import app.services.trigger_executor as trigger_executor
from app.core.config import settings
from app.models.action_execution import ActionExecution
from app.models.integration_connection import IntegrationConnection
from app.models.note import Note, NoteStatus
from app.models.template import Template
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMembership
from app.schemas.template import ActionConfig
from app.services.tool_registry import TOOL_REGISTRY, llm_tools, notion_page_tool
from app.services.trigger_executor import execute_template_tools
from tests.automation_helpers import persist_generation


class _RecordingNango:
    """Fake NangoClient: records every proxy call instead of doing HTTP."""

    def __init__(self, *, api_key=None, base_url=None):
        self.api_key = api_key
        self.base_url = base_url
        self.calls = []

    async def proxy_request(
        self, method, provider_config_key, connection_id, endpoint, params=None, json=None,
    ):
        self.calls.append({
            "method": method,
            "provider_config_key": provider_config_key,
            "connection_id": connection_id,
            "endpoint": endpoint,
            "params": params,
            "json": json,
        })
        return SimpleNamespace(status_code=200, body={"id": "provider_id_1"})


def _patch_nango(monkeypatch):
    """Install a recording Nango client and return it."""
    recording = _RecordingNango()

    def factory(**kwargs):
        recording.api_key = kwargs.get("api_key")
        recording.base_url = kwargs.get("base_url")
        return recording

    monkeypatch.setattr(trigger_executor, "NangoClient", factory)
    return recording


def _never_called(*_args, **_kwargs):
    raise AssertionError("a LLM não deveria ser chamada sem ferramentas disponíveis")


async def _scenario(db_session, *, actions, providers=()):
    """Persist a user, an owned template and the given active connections."""
    workspace = Workspace(name="Tool calling workspace", timezone="UTC")
    user = User(
        name="Tool owner",
        email=f"tools-{uuid.uuid4().hex[:8]}@example.test",
        hashed_password="unused",
        workspace_id=workspace.id,
    )
    db_session.add_all([workspace, user])
    await db_session.flush()
    db_session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="OWNER"))
    template = Template(
        user_id=user.id,
        title="Conversas do Dia",
        purpose="Registrar conversas e combinados",
        semantic_trigger="conversa do dia",
        actions=actions,
        output_schema={"type": "object"},
    )
    db_session.add(template)
    for provider in providers:
        db_session.add(IntegrationConnection(
            workspace_id=workspace.id,
            user_id=user.id,
            provider=provider,
            provider_config_key=f"{provider}-config-key",
            nango_connection_id=f"nango_conn_{provider}",
            status="active",
        ))
    await db_session.commit()
    return user, template


def _integration_action(provider, *, enabled=True):
    """Canonical template action enabling a dynamic integration tool."""
    return {"type": "integration", "enabled": enabled, "config": {"provider": provider}}


def _tool_call(name, arguments):
    """OpenAI-shaped tool call object (name + JSON-string arguments)."""
    return SimpleNamespace(function=SimpleNamespace(name=name, arguments=json.dumps(arguments)))


async def test_llm_receives_only_enabled_and_connected_tools(db_session, monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(settings, "NANGO_ENV_KEY", "nango-env-secret")
    monkeypatch.setattr(settings, "NANGO_BASE_URL", "https://nango.example.test")
    # google_tasks: habilitado + conectado; notion: habilitado, sem conexão;
    # google_drive: desabilitado; google_calendar: conectado, fora do template.
    user, template = await _scenario(
        db_session,
        actions=[
            _integration_action("google_tasks"),
            _integration_action("notion"),
            _integration_action("google_drive", enabled=False),
        ],
        providers=("google_tasks", "google_calendar", "google_drive"),
    )
    seen = {}

    async def fake_llm(messages, tools):
        seen["tools"] = tools
        seen["messages"] = messages
        return []

    results = await execute_template_tools(
        db_session,
        user_id=user.id,
        template_id=template.id,
        transcription="Hoje preciso recolher o lixo às 16h.",
        note_body="## Conversas do Dia\n- Recolher o lixo às 16h",
        llm_call=fake_llm,
    )

    offered = [entry["function"]["name"] for entry in seen["tools"]]
    assert offered == ["create_google_task"]
    assert seen["tools"][0]["function"]["parameters"] == TOOL_REGISTRY["create_google_task"].schema
    # A transcrição e a nota chegam ao modelo; nada de conexão ou credencial.
    assert "recolher o lixo" in seen["messages"][1]["content"]
    assert results == []


async def test_tool_calls_execute_against_the_users_connection(db_session, monkeypatch):
    """Fluxo completo: tool_calls mockados da LLM -> Nango proxy do usuário."""
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(settings, "NANGO_ENV_KEY", "nango-env-secret")
    monkeypatch.setattr(settings, "NANGO_BASE_URL", "https://nango.example.test")
    recording = _patch_nango(monkeypatch)
    user, template = await _scenario(
        db_session,
        actions=[_integration_action("google_tasks")],
        providers=("google_tasks",),
    )
    arguments = {
        "title": "Recolher o lixo",
        "notes": "Levar as caixas para a calçada",
        "due": "2026-10-14T16:00:00-03:00",
    }

    async def fake_llm(_messages, _tools):
        return [_tool_call("create_google_task", arguments)]

    results = await execute_template_tools(
        db_session,
        user_id=user.id,
        template_id=template.id,
        transcription="Preciso recolher o lixo às 16h hoje.",
        note_body="## Conversas do Dia\n- Recolher o lixo às 16h",
        llm_call=fake_llm,
    )

    assert len(recording.calls) == 1
    call = recording.calls[0]
    assert call["method"] == "POST"
    assert call["provider_config_key"] == "google_tasks-config-key"
    assert call["connection_id"] == "nango_conn_google_tasks"
    # Lista padrão (@default): nunca a variante /users/@me.
    assert call["endpoint"] == "tasks/v1/lists/@default/tasks"
    assert "@me" not in call["endpoint"]
    assert call["json"] == arguments
    assert call["params"] is None
    assert recording.api_key == "nango-env-secret"
    assert recording.base_url == "https://nango.example.test"
    assert results == [
        trigger_executor.ToolCallResult(
            tool="create_google_task", status="success", detail="create_google_task:provider_id_1",
        ),
    ]


async def test_tool_not_offered_to_the_llm_is_never_executed(db_session, monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(settings, "NANGO_ENV_KEY", "nango-env-secret")
    recording = _patch_nango(monkeypatch)
    # O template habilita google_tasks (usuário conectado) e notion (sem conexão).
    user, template = await _scenario(
        db_session,
        actions=[_integration_action("google_tasks"), _integration_action("notion")],
        providers=("google_tasks",),
    )

    async def fake_llm(_messages, _tools):
        return [
            _tool_call("create_notion_page", {"title": "Conversas", "content": "corpo"}),
            _tool_call("append_google_sheet_row", {"spreadsheet_id": "abc", "values": ["x"]}),
        ]

    results = await execute_template_tools(
        db_session,
        user_id=user.id,
        template_id=template.id,
        transcription="Salvar a conversa em algum lugar.",
        note_body="corpo",
        llm_call=fake_llm,
    )

    assert recording.calls == []
    assert [(item.tool, item.status) for item in results] == [
        ("create_notion_page", "error"),
        ("append_google_sheet_row", "error"),
    ]
    assert all(item.detail == "tool não disponível" for item in results)


async def test_one_failing_tool_does_not_abort_the_others(db_session, monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(settings, "NANGO_ENV_KEY", "nango-env-secret")
    monkeypatch.setattr(settings, "NANGO_NOTION_DEFAULT_DATABASE_ID", "db_123")
    recording = _patch_nango(monkeypatch)
    user, template = await _scenario(
        db_session,
        actions=[_integration_action("google_tasks"), _integration_action("notion")],
        providers=("google_tasks", "notion"),
    )
    original = recording.proxy_request

    async def flaky(method, provider_config_key, connection_id, endpoint, params=None, json=None):
        if endpoint == "v1/pages":
            from app.services.nango_client import NangoClientError
            raise NangoClientError(502, "http_error")
        return await original(method, provider_config_key, connection_id, endpoint, params=params, json=json)

    recording.proxy_request = flaky

    async def fake_llm(_messages, _tools):
        return [
            _tool_call("create_notion_page", {"title": "Conversas", "content": "corpo"}),
            _tool_call("create_google_task", {"title": "Recolher o lixo", "due": "2026-10-14T16:00:00-03:00"}),
        ]

    results = await execute_template_tools(
        db_session,
        user_id=user.id,
        template_id=template.id,
        transcription="Recolher o lixo às 16h e salvar a conversa no Notion.",
        note_body="corpo",
        llm_call=fake_llm,
    )

    assert [(item.tool, item.status) for item in results] == [
        ("create_notion_page", "error"),
        ("create_google_task", "success"),
    ]
    assert results[0].detail == "Nango HTTP 502 (http_error)"
    # A tarefa foi criada com o connection_id da conexão google_tasks do usuário.
    assert recording.calls[-1]["connection_id"] == "nango_conn_google_tasks"
    assert recording.calls[-1]["endpoint"] == "tasks/v1/lists/@default/tasks"


async def test_malformed_tool_arguments_are_isolated(db_session, monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(settings, "NANGO_ENV_KEY", "nango-env-secret")
    recording = _patch_nango(monkeypatch)
    user, template = await _scenario(
        db_session,
        actions=[_integration_action("google_tasks")],
        providers=("google_tasks",),
    )

    async def fake_llm(_messages, _tools):
        # Primeira: JSON malformado de verdade. Segunda: JSON válido, mas não
        # é um objeto de argumentos.
        return [
            SimpleNamespace(function=SimpleNamespace(name="create_google_task", arguments="{não json}")),
            SimpleNamespace(function=SimpleNamespace(name="create_google_task", arguments="[1, 2]")),
        ]

    results = await execute_template_tools(
        db_session,
        user_id=user.id,
        template_id=template.id,
        transcription="Recolher o lixo às 16h.",
        note_body="corpo",
        llm_call=fake_llm,
    )

    assert [(item.tool, item.status, item.detail) for item in results] == [
        ("create_google_task", "error", "argumentos_json_invalidos"),
        ("create_google_task", "error", "argumentos_invalidos"),
    ]
    assert recording.calls == []


@pytest.mark.parametrize(
    ("actions", "providers", "expected_tools"),
    [
        ([], ("google_tasks",), []),                                    # template sem integrações
        ([_integration_action("google_tasks", enabled=False)], ("google_tasks",), []),  # desabilitada
        ([_integration_action("google_tasks")], (), []),                # sem conexão ativa
        ([_integration_action("google_tasks")], ("google_calendar",), []),  # conexão de outro provedor
    ],
)
async def test_no_candidate_tools_never_calls_the_llm(db_session, actions, providers, expected_tools):
    user, template = await _scenario(db_session, actions=actions, providers=providers)
    results = await execute_template_tools(
        db_session,
        user_id=user.id,
        template_id=template.id,
        transcription="Recolher o lixo às 16h.",
        note_body="corpo",
        llm_call=_never_called,
    )
    assert results == expected_tools


async def test_missing_template_or_blank_transcription_short_circuits(db_session):
    user, template = await _scenario(
        db_session, actions=[_integration_action("google_tasks")], providers=("google_tasks",),
    )
    assert await execute_template_tools(
        db_session, user_id=user.id, template_id=None,
        transcription="algo", note_body="corpo", llm_call=_never_called,
    ) == []
    assert await execute_template_tools(
        db_session, user_id=user.id, template_id=template.id,
        transcription="   \n ", note_body="corpo", llm_call=_never_called,
    ) == []


async def test_default_llm_call_is_fail_closed_without_openai_key(db_session, monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "")
    user, template = await _scenario(
        db_session, actions=[_integration_action("google_tasks")], providers=("google_tasks",),
    )
    assert await execute_template_tools(
        db_session, user_id=user.id, template_id=template.id,
        transcription="Recolher o lixo às 16h.", note_body="corpo",
    ) == []


async def test_google_task_handler_posts_to_default_list():
    tool = TOOL_REGISTRY["create_google_task"]
    recording = _RecordingNango()
    result = await tool.handler(
        recording,
        "conn_1",
        provider_config_key="google-tasks",
        title="Recolher o lixo",
        notes="Levar as caixas",
        due="2026-10-14T16:00:00-03:00",
    )
    assert recording.calls == [{
        "method": "POST",
        "provider_config_key": "google-tasks",
        "connection_id": "conn_1",
        "endpoint": "tasks/v1/lists/@default/tasks",
        "params": None,
        "json": {
            "title": "Recolher o lixo",
            "notes": "Levar as caixas",
            "due": "2026-10-14T16:00:00-03:00",
        },
    }]
    assert result == {"id": "provider_id_1"}


async def test_google_task_handler_omits_absent_optional_fields():
    tool = TOOL_REGISTRY["create_google_task"]
    recording = _RecordingNango()
    await tool.handler(
        recording, "conn_1", provider_config_key="google-tasks", title="Ligar para Ana",
    )
    assert recording.calls[0]["json"] == {"title": "Ligar para Ana"}


async def test_google_drive_file_handler_posts_metadata():
    tool = TOOL_REGISTRY["create_google_drive_file"]
    recording = _RecordingNango()
    result = await tool.handler(
        recording, "conn_1", provider_config_key="google-drive",
        name="Conversas do Dia.md", description="resumo",
    )
    assert recording.calls == [{
        "method": "POST",
        "provider_config_key": "google-drive",
        "connection_id": "conn_1",
        "endpoint": "drive/v3/files",
        "params": None,
        "json": {
            "name": "Conversas do Dia.md",
            "mimeType": "text/markdown",
            "description": "resumo",
        },
    }]
    assert result == {"id": "provider_id_1"}


async def test_google_sheets_handler_appends_row_with_query_params():
    tool = TOOL_REGISTRY["append_google_sheet_row"]
    recording = _RecordingNango()
    await tool.handler(
        recording, "conn_1", provider_config_key="google-sheets",
        spreadsheet_id="1AbC-xyz_9", values=["2026-10-14", "lixo", 3, True],
    )
    assert recording.calls == [{
        "method": "POST",
        "provider_config_key": "google-sheets",
        "connection_id": "conn_1",
        "endpoint": "sheets/v4/spreadsheets/1AbC-xyz_9/values/Sheet1!A1:append",
        "params": {"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"},
        "json": {"range": "Sheet1!A1", "majorDimension": "ROWS", "values": [["2026-10-14", "lixo", 3, True]]},
    }]


async def test_google_sheets_handler_honors_custom_sheet_name():
    tool = TOOL_REGISTRY["append_google_sheet_row"]
    recording = _RecordingNango()
    await tool.handler(
        recording, "conn_1", provider_config_key="google-sheets",
        spreadsheet_id="sheet-1", sheet_name="Outubro 2026", values=["ok"],
    )
    call = recording.calls[0]
    assert call["endpoint"] == "sheets/v4/spreadsheets/sheet-1/values/Outubro 2026!A1:append"
    assert call["json"]["range"] == "Outubro 2026!A1"


@pytest.mark.parametrize(
    "bad_argument",
    ["../connections", "a/b", "x?y=1", "spreadsheets/1"],
)
async def test_google_sheets_handler_blocks_url_injection(bad_argument):
    tool = TOOL_REGISTRY["append_google_sheet_row"]
    recording = _RecordingNango()
    with pytest.raises(ValueError, match="spreadsheet_id|sheet_name"):
        await tool.handler(
            recording, "conn_1", provider_config_key="google-sheets",
            spreadsheet_id="sheet-1", sheet_name=bad_argument, values=["ok"],
        )
    assert recording.calls == []


async def test_google_sheets_handler_rejects_bad_spreadsheet_id():
    tool = TOOL_REGISTRY["append_google_sheet_row"]
    recording = _RecordingNango()
    with pytest.raises(ValueError, match="spreadsheet_id"):
        await tool.handler(
            recording, "conn_1", provider_config_key="google-sheets",
            spreadsheet_id="../..", values=["ok"],
        )
    assert recording.calls == []


async def test_notion_handler_uses_server_configured_database(monkeypatch):
    monkeypatch.setattr(settings, "NANGO_NOTION_DEFAULT_DATABASE_ID", "db_server_123")
    tool = TOOL_REGISTRY["create_notion_page"]
    recording = _RecordingNango()
    result = await tool.handler(
        recording, "conn_1", provider_config_key="notion",
        title="Conversas do Dia", content="Resumo da conversa",
    )
    assert recording.calls[0]["endpoint"] == "v1/pages"
    assert recording.calls[0]["method"] == "POST"
    assert recording.calls[0]["connection_id"] == "conn_1"
    assert recording.calls[0]["provider_config_key"] == "notion"
    payload = recording.calls[0]["json"]
    assert payload["parent"] == {"database_id": "db_server_123"}
    assert payload["properties"]["Name"]["title"][0]["text"]["content"] == "Conversas do Dia"
    assert payload["children"][0]["paragraph"]["rich_text"][0]["text"]["content"] == "Resumo da conversa"
    assert result == {"id": "provider_id_1"}


async def test_notion_handler_fails_closed_without_configured_database(monkeypatch):
    monkeypatch.setattr(settings, "NANGO_NOTION_DEFAULT_DATABASE_ID", "")
    tool = TOOL_REGISTRY["create_notion_page"]
    recording = _RecordingNango()
    with pytest.raises(ValueError, match="Notion database não configurado"):
        await tool.handler(
            recording, "conn_1", provider_config_key="notion", title="Conversas", content="corpo",
        )
    assert recording.calls == []


async def test_notion_factory_binds_database_per_template():
    tool = notion_page_tool("db_template_9", title_property="Nome")
    recording = _RecordingNango()
    await tool.handler(
        recording, "conn_1", provider_config_key="notion", title="Conversas",
    )
    payload = recording.calls[0]["json"]
    assert payload["parent"] == {"database_id": "db_template_9"}
    assert "children" not in payload
    assert "Nome" in payload["properties"]


def test_llm_tools_serializes_registry_to_openai_format():
    serialized = llm_tools([TOOL_REGISTRY["create_google_task"]])
    assert serialized == [{
        "type": "function",
        "function": {
            "name": "create_google_task",
            "description": TOOL_REGISTRY["create_google_task"].description,
            "parameters": TOOL_REGISTRY["create_google_task"].schema,
        },
    }]
    assert json.dumps(serialized)  # serializável para a chamada da LLM


def test_integration_action_config_validates_provider():
    accepted = ActionConfig.model_validate(_integration_action("google_tasks"))
    assert accepted.enabled is True
    for invalid in (
        {"type": "integration", "enabled": True, "config": {}},
        {"type": "integration", "enabled": True, "config": {"provider": "Google Tasks"}},
        {"type": "integration", "enabled": True, "config": {"provider": "../etc/passwd"}},
        {"type": "integration", "enabled": True, "config": {"provider": "a" * 65}},
    ):
        with pytest.raises(ValidationError):
            ActionConfig.model_validate(invalid)


async def _persisted_generation(db_session):
    """Persist a processed note + owned template with an integration action."""
    note = Note(
        user_id=uuid.uuid4(), title="Conversas", body="corpo", summary="resumo",
        status=NoteStatus.PROCESSED, raw_transcription="Recolher o lixo às 16h.",
        structured_data={"combinado": "lixo"},
    )
    template = Template(
        user_id=note.user_id, title="Conversas do Dia", semantic_trigger="conversa",
        actions=[_integration_action("google_tasks")],
        output_schema={"type": "object"},
    )
    user, document = await persist_generation(db_session, note, template)
    return note, template, user, document


async def test_static_action_ledger_skips_integration_entries(db_session):
    """Entradas de integração não viram ActionExecution do ledger estático."""
    note, _template, user, _document = await _persisted_generation(db_session)
    from app.services.template_actions import prepare_template_actions

    rows = await prepare_template_actions(db_session, note, user.id)
    await db_session.commit()
    assert rows == []
    assert list((await db_session.exec(select(ActionExecution))).all()) == []


async def test_pipeline_runs_tool_calling_after_note_generation(db_session, monkeypatch):
    """O wire-in do pós-processamento chama o orquestrador ao final do fluxo."""
    note, template, user, _document = await _persisted_generation(db_session)
    seen = {}

    async def fake_executor(session, **kwargs):
        seen.update(kwargs)
        seen["user_id"] = kwargs.get("user_id")
        return []

    monkeypatch.setattr(note_pipeline, "execute_template_tools", fake_executor)
    await note_pipeline.process_transcription(db_session, note)
    assert seen["user_id"] == user.id
    assert seen["template_id"] == template.id
    assert seen["transcription"] == "Recolher o lixo às 16h."


async def test_tool_calling_failure_never_breaks_the_note_flow(db_session, monkeypatch):
    """Falha de integração é isolada: a nota/documento já persistiram."""
    note, _template, _user, _document = await _persisted_generation(db_session)

    async def exploding(session, **kwargs):
        raise RuntimeError("nango indisponível")

    monkeypatch.setattr(note_pipeline, "execute_template_tools", exploding)
    await note_pipeline.process_transcription(db_session, note)
    saved = await db_session.get(Note, note.id)
    assert saved.status == NoteStatus.PROCESSED
    assert saved.body == "corpo"
