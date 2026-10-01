"""Focused action executor transaction, ordering, and provider tests."""

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

import app.services.action_executor as action_executor
import app.services.integration_service as integration_service
from app.core.config import settings
from app.core.user_secret_crypto import encrypt_user_secret
from app.models.action_execution import ActionExecution, ActionExecutionStatus
from app.models.action_item import Reminder, Task
from app.models.user import User
from app.models.user_secret import UserSecret
from app.models.workflow import Workflow, WorkflowExecution, WorkflowStep
from app.models.workspace import Workspace, WorkspaceMembership
from app.services.action_executor import process_pending_actions, run_action_execution


async def _execution(
    session,
    *,
    action_type="CREATE_TASK",
    action_input=None,
    status: ActionExecutionStatus = "pending",
    attempts: int = 0,
    lease_expires_at: datetime | None = None,
    position=0,
    workflow_enabled=True,
    workflow_version=1,
    execution_id=None,
):
    workspace = Workspace(name="Executor workspace")
    user = User(
        name="Executor user",
        email=f"executor-{uuid.uuid4().hex}@example.test",
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
        title="Executor workflow",
        trigger_type="DOCUMENT_GENERATED",
        enabled=workflow_enabled,
        version=workflow_version,
    )
    session.add(workflow)
    await session.flush()
    step = WorkflowStep(
        workflow_id=workflow.id,
        position=position,
        action_type=action_type,
        input_template={},
        enabled=True,
    )
    session.add(step)
    workflow_execution = WorkflowExecution(
        workspace_id=workspace.id,
        user_id=user.id,
        workflow_id=workflow.id,
        trigger_type="DOCUMENT_GENERATED",
        source_id=str(uuid.uuid4()),
        idempotency_key=str(uuid.uuid4()),
        status="pending",
    )
    session.add(workflow_execution)
    await session.flush()
    canonical_input = action_input or {
        "title": "Create a safe task",
        "summary": "",
        "confidence": None,
        "due_at": None,
    }
    action = ActionExecution(
        id=execution_id or uuid.uuid4(),
        workspace_id=workspace.id,
        user_id=user.id,
        workflow_execution_id=workflow_execution.id,
        workflow_step_id=step.id,
        trigger_type="DOCUMENT_GENERATED",
        source_id=workflow_execution.source_id,
        action_type=action_type,
        idempotency_key=str(uuid.uuid4()),
        policy_snapshot={
            "workflow_id": str(workflow.id),
            "workflow_version": workflow_version,
            "authorized": True,
            "require_confirmation": False,
        },
        action_input=canonical_input,
        workflow_version=workflow_version,
        status=status,
        attempts=attempts,
        lease_expires_at=lease_expires_at,
    )
    session.add(action)
    await session.commit()
    await session.refresh(action)
    return workspace, user, workflow, workflow_execution, step, action


async def test_task_and_reminder_write_with_success_in_same_execution_transaction(worker_db, monkeypatch):
    factory = async_sessionmaker(worker_db.bind, class_=AsyncSession, expire_on_commit=False)
    schedule = AsyncMock()
    monkeypatch.setattr(action_executor, "schedule_reminder", schedule)
    _, _, _, _, _, task_execution = await _execution(worker_db)

    task_result = await run_action_execution(factory, task_execution.id)
    await worker_db.refresh(task_execution)
    assert task_result == "succeeded", task_execution.last_error
    assert task_execution.status == "succeeded"
    schedule.assert_not_awaited()
    tasks = list((await worker_db.exec(select(Task))).all())
    assert len(tasks) == 1 and tasks[0].title == "Create a safe task"

    reminder_input = {
        "title": "Review draft",
        "remind_at": "2030-01-01T10:00:00+00:00",
        "confidence": None,
    }
    _, _, _, _, _, reminder_execution = await _execution(
        worker_db,
        action_type="CREATE_REMINDER",
        action_input=reminder_input,
    )
    assert await run_action_execution(factory, reminder_execution.id) == "succeeded"
    await worker_db.refresh(reminder_execution)
    reminders = list((await worker_db.exec(select(Reminder))).all())
    assert reminder_execution.status == "succeeded"
    assert len(reminders) == 1 and reminders[0].title == "Review draft"
    schedule.assert_awaited_once()


