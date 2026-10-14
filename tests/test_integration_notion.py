"""CREATE_NOTION_PAGE creates a page in the user's own Notion database via Nango.

No real Notion/Nango call ever happens: ``NangoClient`` is stubbed exactly like
the Slack/Gmail provider tests, and every assertion inspects the captured
in-process call instead of touching the wire. The destination ``database_id`` and
the property-name mapping are proven to come from the template config, never
from note/LLM content.
"""

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession

import app.services.integration_service as integration_service
from app.core.config import settings
from app.models.integration_connection import IntegrationConnection
from app.models.note import Note, NoteStatus
from app.models.template import Template
from app.services.integration_service import IntegrationExecutionError, IntegrationService
from app.services.nango_client import NangoClientError
from app.services.action_executor import run_action_execution
from app.services.template_actions import prepare_template_actions
from tests.automation_helpers import persist_generation


@pytest.fixture
def nango(monkeypatch):
    """In-process Nango stub: the real Notion/Nango API is never reached."""
    captured = {
        "calls": [],
        "error": None,
        "response": {"id": "b0f1c2d3e4f5a6b7c8d9e0f1a2b3c4d5"},
    }
    monkeypatch.setattr(settings, "NANGO_ENV_KEY", "nango-env-secret")
    monkeypatch.setattr(settings, "NANGO_BASE_URL", "https://nango.example.test")
    monkeypatch.setattr(settings, "NANGO_NOTION_PROVIDER_CONFIG_KEY", "notion-test-key")

    class FakeNangoClient:
        def __init__(self, *, api_key, base_url):
            captured["api_key"] = api_key
            captured["base_url"] = base_url

        async def proxy_request(self, method, provider_config_key, connection_id, endpoint, params=None, json=None):
            captured["calls"].append({
                "method": method,
                "endpoint": endpoint,
                "connection_id": connection_id,
                "provider_config_key": provider_config_key,
                "params": params,
                "json": json,
            })
            if captured["error"] is not None:
                raise captured["error"]
            return SimpleNamespace(status_code=200, body=dict(captured["response"]))

    monkeypatch.setattr(integration_service, "NangoClient", FakeNangoClient)
    return captured


def _factory(session):
    return async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False)


async def _notion_generation(session, config, *, title="Resumo da reunião", summary="Combinado sexta"):
    """Persist a real generation whose selected template configures a Notion action."""
    note = Note(
        user_id=uuid.uuid4(), title=title, body="Corpo gerado", summary=summary,
        status=NoteStatus.PROCESSED, raw_transcription="Áudio bruto",
        structured_data={"client": {"name": "Ana"}},
    )
    template = Template(
        user_id=note.user_id, title=title, semantic_trigger=title,
        actions=[{"type": "notion", "enabled": True, "config": config}],
        output_schema={"type": "object"},
    )
    user, document = await persist_generation(session, note, template)
    return note, template, user, document


async def _connect_notion(session, user, *, index=1, status="active"):
    session.add(IntegrationConnection(
        workspace_id=user.workspace_id,
        user_id=user.id,
        provider="notion",
        provider_config_key="notion-test-key",
        nango_connection_id=f"nango_notion_{index}",
        status=status,
    ))
    await session.flush()


async def _claimed_notion(session, *, config, connections=1, connection_status="active",
                          title="Resumo da reunião"):
    """Prepare a template-origin ledger row and mark it claimed for execution."""
    note, _template, user, _document = await _notion_generation(session, config, title=title)
    for index in range(connections):
        await _connect_notion(session, user, index=index + 1, status=connection_status)
    rows = await prepare_template_actions(session, note, user.id)
    execution = rows[0]
    execution.status = "claimed"
    await session.commit()
    return note, user, execution


_DATABASE_CONFIG = {
    "database_id": "d1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6",
    "title_property": "Name",
    "properties": {
        "Resumo": {"rich_text": [{"text": {"content": "{summary}"}}]},
    },
}


async def test_create_notion_page_posts_to_the_template_database(db_session, nango):
    # Given a claimed CREATE_NOTION_PAGE with an active Notion connection.
    _note, _user, execution = await _claimed_notion(db_session, config=_DATABASE_CONFIG)

    # When the claimed row executes.
    result = await IntegrationService().execute_claimed(db_session, execution.id)

    # Then it proxies v1/pages through the owned connection, into the configured
    # database, with the title property plus the rendered template properties.
    assert result == {"entity": "notion_page", "id": "b0f1c2d3e4f5a6b7c8d9e0f1a2b3c4d5"}
    assert len(nango["calls"]) == 1
    call = nango["calls"][0]
    assert nango["api_key"] == "nango-env-secret"
    assert nango["base_url"] == "https://nango.example.test"
    assert call["method"] == "POST"
    assert call["endpoint"] == "v1/pages"
    assert call["connection_id"] == "nango_notion_1"
    assert call["provider_config_key"] == "notion-test-key"
    assert call["json"] == {
        "parent": {"database_id": "d1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"},
        "properties": {
            "Name": {"title": [{"text": {"content": "Resumo da reunião"}}]},
            "Resumo": {"rich_text": [{"text": {"content": "Combinado sexta"}}]},
        },
    }


