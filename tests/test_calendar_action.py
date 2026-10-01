"""Calendar action schema, confirmation policy, and Nango integration tests."""

import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

import app.services.integration_service as integration_service
import app.services.workflow_engine as workflow_engine
from app.models.action_execution import ActionExecution
from app.models.automation_settings import AutomationMode, AutomationSettings
from app.models.integration_connection import IntegrationConnection
from app.models.user import User
from app.models.workflow import Workflow, WorkflowExecution, WorkflowStep
from app.models.workspace import Workspace, WorkspaceMembership
from app.schemas.workflow import WorkflowStepWrite
from app.schemas.workflow_action import ActionValidationError, WorkflowActionType
from app.services.action_registry import validate_action
from app.services.integration_service import IntegrationExecutionError, IntegrationService
from app.services.nango_client import NangoClientError
from app.services.workflow_engine import prepare_workflow_executions


def _calendar_action(**updates):
    payload = {
        "type": "CREATE_CALENDAR_EVENT",
        "summary": "Quarterly review",
        "start_at": "2030-01-15T10:00:00-05:00",
        "end_at": "2030-01-15T11:00:00-05:00",
        "timezone": "America/New_York",
    }
    payload.update(updates)
    return payload


def test_calendar_action_validates_aware_interval_and_registry_fields():
    action = validate_action(_calendar_action())
    assert action.type is WorkflowActionType.CREATE_CALENDAR_EVENT
    assert action.start_at.utcoffset() == action.end_at.utcoffset()

    invalid = [
        _calendar_action(start_at="2030-01-15T10:00:00"),
        _calendar_action(end_at="2030-01-15T09:00:00-05:00"),
        _calendar_action(timezone="Not/AZone"),
        _calendar_action(calendarId="other-calendar"),
        _calendar_action(attendees=[{"email": "victim@example.test"}]),
        _calendar_action(connectionId="conn-foreign"),
        _calendar_action(token="credential"),
        _calendar_action(summary="Visit https://evil.example"),
    ]
    for payload in invalid:
        with pytest.raises(ActionValidationError):
            validate_action(payload)


def test_workflow_calendar_step_defaults_to_confirmation():
    defaulted = WorkflowStepWrite.model_validate({
        "position": 0,
        "action_type": "CREATE_CALENDAR_EVENT",
        "input_template": {
            "summary": "Review",
            "start_at": "2030-01-15T10:00:00Z",
            "end_at": "2030-01-15T11:00:00Z",
        },
    })
    explicitly_automatic = WorkflowStepWrite.model_validate({
        "position": 0,
        "action_type": "CREATE_CALENDAR_EVENT",
        "input_template": {
            "summary": "Review",
            "start_at": "2030-01-15T10:00:00Z",
            "end_at": "2030-01-15T11:00:00Z",
        },
        "require_confirmation": False,
    })
    assert defaulted.require_confirmation is True
    assert explicitly_automatic.require_confirmation is False


async def _prepare_calendar_action(session, *, mode: AutomationMode | None):
    workspace = Workspace(name="Calendar action workspace", timezone="UTC")
    user = User(
        name="Calendar owner",
        email=f"calendar-{uuid.uuid4().hex}@example.test",
        hashed_password="not-a-real-password",
        workspace_id=workspace.id,
    )
    session.add(workspace)
    session.add(user)
    await session.flush()
    session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="OWNER"))
    if mode is not None:
        session.add(AutomationSettings(
            workspace_id=workspace.id,
            user_id=user.id,
            automation_mode=mode,
            action_config={"CREATE_CALENDAR_EVENT": "automatic"},
        ))
    workflow = Workflow(
        workspace_id=workspace.id,
        user_id=user.id,
        title="Calendar workflow",
        trigger_type="DOCUMENT_GENERATED",
        enabled=True,
    )
    session.add(workflow)
    await session.flush()
    session.add(WorkflowStep(
        workflow_id=workflow.id,
        position=0,
        action_type="CREATE_CALENDAR_EVENT",
        input_template={
            "summary": "Review",
            "start_at": "2030-01-15T10:00:00Z",
            "end_at": "2030-01-15T11:00:00Z",
        },
        require_confirmation=False,
        enabled=True,
    ))
    await session.commit()
    await prepare_workflow_executions(
        session, workspace.id, user.id, "DOCUMENT_GENERATED", source_id=str(uuid.uuid4())
    )
    execution = (await session.exec(
        select(ActionExecution).where(
            ActionExecution.workspace_id == workspace.id,
            ActionExecution.user_id == user.id,
        )
    )).one()
    return workspace, user, workflow, execution


