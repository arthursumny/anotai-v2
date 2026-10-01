"""Focused security, idempotency, and Telegram provider tests."""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlmodel import select

from app.core.config import settings
from app.core.user_secret_crypto import encrypt_user_secret
from app.models.action_execution import ActionExecution
from app.models.action_item import Reminder, Task
from app.models.user import User
from app.models.user_secret import UserSecret
from app.models.workflow import Workflow, WorkflowExecution, WorkflowStep
from app.models.workspace import Workspace, WorkspaceMembership
from app.services.integration_service import IntegrationExecutionError, IntegrationService


async def _seed_action(
    session,
    *,
    action_type="CREATE_TASK",
    action_input=None,
    status="claimed",
    policy_snapshot=None,
    membership=True,
    trigger_type="MANUAL",
    standalone=False,
):
    workspace = Workspace(name="Integration")
    session.add(workspace)
    await session.flush()
    user = User(
        name="Integration user", email=f"{uuid.uuid4()}@example.com", hashed_password="unused",
        workspace_id=workspace.id,
    )
    session.add(user)
    await session.flush()
    if membership:
        session.add(WorkspaceMembership(
            workspace_id=workspace.id, user_id=user.id, role="OWNER",
        ))
    workflow = Workflow(
        workspace_id=workspace.id,
        user_id=user.id,
        title="Integration workflow",
        trigger_type="MANUAL",
    )
    session.add(workflow)
    await session.flush()
    step = WorkflowStep(workflow_id=workflow.id, position=0, action_type=action_type)
    workflow_run = WorkflowExecution(
        workspace_id=workspace.id,
        user_id=user.id,
        workflow_id=workflow.id,
        trigger_type="MANUAL",
        idempotency_key=f"run-{uuid.uuid4()}",
    )
    session.add_all([step, workflow_run])
    await session.flush()
    snapshot = dict(policy_snapshot) if policy_snapshot is not None else {
        "authorized": True, "require_confirmation": False,
    }
    if standalone:
        snapshot["standalone"] = True
        snapshot.pop("workflow_id", None)
    else:
        snapshot.setdefault("workflow_id", str(workflow.id))
    execution = ActionExecution(
        workspace_id=workspace.id,
        user_id=user.id,
        workflow_execution_id=None if standalone else workflow_run.id,
        workflow_step_id=None if standalone else step.id,
        workflow_version=None if standalone else workflow.version,
        trigger_type=trigger_type,
        source_id="source-1",
        action_type=action_type,
        idempotency_key=f"ledger-{uuid.uuid4()}",
        action_input=action_input if action_input is not None else {"title": "Follow up"},
        policy_snapshot=snapshot,
        status=status,
    )
    session.add(execution)
    await session.flush()
    return workspace, user, workflow, step, execution


async def test_task_execution_is_idempotent_and_uses_ledger_owner_workspace(db_session):
    workspace, user, workflow, _step, execution = await _seed_action(
        db_session,
        action_input={
            "title": "Review invoice",
            "summary": "Confirm the total",
            "confidence": 0.9,
            "due_at": "2030-05-01T12:00:00+03:00",
        },
    )
    service = IntegrationService()

    first = await service.execute_claimed(db_session, execution.id)
    second = await service.execute_claimed(db_session, execution.id)

    assert first == second
    tasks = (await db_session.exec(select(Task))).all()
    assert len(tasks) == 1
    assert tasks[0].workspace_id == workspace.id and tasks[0].user_id == user.id
    assert tasks[0].status == "open"
    assert tasks[0].timezone == workspace.timezone
    assert tasks[0].due_at.replace(tzinfo=timezone.utc) == datetime(2030, 5, 1, 9, tzinfo=timezone.utc)
    assert Task.__table__.c.due_at.type.timezone is True
    assert execution.status == "claimed"
    assert db_session.in_transaction()


async def test_reminder_is_created_utc_without_scheduling_external_job(db_session):
    workspace, user, workflow, _step, execution = await _seed_action(
        db_session,
        action_type="CREATE_REMINDER",
        action_input={"title": "Call back", "remind_at": "2031-02-03T12:00:00-05:00"},
    )
    result = await IntegrationService().execute_claimed(db_session, execution.id)
    reminder = (await db_session.exec(select(Reminder))).one()
    assert result["entity"] == "reminder"
    assert reminder.workspace_id == workspace.id and reminder.user_id == user.id
    assert reminder.status == "scheduled"
    assert reminder.timezone == workspace.timezone
    assert reminder.remind_at.replace(tzinfo=timezone.utc) == datetime(2031, 2, 3, 17, tzinfo=timezone.utc)
    assert Reminder.__table__.c.remind_at.type.timezone is True