async def test_note_content_cannot_redirect_the_database(db_session, nango):
    # Given a note whose generated content merely looks like a database id.
    _note, _user, execution = await _claimed_notion(
        db_session,
        config={**_DATABASE_CONFIG, "database_id": "d0SAFE0001"},
        title="d0EVIL0099",
    )

    result = await IntegrationService().execute_claimed(db_session, execution.id)

    # Then the destination is still the template's database, and the note-derived
    # title only ever lands inside the title PROPERTY VALUE.
    assert result["entity"] == "notion_page"
    call = nango["calls"][0]
    assert call["json"]["parent"]["database_id"] == "d0SAFE0001"
    assert call["json"]["properties"]["Name"]["title"][0]["text"]["content"] == "d0EVIL0099"
    assert "d0EVIL0099" not in str(call["json"]["parent"])


async def test_missing_provider_config_key_fails_without_sending(db_session, nango, monkeypatch):
    monkeypatch.setattr(settings, "NANGO_NOTION_PROVIDER_CONFIG_KEY", "")
    _note, _user, execution = await _claimed_notion(db_session, config=_DATABASE_CONFIG)

    with pytest.raises(IntegrationExecutionError, match="Notion não está configurado"):
        await IntegrationService().execute_claimed(db_session, execution.id)
    assert nango["calls"] == []


async def test_no_active_notion_connection_fails_explicitly(db_session, nango):
    _note, _user, execution = await _claimed_notion(db_session, config=_DATABASE_CONFIG, connections=0)

    with pytest.raises(IntegrationExecutionError, match="Conecte seu Notion"):
        await IntegrationService().execute_claimed(db_session, execution.id)
    assert nango["calls"] == []


async def test_ambiguous_connections_are_not_guessed(db_session, nango, monkeypatch):
    # The unique constraint normally forbids two rows per owner+provider, so the
    # ambiguity is injected at the lookup boundary to exercise the strict guard.
    _note, _user, execution = await _claimed_notion(
        db_session, config=_DATABASE_CONFIG, connections=1,
    )
    real_exec = db_session.exec

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

        def first(self):
            return self._rows[0] if self._rows else None

    injected = [
        SimpleNamespace(nango_connection_id="nango_notion_1", provider_config_key="notion-test-key"),
        SimpleNamespace(nango_connection_id="nango_notion_2", provider_config_key="notion-test-key"),
    ]

    async def fake_exec(statement, *args, **kwargs):
        descriptions = getattr(statement, "column_descriptions", None)
        if descriptions and descriptions[0].get("entity") is IntegrationConnection:
            return _Result(injected)
        return await real_exec(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "exec", fake_exec)

    with pytest.raises(IntegrationExecutionError, match="Conecte seu Notion"):
        await IntegrationService().execute_claimed(db_session, execution.id)
    assert nango["calls"] == []


async def test_inactive_connection_is_not_enough(db_session, nango):
    _note, _user, execution = await _claimed_notion(
        db_session, config=_DATABASE_CONFIG, connection_status="disconnected",
    )

    with pytest.raises(IntegrationExecutionError, match="Conecte seu Notion"):
        await IntegrationService().execute_claimed(db_session, execution.id)
    assert nango["calls"] == []


async def test_proxy_failure_is_sanitized_and_retryable(db_session, nango):
    note, template, user, _document = await _notion_generation(db_session, _DATABASE_CONFIG)
    await _connect_notion(db_session, user)
    rows = await prepare_template_actions(db_session, note, user.id)
    await db_session.commit()
    nango["error"] = NangoClientError(502, "http_error")

    # When the executor handles the provider failure in its own transaction.
    result = await run_action_execution(_factory(db_session), rows[0].id)

    # Then the ledger records a sanitized retryable failure, never a success.
    assert result == "retry_scheduled"
    # The proxy WAS attempted (once) and refused; nothing was silently dropped.
    assert len(nango["calls"]) == 1
    assert nango["calls"][0]["endpoint"] == "v1/pages"
    await db_session.refresh(rows[0])
    assert rows[0].last_error == "integrationexecutionerror"
    assert "http_error" not in (rows[0].last_error or "")
    assert rows[0].attempts == 1


async def test_malformed_page_id_is_rejected(db_session, nango):
    nango["response"] = {"id": "not a valid id!"}
    _note, _user, execution = await _claimed_notion(db_session, config=_DATABASE_CONFIG)

    with pytest.raises(IntegrationExecutionError, match="página inválida"):
        await IntegrationService().execute_claimed(db_session, execution.id)


async def test_config_without_database_id_fails_validation(db_session, nango):
    # A template Notion action with no destination must fail at prepare time,
    # not silently create a page somewhere unexpected.
    note, template, user, _document = await _notion_generation(
        db_session, {"title": "{title}", "properties": {}},
    )
    template.actions = [{"type": "notion", "enabled": True, "config": {"title": "{title}"}}]
    db_session.add(template)
    await db_session.flush()

    rows = await prepare_template_actions(db_session, note, user.id)

    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert nango["calls"] == []


async def test_property_placeholder_missing_fails_without_sending(db_session, nango):
    # A placeholder that does not exist in the generated document is an error,
    # not a silent empty value written into the user's Notion.
    note, template, user, _document = await _notion_generation(
        db_session,
        {**_DATABASE_CONFIG, "properties": {"X": {"rich_text": [{"text": {"content": "{nope}"}}]}}},
    )
    template.actions = [{"type": "notion", "enabled": True, "config": {
        "database_id": _DATABASE_CONFIG["database_id"],
        "properties": {"X": {"rich_text": [{"text": {"content": "{nope}"}}]}},
    }}]
    db_session.add(template)
    await db_session.flush()

    rows = await prepare_template_actions(db_session, note, user.id)

    assert len(rows) == 1
    assert rows[0].status == "failed"
    assert nango["calls"] == []