async def test_calendar_confirmation_and_automatic_policy(db_session):
    _, _, _, suggestive = await _prepare_calendar_action(db_session, mode=None)
    assert suggestive.status == "awaiting_confirmation"

    _, _, _, automatic = await _prepare_calendar_action(db_session, mode="automatic")
    assert automatic.status == "pending"
    assert automatic.policy_snapshot["authorized"] is True


async def _claimed_calendar_action(session, *, with_connection=True):
    workspace = Workspace(name="Calendar integration workspace", timezone="UTC")
    owner = User(
        name="Calendar owner",
        email=f"calendar-int-{uuid.uuid4().hex}@example.test",
        hashed_password="not-a-real-password",
        workspace_id=workspace.id,
    )
    session.add(workspace)
    session.add(owner)
    await session.flush()
    session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=owner.id, role="OWNER"))
    workflow = Workflow(
        workspace_id=workspace.id,
        user_id=owner.id,
        title="Calendar flow",
        trigger_type="DOCUMENT_GENERATED",
        enabled=True,
        version=2,
    )
    session.add(workflow)
    await session.flush()
    step = WorkflowStep(
        workflow_id=workflow.id,
        position=0,
        action_type="CREATE_CALENDAR_EVENT",
        input_template={},
        require_confirmation=False,
        enabled=True,
    )
    parent = WorkflowExecution(
        workspace_id=workspace.id,
        user_id=owner.id,
        workflow_id=workflow.id,
        trigger_type="DOCUMENT_GENERATED",
        source_id="document-1",
        idempotency_key=str(uuid.uuid4()),
    )
    session.add_all([step, parent])
    await session.flush()
    execution = ActionExecution(
        workspace_id=workspace.id,
        user_id=owner.id,
        workflow_execution_id=parent.id,
        workflow_step_id=step.id,
        trigger_type=workflow.trigger_type,
        source_id=parent.source_id,
        action_type="CREATE_CALENDAR_EVENT",
        idempotency_key=str(uuid.uuid4()),
        policy_snapshot={
            "workflow_id": str(workflow.id),
            "workflow_version": workflow.version,
            "authorized": True,
            "require_confirmation": False,
        },
        workflow_version=workflow.version,
        action_input={
            "summary": "Quarterly review",
            "start_at": "2030-01-15T10:00:00-05:00",
            "end_at": "2030-01-15T11:00:00-05:00",
            "timezone": "America/New_York",
            "confidence": None,
        },
        status="claimed",
    )
    session.add(execution)
    if with_connection:
        session.add(IntegrationConnection(
            workspace_id=workspace.id,
            user_id=owner.id,
            provider="google_calendar",
            provider_config_key="google-calendar-key",
            nango_connection_id="nango_conn_123",
            status="active",
        ))
    await session.commit()
    return workspace, owner, execution


