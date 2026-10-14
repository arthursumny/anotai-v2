"""SEND_EMAIL sends through the user's own connected Gmail via the Nango proxy.

No real Gmail/Nango API call ever happens: ``NangoClient`` is stubbed exactly
like the Calendar/Telegram provider tests, and every assertion decodes the
in-process base64url payload instead of touching the wire.
"""

import base64
import hashlib
import re
import uuid
from email import policy
from email.parser import BytesParser
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession

import app.services.integration_service as integration_service
from app.core.config import settings
from app.models.action_execution import ActionExecution
from app.models.integration_connection import IntegrationConnection
from app.models.user import User
from app.models.workflow import Workflow, WorkflowExecution, WorkflowStep
from app.models.workspace import Workspace, WorkspaceMembership
from app.services.action_executor import run_action_execution
from app.services.integration_service import IntegrationExecutionError, IntegrationService
from app.services.nango_client import NangoClientError


@pytest.fixture
def nango(monkeypatch):
    """In-process Nango stub: the real Gmail/Nango API is never reached."""
    captured = {
        "calls": [],
        "error": None,
        "response": {"id": "gmail_msg_123", "threadId": "thread_42"},
    }
    monkeypatch.setattr(settings, "NANGO_ENV_KEY", "nango-env-secret")
    monkeypatch.setattr(settings, "NANGO_BASE_URL", "https://nango.example.test")
    monkeypatch.setattr(settings, "NANGO_GOOGLE_GMAIL_PROVIDER_CONFIG_KEY", "gmail-test-key")

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


async def _seed_email_action(
    session,
    *,
    action_input=None,
    status="claimed",
    connections=1,
    connection_status="active",
):
    workspace = Workspace(name="Email action workspace", timezone="UTC")
    user = User(
        name="Email owner",
        email=f"email-{uuid.uuid4().hex}@example.test",
        hashed_password="not-a-real-password",
        workspace_id=workspace.id,
    )
    session.add(workspace)
    session.add(user)
    await session.flush()
    session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="OWNER"))
    workflow = Workflow(
        workspace_id=workspace.id,
        user_id=user.id,
        title="Email flow",
        trigger_type="MANUAL",
        enabled=True,
    )
    session.add(workflow)
    await session.flush()
    step = WorkflowStep(
        workflow_id=workflow.id,
        position=0,
        action_type="SEND_EMAIL",
        input_template={},
        require_confirmation=False,
        enabled=True,
    )
    parent = WorkflowExecution(
        workspace_id=workspace.id,
        user_id=user.id,
        workflow_id=workflow.id,
        trigger_type="MANUAL",
        source_id="document-1",
        idempotency_key=f"run-{uuid.uuid4()}",
    )
    session.add_all([step, parent])
    await session.flush()
    execution = ActionExecution(
        workspace_id=workspace.id,
        user_id=user.id,
        workflow_execution_id=parent.id,
        workflow_step_id=step.id,
        trigger_type="MANUAL",
        source_id="document-1",
        action_type="SEND_EMAIL",
        idempotency_key=f"ledger-{uuid.uuid4()}",
        action_input=action_input if action_input is not None else {
            "subject": "Status update",
            "body": "Linha um\nLinha dois",
        },
        policy_snapshot={
            "authorized": True,
            "require_confirmation": False,
            "workflow_id": str(workflow.id),
        },
        workflow_version=workflow.version,
        status=status,
    )
    session.add(execution)
    await session.flush()
    for index in range(connections):
        session.add(IntegrationConnection(
            workspace_id=workspace.id,
            user_id=user.id,
            provider="google_gmail",
            provider_config_key="gmail-test-key",
            nango_connection_id=f"nango_conn_{index + 1}",
            status=connection_status,
        ))
    await session.flush()
    await session.commit()
    return workspace, user, execution


def _decode_mime(raw: str):
    """Decode Gmail's unpadded base64url envelope back into an RFC 2822 message."""
    assert isinstance(raw, str)
    assert re.fullmatch(r"[A-Za-z0-9_-]+", raw), "raw must be unpadded base64url"
    padded = raw + "=" * (-len(raw) % 4)
    return BytesParser(policy=policy.default).parsebytes(base64.urlsafe_b64decode(padded))


@pytest.mark.parametrize("explicit_to", [None, "dest@example.test"])
async def test_send_email_proxies_mime_through_owned_gmail_connection(
    db_session, nango, explicit_to,
):
    # Given a claimed SEND_EMAIL with an active Google Gmail connection.
    action_input = {"subject": "Reunião confirmada", "body": "Olá,\nTudo bem?"}
    if explicit_to is not None:
        action_input["to"] = explicit_to
    _workspace, user, execution = await _seed_email_action(db_session, action_input=action_input)

    # When the claimed row executes.
    result = await IntegrationService().execute_claimed(db_session, execution.id)

    # Then it proxies a base64url raw payload through the owned connection.
    assert result == {"entity": "email", "id": "gmail_msg_123", "thread_id": "thread_42"}
    assert len(nango["calls"]) == 1
    call = nango["calls"][0]
    assert nango["api_key"] == "nango-env-secret"
    assert nango["base_url"] == "https://nango.example.test"
    assert call["method"] == "POST"
    assert call["endpoint"] == "gmail/v1/users/me/messages/send"
    assert call["connection_id"] == "nango_conn_1"
    assert call["provider_config_key"] == "gmail-test-key"
    message = _decode_mime(call["json"]["raw"])
    assert message["To"] == (explicit_to or user.email)
    assert message["Subject"] == "Reunião confirmada"
    # The sender identity belongs to the connected account: never forged here.
    assert message.get("From") is None
    assert message.get_content().splitlines() == ["Olá,", "Tudo bem?"]
    assert re.fullmatch(r"<[0-9a-f]{64}@anotai>", message["Message-ID"])


