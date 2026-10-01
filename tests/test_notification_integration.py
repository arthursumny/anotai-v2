"""Notification preference integration with workflow actions and legacy notes."""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

import app.services.action_executor as action_executor
import app.worker as worker
from app.models.action_execution import ActionExecution
from app.models.automation_settings import AutomationSettings
from app.models.note import Note, NoteStatus
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMembership
from app.schemas.workflow import AutomationSettingsReplace
from app.services.action_executor import process_pending_actions
from app.services.notification_prefs import should_notify


def _execution(session, workspace, user, *, action_type="SEND_TELEGRAM"):
    execution = ActionExecution(
        workspace_id=workspace.id,
        user_id=user.id,
        trigger_type="MANUAL",
        action_type=action_type,
        idempotency_key=str(uuid.uuid4()),
        policy_snapshot={"standalone": True},
        action_input={"text": "hello"},
    )
    session.add(execution)
    return execution


class _FixedDatetime(datetime):
    current = datetime(2026, 9, 30, 20, 30, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        return cls.current if tz is None else cls.current.astimezone(tz)


@pytest.mark.asyncio
async def test_telegram_remains_pending_during_quiet_then_runs_once(worker_db, monkeypatch):
    _FixedDatetime.current = datetime(2026, 9, 30, 20, 30, tzinfo=timezone.utc)
    workspace = Workspace(name="Notifications", timezone="UTC")
    user = User(name="Owner", email=f"owner-{uuid.uuid4().hex}@example.test", hashed_password="x")
    worker_db.add(workspace)
    worker_db.add(user)
    await worker_db.flush()
    user.workspace_id = workspace.id
    worker_db.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id))
    worker_db.add(AutomationSettings(
        workspace_id=workspace.id, user_id=user.id, quiet_start_minute=20 * 60,
        quiet_end_minute=21 * 60,
    ))
    execution = _execution(worker_db, workspace, user)
    await worker_db.commit()
    await worker_db.refresh(execution)
    factory = async_sessionmaker(worker_db.bind, class_=AsyncSession, expire_on_commit=False)
    execute = AsyncMock(return_value={})
    monkeypatch.setattr(action_executor.IntegrationService, "execute_claimed", lambda self, *args: execute(*args))
    monkeypatch.setattr(action_executor, "datetime", _FixedDatetime)

    for _ in range(3):
        counts = await process_pending_actions(factory)
        await worker_db.refresh(execution)
        assert counts["skipped"] == 1
        assert execution.status == "pending"
        assert execution.attempts == 0
    execute.assert_not_awaited()

    _FixedDatetime.current = datetime(2026, 9, 30, 21, 0, tzinfo=timezone.utc)
    assert (await process_pending_actions(factory))["succeeded"] == 1
    assert (await process_pending_actions(factory))["succeeded"] == 0
    await worker_db.refresh(execution)
    assert execution.attempts == 1
    execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_legacy_note_reminder_is_retried_after_quiet_period(worker_db, monkeypatch):
    _FixedDatetime.current = datetime(2026, 9, 30, 20, 30, tzinfo=timezone.utc)
    workspace = Workspace(name="Legacy", timezone="UTC")
    user = User(name="Owner", email=f"legacy-{uuid.uuid4().hex}@example.test", hashed_password="x")
    worker_db.add(workspace)
    worker_db.add(user)
    await worker_db.flush()
    user.workspace_id = workspace.id
    worker_db.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id))
    worker_db.add(AutomationSettings(
        workspace_id=workspace.id, user_id=user.id, quiet_start_minute=20 * 60,
        quiet_end_minute=21 * 60,
    ))
    note = Note(user_id=user.id, title="Call", status=NoteStatus.PROCESSED,
                reminder_at=datetime(2026, 9, 30, 19, 0, tzinfo=timezone.utc), reminder_sent=False)
    worker_db.add(note)
    await worker_db.commit()
    await worker_db.refresh(note)
    send = AsyncMock(return_value={})
    monkeypatch.setattr(worker, "_send_reminder_telegram", send)
    monkeypatch.setattr(worker, "datetime", _FixedDatetime)

    result = await worker.check_scheduled_reminders({})
    await worker_db.refresh(note)
    assert result["skipped"] == 1
    assert note.reminder_sent is False
    send.assert_not_awaited()

    _FixedDatetime.current = datetime(2026, 9, 30, 21, 0, tzinfo=timezone.utc)
    assert (await worker.check_scheduled_reminders({}))["sent"] == 1
    await worker_db.refresh(note)
    assert note.reminder_sent is True
    send.assert_awaited_once()


def test_settings_quiet_minutes_must_be_a_pair():
    with pytest.raises(ValueError):
        AutomationSettingsReplace(quiet_start_minute=60)


def test_notification_timezone_conversion():
    assert should_notify(
        datetime(2026, 9, 30, 20, 30, tzinfo=timezone.utc), "America/Los_Angeles",
        notifications_enabled=True, quiet_start_minute=13 * 60, quiet_end_minute=14 * 60,
    ) is False


@pytest.mark.asyncio
async def test_another_users_settings_do_not_mute_execution(worker_db):
    workspace = Workspace(name="Tenant scope", timezone="UTC")
    owner = User(name="Owner", email=f"scope-{uuid.uuid4().hex}@example.test", hashed_password="x")
    other = User(name="Other", email=f"scope-{uuid.uuid4().hex}@example.test", hashed_password="x")
    worker_db.add(workspace)
    worker_db.add(owner)
    worker_db.add(other)
    await worker_db.flush()
    owner.workspace_id = workspace.id
    other.workspace_id = workspace.id
    worker_db.add(WorkspaceMembership(workspace_id=workspace.id, user_id=owner.id))
    worker_db.add(WorkspaceMembership(workspace_id=workspace.id, user_id=other.id))
    worker_db.add(AutomationSettings(
        workspace_id=workspace.id, user_id=other.id, notifications_enabled=False,
    ))
    execution = _execution(worker_db, workspace, owner)
    await worker_db.commit()

    assert await action_executor._telegram_notifications_allowed(
        worker_db, execution.id, datetime(2026, 9, 30, 20, 30, tzinfo=timezone.utc),
    ) is True


@pytest.mark.asyncio
async def test_settings_endpoint_rejects_unpaired_quiet_minutes(client, auth_headers):
    headers, _ = auth_headers
    response = await client.put(
        "/api/v1/workflows/settings",
        headers=headers,
        json={"quiet_start_minute": 60},
    )
    assert response.status_code == 422