async def test_calendar_proxy_uses_owned_connection_and_safe_event_payload(db_session, monkeypatch):
    workspace, owner, execution = await _claimed_calendar_action(db_session)
    monkeypatch.setattr(integration_service.settings, "NANGO_ENV_KEY", "nango-env-secret")
    monkeypatch.setattr(integration_service.settings, "NANGO_BASE_URL", "https://nango.example.test")
    monkeypatch.setattr(
        integration_service.settings,
        "NANGO_GOOGLE_CALENDAR_PROVIDER_CONFIG_KEY",
        "google-calendar-key",
    )
    seen = {}

    class FakeNangoClient:
        def __init__(self, *, api_key, base_url):
            seen["api_key"] = api_key
            seen["base_url"] = base_url

        async def proxy_request(self, method, path, **kwargs):
            seen.update({"method": method, "path": path, **kwargs})
            return SimpleNamespace(status_code=200, body={"id": "google_event_123", "htmlLink": "do-not-return"})

    monkeypatch.setattr(integration_service, "NangoClient", FakeNangoClient)

    result = await IntegrationService().execute_claimed(db_session, execution.id)

    assert result == {"entity": "calendar_event", "id": "google_event_123"}
    assert seen["api_key"] == "nango-env-secret"
    assert seen["base_url"] == "https://nango.example.test"
    assert seen["method"] == "POST"
    assert seen["path"] == "calendar/v3/calendars/primary/events"
    assert seen["connection_id"] == "nango_conn_123"
    assert seen["provider_config_key"] == "google-calendar-key"
    assert seen["json_body"] == {
        "summary": "Quarterly review",
        "start": {"dateTime": "2030-01-15T10:00:00-05:00", "timeZone": "America/New_York"},
        "end": {"dateTime": "2030-01-15T11:00:00-05:00", "timeZone": "America/New_York"},
    }
    assert workspace.id == execution.workspace_id and owner.id == execution.user_id
    assert "htmlLink" not in str(result)


@pytest.mark.parametrize("connection_kind", ["missing", "foreign", "inactive"])
async def test_calendar_fails_before_proxy_without_owned_active_connection(
    db_session, monkeypatch, connection_kind
):
    workspace, owner, execution = await _claimed_calendar_action(db_session, with_connection=False)
    if connection_kind in {"foreign", "inactive"}:
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
            provider="google_calendar",
            provider_config_key="google-calendar-key",
            nango_connection_id="foreign_conn_123",
            status="active" if connection_kind == "foreign" else "disconnected",
        ))
        await db_session.commit()
    proxy = AsyncMock()

    class FakeNangoClient:
        def __init__(self, **_kwargs):
            pass

        proxy_request = proxy

    monkeypatch.setattr(integration_service, "NangoClient", FakeNangoClient)
    monkeypatch.setattr(integration_service.settings, "NANGO_ENV_KEY", "env-key")
    monkeypatch.setattr(
        integration_service.settings,
        "NANGO_GOOGLE_CALENDAR_PROVIDER_CONFIG_KEY",
        "google-calendar-key",
    )

    with pytest.raises(IntegrationExecutionError, match="Conexão Google Calendar"):
        await IntegrationService().execute_claimed(db_session, execution.id)
    proxy.assert_not_awaited()


async def test_calendar_proxy_errors_are_sanitized(db_session, monkeypatch):
    await _claimed_calendar_action(db_session)
    monkeypatch.setattr(integration_service.settings, "NANGO_ENV_KEY", "env-key")
    monkeypatch.setattr(
        integration_service.settings,
        "NANGO_GOOGLE_CALENDAR_PROVIDER_CONFIG_KEY",
        "google-calendar-key",
    )

    class FakeNangoClient:
        def __init__(self, **_kwargs):
            pass

        async def proxy_request(self, *_args, **_kwargs):
            raise NangoClientError(401, "oauth_secret_must_not_leak")

    monkeypatch.setattr(integration_service, "NangoClient", FakeNangoClient)

    with pytest.raises(IntegrationExecutionError, match="request falhou") as error:
        await IntegrationService().execute_claimed(db_session, (await db_session.exec(select(ActionExecution))).one().id)
    assert "oauth_secret" not in str(error.value)
