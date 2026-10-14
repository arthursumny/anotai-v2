"""SEND_SLACK_MESSAGE posts through the user's own Slack connection via Nango.

No real Slack/Nango call ever happens: ``NangoClient`` is stubbed exactly like
the Gmail/Calendar provider tests, and every assertion inspects the captured
in-process call instead of touching the wire. The destination channel is proven
to come from the template config, never from note/LLM content.
"""

import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession

import app.services.integration_service as integration_service
from app.core.config import settings
from app.models.integration_connection import IntegrationConnection
from app.models.note import Note, NoteStatus
from app.models.template import Template
from app.schemas.template import ActionConfig
from app.schemas.workflow_action import ActionValidationError, SendSlackMessageAction
from app.services.action_registry import validate_action
from app.services.action_executor import run_action_execution
from app.services.integration_service import IntegrationExecutionError, IntegrationService
from app.services.nango_client import NangoClientError
from app.services.template_actions import prepare_template_actions
from tests.automation_helpers import persist_generation


@pytest.fixture
def nango(monkeypatch):
    """In-process Nango stub: the real Slack/Nango API is never reached."""
    captured = {
        "calls": [],
        "error": None,
        "response": {"ok": True, "ts": "1700000000.000100", "channel": "C0123456789"},
    }
    monkeypatch.setattr(settings, "NANGO_ENV_KEY", "nango-env-secret")
    monkeypatch.setattr(settings, "NANGO_BASE_URL", "https://nango.example.test")
    monkeypatch.setattr(settings, "NANGO_SLACK_PROVIDER_CONFIG_KEY", "slack-test-key")

    class FakeNangoClient:
        def __init__(self, *, api_key, base_url):
            captured["api_key"] = api_key
            captured["base_url"] = base_url

        async def proxy_request(self, method, path, **kwargs):
            captured["calls"].append({"method": method, "path": path, **kwargs})
            if captured["error"] is not None:
                raise captured["error"]
            return SimpleNamespace(status_code=200, body=dict(captured["response"]))

    monkeypatch.setattr(integration_service, "NangoClient", FakeNangoClient)
    return captured


def _factory(session):
    return async_sessionmaker(session.bind, class_=AsyncSession, expire_on_commit=False)


async def _slack_generation(session, config, *, title="Status do projeto", summary="Tudo certo"):
    """Persist a real generation whose selected template configures a Slack action."""
    note = Note(
        user_id=uuid.uuid4(), title=title, body="Corpo gerado", summary=summary,
        status=NoteStatus.PROCESSED, raw_transcription="Áudio bruto",
        structured_data={"client": {"name": "Ana"}},
    )
    template = Template(
        user_id=note.user_id, title=title, semantic_trigger=title,
        actions=[{"type": "slack", "enabled": True, "config": config}],
        output_schema={"type": "object"},
    )
    user, document = await persist_generation(session, note, template)
    return note, template, user, document


async def _connect_slack(session, user, *, index=1, status="active"):
    session.add(IntegrationConnection(
        workspace_id=user.workspace_id,
        user_id=user.id,
        provider="slack",
        provider_config_key="slack-test-key",
        nango_connection_id=f"nango_slack_{index}",
        status=status,
    ))
    await session.flush()


async def _claimed_slack(session, *, config, connections=1, connection_status="active", title="Status do projeto"):
    """Prepare a template-origin ledger row and mark it claimed for execution."""
    note, _template, user, _document = await _slack_generation(session, config, title=title)
    for index in range(connections):
        await _connect_slack(session, user, index=index + 1, status=connection_status)
    rows = await prepare_template_actions(session, note, user.id)
    execution = rows[0]
    execution.status = "claimed"
    await session.commit()
    return note, user, execution