@pytest.mark.parametrize("status", ["pending", "awaiting_confirmation", "succeeded"])
async def test_only_claimed_action_can_execute(db_session, status):
    _workspace, _user, _workflow, _step, execution = await _seed_action(db_session, status=status)
    with pytest.raises(IntegrationExecutionError, match="claimed"):
        await IntegrationService().execute_claimed(db_session, execution.id)
    assert (await db_session.exec(select(Task))).all() == []


async def test_missing_membership_and_unapproved_policy_are_rejected(db_session):
    workspace, user, workflow, _step, execution = await _seed_action(
        db_session, membership=False,
    )
    with pytest.raises(IntegrationExecutionError, match="escopo"):
        await IntegrationService().execute_claimed(db_session, execution.id)

    session_user = User(
        name="Member", email=f"member-{uuid.uuid4()}@example.com", hashed_password="unused",
        workspace_id=workspace.id,
    )
    db_session.add(session_user)
    await db_session.flush()
    db_session.add(WorkspaceMembership(
        workspace_id=workspace.id, user_id=session_user.id, role="OWNER",
    ))
    session_workflow = Workflow(
        workspace_id=workspace.id, user_id=session_user.id, title="Member workflow",
        trigger_type="MANUAL",
    )
    db_session.add(session_workflow)
    await db_session.flush()
    unauthorized_ledger = ActionExecution(
        workspace_id=workspace.id, user_id=session_user.id,
        trigger_type="MANUAL", action_type="CREATE_TASK", idempotency_key="unapproved",
        action_input={"title": "Must not run"},
        policy_snapshot={
            "authorized": False, "require_confirmation": False,
            "workflow_id": str(session_workflow.id),
        },
        workflow_version=session_workflow.version,
        status="claimed",
    )
    db_session.add(unauthorized_ledger)
    await db_session.flush()
    with pytest.raises(IntegrationExecutionError, match="Policy snapshot"):
        await IntegrationService().execute_claimed(db_session, unauthorized_ledger.id)
    assert (await db_session.exec(select(Task))).all() == []


async def test_confirmation_policy_requires_explicit_user_confirmation(db_session):
    _workspace, _user, _workflow, _step, execution = await _seed_action(
        db_session,
        policy_snapshot={"authorized": True, "require_confirmation": True},
    )
    with pytest.raises(IntegrationExecutionError, match="confirmação"):
        await IntegrationService().execute_claimed(db_session, execution.id)

    execution.policy_snapshot = {
        **execution.policy_snapshot,
        "authorized": True,
        "require_confirmation": True,
        "confirmed_by_user": True,
    }
    await db_session.flush()
    assert (await IntegrationService().execute_claimed(db_session, execution.id))["entity"] == "task"


async def test_manual_user_confirmation_is_sufficient_even_without_auto_authorization(db_session):
    _workspace, _user, _workflow, _step, execution = await _seed_action(
        db_session,
        policy_snapshot={
            "authorized": False,
            "require_confirmation": True,
            "confirmed_by_user": True,
        },
    )
    result = await IntegrationService().execute_claimed(db_session, execution.id)
    assert result["entity"] == "task"


@pytest.mark.parametrize("change", ["deleted", "disabled", "version"])
async def test_deleted_disabled_or_stale_workflow_is_rejected_before_provider_call(
    db_session, monkeypatch, change,
):
    _workspace, _user, workflow, _step, execution = await _seed_action(
        db_session,
        action_type="SEND_TELEGRAM",
        action_input={"text": "An approved message"},
    )
    bot_calls = []

    class FakeBot:
        def __init__(self, **kwargs):
            bot_calls.append(kwargs)

        async def send_message(self, **kwargs):
            bot_calls.append(kwargs)
            return SimpleNamespace(message_id=1)

    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "server-token")
    monkeypatch.setattr("app.services.integration_service.Bot", FakeBot)
    if change == "deleted":
        await db_session.delete(workflow)
    elif change == "disabled":
        workflow.enabled = False
    else:
        workflow.version += 1
    await db_session.flush()

    with pytest.raises(IntegrationExecutionError, match="Workflow"):
        await IntegrationService().execute_claimed(db_session, execution.id)
    assert bot_calls == []


@pytest.mark.parametrize("workflow_id", [None, "not-a-uuid"])
async def test_workflow_action_without_valid_workflow_provenance_is_rejected(db_session, workflow_id):
    _workspace, _user, _workflow, _step, execution = await _seed_action(db_session)
    execution.policy_snapshot["workflow_id"] = workflow_id
    await db_session.flush()
    with pytest.raises(IntegrationExecutionError, match="Workflow provenance"):
        await IntegrationService().execute_claimed(db_session, execution.id)