async def test_reminder_wakeup_runs_after_commit_with_utc_target_and_duplicate_is_noop(
    worker_db, monkeypatch
):
    factory = async_sessionmaker(worker_db.bind, class_=AsyncSession, expire_on_commit=False)
    reminder_input = {
        "title": "Call tomorrow",
        "remind_at": "2030-01-01T10:00:00-03:00",
        "confidence": None,
    }
    _, _, _, _, _, execution = await _execution(
        worker_db, action_type="CREATE_REMINDER", action_input=reminder_input
    )
    observations = []

    async def schedule_after_commit(reminder_id, target):
        async with factory() as session:
            reminder = await session.get(Reminder, reminder_id)
            ledger = await session.get(ActionExecution, execution.id)
            assert reminder is not None and reminder.status == "scheduled"
            assert ledger is not None and ledger.status == "succeeded"
            observations.append((reminder_id, target))

    monkeypatch.setattr(action_executor, "schedule_reminder", schedule_after_commit)

    assert await run_action_execution(factory, execution.id) == "succeeded"
    assert await run_action_execution(factory, execution.id) == "not_claimed"

    assert len(observations) == 1
    reminder_id, target = observations[0]
    assert target == datetime(2030, 1, 1, 13, tzinfo=timezone.utc)
    assert target.utcoffset() == timedelta(0)
    assert reminder_id == (await worker_db.exec(select(Reminder))).one().id


async def test_reminder_redis_failure_does_not_undo_succeeded_action(worker_db, monkeypatch):
    factory = async_sessionmaker(worker_db.bind, class_=AsyncSession, expire_on_commit=False)
    _, _, _, _, _, execution = await _execution(
        worker_db,
        action_type="CREATE_REMINDER",
        action_input={
            "title": "Call tomorrow",
            "remind_at": "2030-01-01T10:00:00+00:00",
            "confidence": None,
        },
    )

    async def redis_down(*_args, **_kwargs):
        raise ConnectionError("redis credential and reminder text should not leak")

    monkeypatch.setattr(action_executor, "schedule_reminder", redis_down)

    assert await run_action_execution(factory, execution.id) == "succeeded"
    await worker_db.refresh(execution)
    assert execution.status == "succeeded"
    assert len(list((await worker_db.exec(select(Reminder))).all())) == 1


async def test_stale_completion_does_not_schedule_reminder(worker_db, monkeypatch):
    factory = async_sessionmaker(worker_db.bind, class_=AsyncSession, expire_on_commit=False)
    _, _, _, _, _, execution = await _execution(
        worker_db,
        action_type="CREATE_REMINDER",
        action_input={
            "title": "No stale enqueue",
            "remind_at": "2030-01-01T10:00:00+00:00",
            "confidence": None,
        },
    )
    schedule = AsyncMock()

    async def stale_completion(session, *_args, **_kwargs):
        await session.rollback()
        return False

    monkeypatch.setattr(action_executor, "schedule_reminder", schedule)
    monkeypatch.setattr(action_executor, "complete_action", stale_completion)

    assert await run_action_execution(factory, execution.id) == "stale_completion"
    schedule.assert_not_awaited()


async def test_retry_then_terminal_failure_uses_sanitized_error_kind(worker_db, monkeypatch):
    factory = async_sessionmaker(worker_db.bind, class_=AsyncSession, expire_on_commit=False)
    _, _, _, _, _, execution = await _execution(worker_db)

    async def fail_execution(*_args, **_kwargs):
        raise RuntimeError("provider response included sensitive-token-value")

    monkeypatch.setattr(integration_service.IntegrationService, "execute_claimed", fail_execution)
    for _ in range(4):
        assert await run_action_execution(factory, execution.id) == "retry_scheduled"
    assert await run_action_execution(factory, execution.id) == "failed"

    await worker_db.refresh(execution)
    assert execution.status == "failed"
    assert execution.attempts == 5
    assert execution.last_error == "runtimeerror"
    assert "sensitive-token-value" not in str(execution.last_error)
    assert list((await worker_db.exec(select(Task))).all()) == []


async def test_awaiting_confirmation_is_never_executed(worker_db, monkeypatch):
    factory = async_sessionmaker(worker_db.bind, class_=AsyncSession, expire_on_commit=False)
    _, _, _, _, _, execution = await _execution(worker_db, status="awaiting_confirmation")
    execute = AsyncMock()
    monkeypatch.setattr(integration_service.IntegrationService, "execute_claimed", execute)

    counts = await process_pending_actions(factory)
    result = await run_action_execution(factory, execution.id)

    await worker_db.refresh(execution)
    assert result == "not_claimed"
    assert execution.status == "awaiting_confirmation" and execution.attempts == 0
    execute.assert_not_awaited()
    assert counts["succeeded"] == 0