@pytest.mark.parametrize("connection_kind", ["missing", "inactive", "foreign"])
async def test_send_email_requires_an_owned_active_gmail_connection(
    db_session, nango, connection_kind,
):
    # Given no usable Gmail connection for this execution's owner.
    workspace, _user, execution = await _seed_email_action(
        db_session,
        connections=0 if connection_kind in {"missing", "foreign"} else 1,
        connection_status="disconnected" if connection_kind == "inactive" else "active",
    )
    if connection_kind == "foreign":
        foreign = User(
            name="Foreign",
            email=f"foreign-{uuid.uuid4().hex}@example.test",
            hashed_password="not-a-real-password",
            workspace_id=workspace.id,
        )
        db_session.add(foreign)
        await db_session.flush()
        db_session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=foreign.id, role="MEMBER"))
        db_session.add(IntegrationConnection(
            workspace_id=workspace.id,
            user_id=foreign.id,
            provider="google_gmail",
            provider_config_key="gmail-test-key",
            nango_connection_id="foreign_conn_1",
            status="active",
        ))
        await db_session.commit()

    # When execution runs, it fails with an actionable error before any proxy call.
    with pytest.raises(IntegrationExecutionError, match="Conecte seu Gmail"):
        await IntegrationService().execute_claimed(db_session, execution.id)
    assert nango["calls"] == []


async def test_send_email_rejects_ambiguous_active_connections(db_session, nango, monkeypatch):
    # The schema (uq_integration_connection_owner_provider) normally forbids two
    # rows per owner+provider, so the ambiguity is injected at the lookup
    # boundary to exercise the service's strict defense-in-depth guard.
    _workspace, _user, execution = await _seed_email_action(db_session, connections=1)
    real_exec = db_session.exec

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

        def first(self):
            return self._rows[0] if self._rows else None

    injected = [
        SimpleNamespace(nango_connection_id="nango_conn_1", provider_config_key="gmail-test-key"),
        SimpleNamespace(nango_connection_id="nango_conn_2", provider_config_key="gmail-test-key"),
    ]

    async def fake_exec(statement, *args, **kwargs):
        descriptions = getattr(statement, "column_descriptions", None)
        if descriptions and descriptions[0].get("entity") is IntegrationConnection:
            return _Result(injected)
        return await real_exec(statement, *args, **kwargs)

    monkeypatch.setattr(db_session, "exec", fake_exec)

    with pytest.raises(IntegrationExecutionError, match="Conecte seu Gmail"):
        await IntegrationService().execute_claimed(db_session, execution.id)
    assert nango["calls"] == []


@pytest.mark.parametrize("missing", ["env_key", "provider_key"])
async def test_send_email_fails_closed_when_nango_is_not_configured(
    db_session, nango, monkeypatch, missing,
):
    # Given Nango environment or the Gmail provider config key is unset.
    _workspace, _user, execution = await _seed_email_action(db_session, connections=1)
    monkeypatch.setattr(
        settings,
        "NANGO_ENV_KEY" if missing == "env_key" else "NANGO_GOOGLE_GMAIL_PROVIDER_CONFIG_KEY",
        "",
    )

    with pytest.raises(IntegrationExecutionError, match="Gmail não está configurado"):
        await IntegrationService().execute_claimed(db_session, execution.id)
    assert nango["calls"] == []


@pytest.mark.parametrize("error", ["client_error", "value_error"])
async def test_proxy_failure_is_raised_and_never_a_silent_success(
    db_session, nango, error,
):
    _workspace, _user, execution = await _seed_email_action(db_session)
    nango["error"] = (
        NangoClientError(502, "http_error") if error == "client_error"
        else ValueError("raw inválido")
    )

    with pytest.raises(IntegrationExecutionError, match="Gmail request falhou") as exc_info:
        await IntegrationService().execute_claimed(db_session, execution.id)
    assert len(nango["calls"]) == 1
    assert "http_error" not in str(exc_info.value)


async def test_proxy_failure_marks_a_retryable_failed_ledger_row(db_session, nango):
    # Given a pending ledger row whose Gmail proxy call explodes.
    _workspace, _user, execution = await _seed_email_action(db_session, status="pending")
    nango["error"] = NangoClientError(502, "http_error")
    factory = async_sessionmaker(db_session.bind, class_=AsyncSession, expire_on_commit=False)

    result = await run_action_execution(factory, execution.id)

    # Then the ledger records a sanitized retryable failure, never a success.
    assert result == "retry_scheduled"
    await db_session.refresh(execution)
    assert execution.status == "pending"
    assert execution.attempts == 1
    assert execution.last_error == "integrationexecutionerror"
    assert "http_error" not in (execution.last_error or "")


async def test_message_id_stays_idempotent_across_retries(db_session, nango):
    # Given an at-least-once retry of the same claimed ledger row.
    _workspace, _user, execution = await _seed_email_action(db_session)
    service = IntegrationService()

    await service.execute_claimed(db_session, execution.id)
    await service.execute_claimed(db_session, execution.id)

    # Then both deliveries carry the same deterministic Message-ID.
    assert len(nango["calls"]) == 2
    message_ids = [
        _decode_mime(call["json"]["raw"])["Message-ID"]
        for call in nango["calls"]
    ]
    expected = hashlib.sha256(execution.idempotency_key.encode("utf-8")).hexdigest()
    assert message_ids[0] == message_ids[1] == f"<{expected}@anotai>"