async def test_send_slack_message_posts_text_to_the_template_channel(db_session, nango):
    # Given a claimed SEND_SLACK_MESSAGE with an active Slack connection.
    _note, _user, execution = await _claimed_slack(
        db_session,
        config={"channel": "C0EXAMPLE123", "message": "{title}: {summary}"},
    )

    # When the claimed row executes.
    result = await IntegrationService().execute_claimed(db_session, execution.id)

    # Then it proxies chat.postMessage through the owned connection.
    assert result == {
        "entity": "slack_message", "ts": "1700000000.000100", "channel": "C0123456789",
    }
    assert len(nango["calls"]) == 1
    call = nango["calls"][0]
    assert nango["api_key"] == "nango-env-secret"
    assert nango["base_url"] == "https://nango.example.test"
    assert call["method"] == "POST"
    assert call["path"] == "chat.postMessage"
    assert call["connection_id"] == "nango_slack_1"
    assert call["provider_config_key"] == "slack-test-key"
    assert call["json_body"] == {
        "channel": "C0EXAMPLE123", "text": "Status do projeto: Tudo certo",
    }


async def test_note_content_cannot_redirect_the_channel(db_session, nango):
    # Given a note whose generated content merely looks like a channel id.
    _note, _user, execution = await _claimed_slack(
        db_session,
        config={"channel": "C0SAFE0001", "message": "{title}"},
        title="C0EVIL0099",
    )

    # When it executes, the configured channel is still the only destination.
    result = await IntegrationService().execute_claimed(db_session, execution.id)
    assert result["channel"] == "C0123456789"
    assert nango["calls"][0]["json_body"] == {"channel": "C0SAFE0001", "text": "C0EVIL0099"}


async def test_slack_ok_false_is_never_reported_as_success(db_session, nango):
    # Given Slack rejecting the post.
    _note, _user, execution = await _claimed_slack(
        db_session, config={"channel": "C0EXAMPLE123", "message": "{summary}"},
    )
    nango["response"] = {"ok": False, "error": "channel_not_found"}

    with pytest.raises(IntegrationExecutionError, match="Slack recusou a mensagem"):
        await IntegrationService().execute_claimed(db_session, execution.id)
    assert len(nango["calls"]) == 1


@pytest.mark.parametrize("missing", ["env_key", "provider_key"])
async def test_slack_fails_closed_when_nango_is_not_configured(
    db_session, nango, monkeypatch, missing,
):
    # Given the Nango environment or the Slack provider config key is unset.
    _note, _user, execution = await _claimed_slack(
        db_session, config={"channel": "C0EXAMPLE123", "message": "{summary}"},
    )
    monkeypatch.setattr(
        settings,
        "NANGO_ENV_KEY" if missing == "env_key" else "NANGO_SLACK_PROVIDER_CONFIG_KEY",
        "",
    )

    with pytest.raises(IntegrationExecutionError, match="Slack não está configurado"):
        await IntegrationService().execute_claimed(db_session, execution.id)
    assert nango["calls"] == []


@pytest.mark.parametrize("connection_kind", ["missing", "inactive"])
async def test_slack_requires_an_owned_active_connection(
    db_session, nango, connection_kind,
):
    # Given no usable active Slack connection for this execution's owner.
    _note, _user, execution = await _claimed_slack(
        db_session,
        config={"channel": "C0EXAMPLE123", "message": "{summary}"},
        connections=0 if connection_kind == "missing" else 1,
        connection_status="disconnected" if connection_kind == "inactive" else "active",
    )

    with pytest.raises(IntegrationExecutionError, match="Conecte seu Slack"):
        await IntegrationService().execute_claimed(db_session, execution.id)
    assert nango["calls"] == []


async def test_slack_rejects_ambiguous_active_connections(db_session, nango, monkeypatch):
    # The unique constraint normally forbids two rows per owner+provider, so the
    # ambiguity is injected at the lookup boundary to exercise the strict guard.
    _note, _user, execution = await _claimed_slack(
        db_session, config={"channel": "C0EXAMPLE123", "message": "{summary}"}, connections=1,
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
        SimpleNamespace(nango_connection_id="nango_slack_1", provider_config_key="slack-test-key"),
        SimpleNamespace(nango_connection_id="nango_slack_2", provider_config_key="slack-test-key"),
    ]

    async def fake_exec(statement, *args, **kwargs):
        descriptions = getattr(statement, "column_descriptions", None)
        if descriptions and descriptions[0].get("entity") is IntegrationConnection:
            return _Result(injected)
        return await real_exec(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "exec", fake_exec)

    with pytest.raises(IntegrationExecutionError, match="Conecte seu Slack"):
        await IntegrationService().execute_claimed(db_session, execution.id)
    assert nango["calls"] == []