async def test_standalone_scheduler_actions_require_exact_server_flag_and_trigger(db_session):
    _workspace, _user, _workflow, _step, allowed = await _seed_action(
        db_session,
        action_type="CREATE_REMINDER",
        action_input={"title": "Reminder", "remind_at": "2032-05-06T07:00:00+00:00"},
        trigger_type="REMINDER_DUE",
        standalone=True,
    )
    assert (await IntegrationService().execute_claimed(db_session, allowed.id))["entity"] == "reminder"

    _workspace, _user, _workflow, _step, wrong_trigger = await _seed_action(
        db_session,
        action_input={"title": "Must not execute"},
        trigger_type="MANUAL",
        standalone=True,
    )
    with pytest.raises(IntegrationExecutionError, match="standalone"):
        await IntegrationService().execute_claimed(db_session, wrong_trigger.id)

    _workspace, _user, _workflow, _step, unflagged = await _seed_action(
        db_session,
        action_input={"title": "Must not execute"},
        trigger_type="FOLLOW_UP_DUE",
        standalone=False,
    )
    unflagged.workflow_execution_id = None
    unflagged.workflow_step_id = None
    unflagged.workflow_version = None
    unflagged.policy_snapshot.pop("workflow_id")
    await db_session.flush()
    with pytest.raises(IntegrationExecutionError, match="Workflow provenance"):
        await IntegrationService().execute_claimed(db_session, unflagged.id)


@pytest.mark.parametrize("action_input", [
    {"type": "CREATE_TASK", "title": "Bad duplicated type"},
    {"title": "Review", "token": "do-not-store"},
    {"title": "Review", "destination": "12345"},
    {"title": "https://attacker.example"},
])
async def test_invalid_or_sensitive_action_input_is_rejected(db_session, action_input):
    _workspace, _user, _workflow, _step, execution = await _seed_action(
        db_session, action_input=action_input,
    )
    with pytest.raises(IntegrationExecutionError):
        await IntegrationService().execute_claimed(db_session, execution.id)
    assert (await db_session.exec(select(Task))).all() == []


async def test_telegram_uses_server_token_user_chat_and_returns_only_message_id(
    db_session, monkeypatch,
):
    _workspace, user, _workflow, _step, execution = await _seed_action(
        db_session,
        action_type="SEND_TELEGRAM",
        action_input={"text": "A" * 2000},
        policy_snapshot={"authorized": True, "require_confirmation": False},
    )
    chat_secret = UserSecret(user_id=user.id, key="TELEGRAM_CHAT_ID", value="")
    token_secret = UserSecret(user_id=user.id, key="TELEGRAM_BOT_TOKEN", value="")
    chat_secret.value = encrypt_user_secret(
        user.id, chat_secret.id, "user-chat-17", key=settings.USER_SECRET_ENCRYPTION_KEY,
    )
    token_secret.value = encrypt_user_secret(
        user.id, token_secret.id, "user-controlled-token", key=settings.USER_SECRET_ENCRYPTION_KEY,
    )
    db_session.add_all([chat_secret, token_secret])
    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "server-bot-token")
    monkeypatch.setattr(settings, "TELEGRAM_DEFAULT_CHAT_ID", "global-default-chat")
    calls = {}

    class FakeBot:
        def __init__(self, *, token):
            calls["token"] = token

        async def send_message(self, *, chat_id, text):
            calls["chat_id"] = chat_id
            calls["text"] = text
            return SimpleNamespace(message_id=981, chat_id=chat_id, text=text)

    monkeypatch.setattr("app.services.integration_service.Bot", FakeBot)
    result = await IntegrationService().execute_claimed(db_session, execution.id)

    assert result == {"message_id": 981}
    assert calls == {
        "token": "server-bot-token", "chat_id": "user-chat-17", "text": "A" * 2000,
    }
    assert execution.status == "claimed"


async def test_telegram_cannot_take_a_destination_from_action_input(db_session, monkeypatch):
    _workspace, user, _workflow, _step, execution = await _seed_action(
        db_session,
        action_type="SEND_TELEGRAM",
        action_input={"text": "Send this", "chat_id": "attacker-chat"},
    )
    chat_secret = UserSecret(user_id=user.id, key="TELEGRAM_CHAT_ID", value="")
    chat_secret.value = encrypt_user_secret(
        user.id, chat_secret.id, "user-chat", key=settings.USER_SECRET_ENCRYPTION_KEY,
    )
    db_session.add(chat_secret)
    calls = []

    class FakeBot:
        def __init__(self, *, token):
            calls.append(token)

        async def send_message(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(message_id=1)

    monkeypatch.setattr(settings, "TELEGRAM_BOT_TOKEN", "server-token")
    monkeypatch.setattr("app.services.integration_service.Bot", FakeBot)
    with pytest.raises(IntegrationExecutionError, match="inválido"):
        await IntegrationService().execute_claimed(db_session, execution.id)
    assert calls == []