@pytest.mark.parametrize("deleted", [False, True])
async def test_disabled_or_deleted_workflow_cannot_run_provider_action(worker_db, deleted):
    factory = async_sessionmaker(worker_db.bind, class_=AsyncSession, expire_on_commit=False)
    _, _, workflow, _, _, execution = await _execution(worker_db)
    if deleted:
        await worker_db.delete(workflow)
        await worker_db.commit()
    else:
        workflow.enabled = False
        await worker_db.commit()

    result = await run_action_execution(factory, execution.id)

    await worker_db.refresh(execution)
    assert result == "retry_scheduled"
    assert execution.status == "pending"
    assert execution.last_error == "integrationexecutionerror"
    assert list((await worker_db.exec(select(Task))).all()) == []


async def test_duplicate_run_does_not_execute_task_twice(worker_db):
    factory = async_sessionmaker(worker_db.bind, class_=AsyncSession, expire_on_commit=False)
    _, _, _, _, _, execution = await _execution(worker_db)

    assert await run_action_execution(factory, execution.id) == "succeeded"
    assert await run_action_execution(factory, execution.id) == "not_claimed"
    assert len(list((await worker_db.exec(select(Task))).all())) == 1


async def test_scheduler_reclaims_expired_claim(worker_db):
    factory = async_sessionmaker(worker_db.bind, class_=AsyncSession, expire_on_commit=False)
    _, _, _, _, _, execution = await _execution(
        worker_db,
        status="claimed",
        attempts=1,
        lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=5),
    )

    counts = await process_pending_actions(factory)

    await worker_db.refresh(execution)
    assert counts["succeeded"] == 1
    assert execution.status == "succeeded" and execution.attempts == 2


async def test_second_step_waits_for_predecessor_success(worker_db):
    factory = async_sessionmaker(worker_db.bind, class_=AsyncSession, expire_on_commit=False)
    _, user, workflow, workflow_execution, first_step, first = await _execution(
        worker_db, status="awaiting_confirmation", position=0
    )
    second_step = WorkflowStep(
        workflow_id=workflow.id,
        position=1,
        action_type="CREATE_TASK",
        input_template={},
        enabled=True,
    )
    worker_db.add(second_step)
    await worker_db.flush()
    second = ActionExecution(
        workspace_id=first.workspace_id,
        user_id=user.id,
        workflow_execution_id=workflow_execution.id,
        workflow_step_id=second_step.id,
        trigger_type=workflow.trigger_type,
        source_id=first.source_id,
        action_type="CREATE_TASK",
        idempotency_key=str(uuid.uuid4()),
        policy_snapshot=first.policy_snapshot,
        action_input={"title": "Second task", "summary": "", "confidence": None, "due_at": None},
        workflow_version=workflow.version,
        status="pending",
    )
    worker_db.add(second)
    await worker_db.commit()

    assert await run_action_execution(factory, second.id) == "blocked_by_previous_step"
    assert list((await worker_db.exec(select(Task))).all()) == []
    first.status = "succeeded"
    await worker_db.commit()
    assert await run_action_execution(factory, second.id) == "succeeded"
    assert [task.title for task in (await worker_db.exec(select(Task))).all()] == ["Second task"]


async def test_telegram_provider_is_mocked_and_never_receives_configured_destination(
    worker_db, monkeypatch
):
    factory = async_sessionmaker(worker_db.bind, class_=AsyncSession, expire_on_commit=False)
    telegram_input = {"text": "Approved notification", "confidence": None}
    _, user, _, _, _, telegram_execution = await _execution(
        worker_db,
        action_type="SEND_TELEGRAM",
        action_input=telegram_input,
    )
    chat_secret = UserSecret(user_id=user.id, key="TELEGRAM_CHAT_ID", value="")
    chat_secret.value = encrypt_user_secret(
        user.id, chat_secret.id, "chat-from-secret", key=settings.USER_SECRET_ENCRYPTION_KEY,
    )
    worker_db.add(chat_secret)
    await worker_db.commit()
    monkeypatch.setattr(integration_service.settings, "TELEGRAM_BOT_TOKEN", "platform-token")

    class FakeBot:
        token = None
        sent = None

        def __init__(self, token):
            FakeBot.token = token

        async def send_message(self, *, chat_id, text):
            FakeBot.sent = {"chat_id": chat_id, "text": text}
            return SimpleNamespace(message_id=42)

    monkeypatch.setattr(integration_service, "Bot", FakeBot)

    assert await run_action_execution(factory, telegram_execution.id) == "succeeded"
    assert FakeBot.token == "platform-token"
    assert FakeBot.sent == {"chat_id": "chat-from-secret", "text": "Approved notification"}