@pytest.mark.parametrize("error", ["client_error", "value_error"])
async def test_slack_proxy_failure_is_sanitized(db_session, nango, error):
    _note, _user, execution = await _claimed_slack(
        db_session, config={"channel": "C0EXAMPLE123", "message": "{summary}"},
    )
    nango["error"] = (
        NangoClientError(502, "http_error") if error == "client_error"
        else ValueError("payload inválido")
    )

    with pytest.raises(IntegrationExecutionError, match="Slack request falhou") as exc_info:
        await IntegrationService().execute_claimed(db_session, execution.id)
    assert len(nango["calls"]) == 1
    assert "http_error" not in str(exc_info.value)


@pytest.mark.parametrize("response", [
    {"ok": True, "ts": "not-a-ts", "channel": "C0123456789"},
    {"ok": True, "ts": "1700000000.000100", "channel": "C0 EVIL/../"},
])
async def test_slack_rejects_malformed_success_responses(db_session, nango, response):
    _note, _user, execution = await _claimed_slack(
        db_session, config={"channel": "C0EXAMPLE123", "message": "{summary}"},
    )
    nango["response"] = response

    with pytest.raises(IntegrationExecutionError, match="Slack retornou mensagem inválida"):
        await IntegrationService().execute_claimed(db_session, execution.id)


async def test_slack_delivery_is_retried_identically(db_session, nango):
    # Given an at-least-once retry of the same claimed ledger row.
    _note, _user, execution = await _claimed_slack(
        db_session,
        config={"channel": "C0EXAMPLE123", "message": "{title}: {summary}"},
    )
    service = IntegrationService()

    first = await service.execute_claimed(db_session, execution.id)
    second = await service.execute_claimed(db_session, execution.id)

    # Then Slack posts are not deduped by us, but the payload stays identical.
    assert first == second
    assert len(nango["calls"]) == 2
    payloads = [call["json_body"] for call in nango["calls"]]
    assert payloads[0] == payloads[1] == {
        "channel": "C0EXAMPLE123", "text": "Status do projeto: Tudo certo",
    }


async def test_slack_proxy_failure_marks_a_retryable_failed_ledger_row(db_session, nango):
    # Given a pending ledger row whose Slack proxy call explodes.
    note, _template, user, _document = await _slack_generation(
        db_session, {"channel": "C0EXAMPLE123", "message": "{summary}"},
    )
    await _connect_slack(db_session, user)
    rows = await prepare_template_actions(db_session, note, user.id)
    await db_session.commit()
    nango["error"] = NangoClientError(502, "http_error")

    # When the executor handles the provider failure in its own transaction.
    result = await run_action_execution(_factory(db_session), rows[0].id)

    # Then the ledger records a sanitized retryable failure, never a success.
    assert result == "retry_scheduled"
    await db_session.refresh(rows[0])
    assert rows[0].last_error == "integrationexecutionerror"
    assert "http_error" not in (rows[0].last_error or "")


def test_slack_config_requires_a_channel_and_payload_cannot_inject_one():
    # A template Slack action without a channel is invalid at the API boundary.
    with pytest.raises(ValidationError):
        ActionConfig.model_validate({"type": "slack", "enabled": True, "config": {"message": "{summary}"}})
    # The action payload has no channel slot: injected channels are rejected.
    with pytest.raises(ActionValidationError):
        validate_action(
            {"type": "SEND_SLACK_MESSAGE", "text": "oi", "channel": "C0EVIL0099"},
            template_authorized=True,
        )
    action = validate_action({"type": "SEND_SLACK_MESSAGE", "text": "oi"}, template_authorized=True)
    assert isinstance(action, SendSlackMessageAction)
