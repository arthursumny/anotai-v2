"""Delayed ARQ reminder/follow-up scheduling and durable recovery tests."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app.models.action_execution import ActionExecution
from app.models.action_item import FollowUp, Reminder
from app.models.note import Note, NoteStatus
from app.models.user import User
from app.models.workflow import Workflow, WorkflowExecution, WorkflowStep
from app.models.workspace import Workspace, WorkspaceMembership
from app.services.reminder_scheduler import (
    process_followup_due,
    process_reminder_due,
    reconcile_due_items,
    schedule_followup,
    schedule_reminder,
)
from app.services.integration_service import IntegrationService
from app import worker
from app.worker import WorkerSettings, reconcile_due_items as reconcile_worker


class FakeRedis:
    def __init__(self, *, raises=False):
        self.raises = raises
        self.calls = []

    async def enqueue_job(self, name, *args, **kwargs):
        self.calls.append((name, args, kwargs))
        if self.raises:
            raise RuntimeError("redis unavailable")
        return None


async def _owner(session):
    workspace = Workspace(name="Scheduler")
    session.add(workspace)
    await session.flush()
    user = User(
        name="Scheduler owner", email=f"{uuid.uuid4()}@example.com", hashed_password="unused",
        workspace_id=workspace.id,
    )
    session.add(user)
    await session.flush()
    session.add(WorkspaceMembership(
        workspace_id=workspace.id, user_id=user.id, role="OWNER",
    ))
    await session.flush()
    return workspace, user


async def _trigger_workflow(session, workspace, user, trigger_type, *, confidence=0.8):
    workflow = Workflow(
        workspace_id=workspace.id,
        user_id=user.id,
        title=f"{trigger_type} workflow",
        trigger_type=trigger_type,
        conditions={"event_type": trigger_type, "minimum_confidence": 0.5},
        enabled=True,
    )
    session.add(workflow)
    await session.flush()
    step = WorkflowStep(
        workflow_id=workflow.id,
        position=0,
        action_type="CREATE_TASK",
        input_template={"title": "Review due item"},
        enabled=True,
        require_confirmation=True,
    )
    session.add(step)
    await session.flush()
    return workflow


async def _reminder(session, workspace, user, remind_at, *, status="scheduled", confidence=0.8):
    reminder = Reminder(
        workspace_id=workspace.id,
        user_id=user.id,
        idempotency_key=f"rem-{uuid.uuid4()}",
        title="Review",
        confidence=confidence,
        status=status,
        remind_at=remind_at,
        timezone=workspace.timezone,
    )
    session.add(reminder)
    await session.flush()
    return reminder


async def _followup(
    session, workspace, user, remind_at, *, status="waiting", response_status="pending",
):
    followup = FollowUp(
        workspace_id=workspace.id,
        user_id=user.id,
        idempotency_key=f"follow-{uuid.uuid4()}",
        title="Check in",
        confidence=0.75,
        status=status,
        response_status=response_status,
        remind_at=remind_at,
        timezone=workspace.timezone,
    )
    session.add(followup)
    await session.flush()
    return followup


async def test_schedule_uses_arq_defer_and_stable_item_target_job_id(db_session):
    workspace, user = await _owner(db_session)
    target = datetime(2032, 1, 2, 3, 4, tzinfo=timezone(timedelta(hours=2)))
    reminder = await _reminder(db_session, workspace, user, target)
    redis = FakeRedis()

    job_id = await schedule_reminder(reminder.id, target, redis=redis)

    expected = "2032-01-02T01:04:00+00:00"
    assert job_id == f"reminder_due_job:{reminder.id}:{expected}"
    assert redis.calls == [(
        "reminder_due_job",
        (str(reminder.id), expected),
        {"_defer_until": datetime(2032, 1, 2, 1, 4, tzinfo=timezone.utc), "_job_id": job_id},
    )]

    followup = await _followup(db_session, workspace, user, target)
    follow_job = await schedule_followup(followup.id, target, redis=redis)
    assert follow_job == f"followup_due_job:{followup.id}:{expected}"
    assert redis.calls[-1][0] == "followup_due_job"


async def test_redis_failure_is_best_effort_and_keeps_sql_scheduled_state(db_session):
    workspace, user = await _owner(db_session)
    target = datetime.now(timezone.utc) + timedelta(hours=1)
    reminder = await _reminder(db_session, workspace, user, target)
    result = await schedule_reminder(reminder.id, target, redis=FakeRedis(raises=True))
    assert result is None
    await db_session.refresh(reminder)
    assert reminder.status == "scheduled"


async def test_due_reminder_emits_trigger_and_marks_due_atomically(db_session, monkeypatch):
    workspace, user = await _owner(db_session)
    await _trigger_workflow(db_session, workspace, user, "REMINDER_DUE")
    now = datetime.now(timezone.utc)
    reminder = await _reminder(db_session, workspace, user, now - timedelta(minutes=1))
    provider_calls = []

    async def forbidden_provider_call(*args, **kwargs):
        provider_calls.append((args, kwargs))

    monkeypatch.setattr(IntegrationService, "execute_claimed", forbidden_provider_call)
    result = await process_reminder_due(db_session, reminder.id, reminder.remind_at, now=now)

    assert result["status"] == "due"
    assert reminder.status == "due"
    workflow_runs = (await db_session.exec(select(WorkflowExecution))).all()
    action_runs = (await db_session.exec(select(ActionExecution))).all()
    assert len(workflow_runs) == 1
    assert workflow_runs[0].trigger_type == "REMINDER_DUE"
    assert workflow_runs[0].source_id == str(reminder.id)
    assert workflow_runs[0].metadata_json["event_type"] == "REMINDER_DUE"
    assert len(action_runs) == 1 and action_runs[0].status == "awaiting_confirmation"
    assert provider_calls == []

    duplicate = await process_reminder_due(db_session, reminder.id, reminder.remind_at, now=now)
    assert duplicate["status"] == "noop"
    assert len((await db_session.exec(select(WorkflowExecution))).all()) == 1


@pytest.mark.parametrize("case", ["early", "cancelled", "rescheduled"])
async def test_stale_early_or_cancelled_reminders_are_noops(db_session, case):
    workspace, user = await _owner(db_session)
    future = datetime.now(timezone.utc) + timedelta(hours=2)
    reminder = await _reminder(db_session, workspace, user, future)
    old_expected = future
    if case == "cancelled":
        reminder.status = "cancelled"
    elif case == "rescheduled":
        reminder.remind_at = future + timedelta(hours=1)
    await db_session.flush()

    result = await process_reminder_due(
        db_session, reminder.id, old_expected, now=datetime.now(timezone.utc),
    )
    assert result["status"] == "noop"
    assert reminder.status in {"scheduled", "cancelled"}
    assert (await db_session.exec(select(WorkflowExecution))).all() == []


@pytest.mark.parametrize(
    "status,response_status",
    [("resolved", "resolved"), ("cancelled", "pending"), ("waiting", "resolved")],
)
async def test_resolved_or_cancelled_followups_are_noops(db_session, status, response_status):
    workspace, user = await _owner(db_session)
    due = datetime.now(timezone.utc) - timedelta(minutes=1)
    followup = await _followup(
        db_session, workspace, user, due, status=status, response_status=response_status,
    )

    result = await process_followup_due(db_session, followup.id, due, now=datetime.now(timezone.utc))
    assert result["status"] == "noop"
    assert (await db_session.exec(select(WorkflowExecution))).all() == []


async def test_due_followup_emits_only_consultive_trigger_and_is_idempotent(db_session, monkeypatch):
    workspace, user = await _owner(db_session)
    await _trigger_workflow(db_session, workspace, user, "FOLLOW_UP_DUE")
    now = datetime.now(timezone.utc)
    followup = await _followup(db_session, workspace, user, now - timedelta(seconds=2))
    provider_calls = []

    async def forbidden_provider_call(*args, **kwargs):
        provider_calls.append((args, kwargs))

    monkeypatch.setattr(IntegrationService, "execute_claimed", forbidden_provider_call)
    result = await process_followup_due(db_session, followup.id, followup.remind_at, now=now)
    assert result["status"] == "due"
    assert followup.status == "due" and followup.response_status == "pending"
    assert len((await db_session.exec(select(WorkflowExecution))).all()) == 1
    assert provider_calls == []
    assert (await process_followup_due(
        db_session, followup.id, followup.remind_at, now=now,
    ))["status"] == "noop"


async def test_reconcile_scans_only_bounded_durable_due_items(db_session):
    workspace, user = await _owner(db_session)
    await _trigger_workflow(db_session, workspace, user, "REMINDER_DUE")
    now = datetime.now(timezone.utc)
    due_a = await _reminder(db_session, workspace, user, now - timedelta(minutes=2))
    due_b = await _reminder(db_session, workspace, user, now - timedelta(minutes=1))
    future = await _reminder(db_session, workspace, user, now + timedelta(hours=1))
    ignored_status = await _reminder(db_session, workspace, user, now - timedelta(minutes=3), status="suggested")
    followup = await _followup(db_session, workspace, user, now - timedelta(minutes=2))
    resolved = await _followup(
        db_session, workspace, user, now - timedelta(minutes=4), status="resolved", response_status="resolved",
    )
    # Existing legacy Notes and AI suggestions are intentionally not scanned.
    note = Note(
        user_id=user.id, title="Legacy due note", raw_transcription="legacy",
        status=NoteStatus.PROCESSED, reminder_at=now - timedelta(minutes=5),
    )
    session_suggestion = await _followup(
        db_session, workspace, user, now - timedelta(minutes=4), status="suggested",
    )
    db_session.add(note)
    await db_session.flush()

    result = await reconcile_due_items(db_session, batch_size=1)
    assert result == {"reminders_scanned": 1, "followups_scanned": 1, "processed": 2, "failed": 0}
    assert len((await db_session.exec(select(WorkflowExecution))).all()) == 1
    assert due_a.status == "due" and followup.status == "due"
    assert due_b.status == "scheduled" and future.status == "scheduled"
    assert ignored_status.status == "suggested"
    assert resolved.status == "resolved" and session_suggestion.status == "suggested"
    assert note.reminder_at is not None


async def test_reconcile_isolates_poison_reminder_and_continues_across_workspaces(db_session, monkeypatch):
    workspace_a, user_a = await _owner(db_session)
    workspace_b, user_b = await _owner(db_session)
    now = datetime.now(timezone.utc)
    poison = await _reminder(db_session, workspace_a, user_a, now - timedelta(minutes=2))
    valid = await _reminder(db_session, workspace_b, user_b, now - timedelta(minutes=1))
    poison_id, valid_id = poison.id, valid.id
    original_title = poison.title
    await db_session.commit()
    from app.services import reminder_scheduler

    emit = reminder_scheduler.emit_trigger

    async def poison_once(session, workspace_id, user_id, event_type, *, source_id, context):
        if source_id == poison_id:
            raise RuntimeError("sensitive payload must not be logged")
        return await emit(
            session, workspace_id, user_id, event_type, source_id=source_id, context=context,
        )

    monkeypatch.setattr(reminder_scheduler, "emit_trigger", poison_once)
    result = await reconcile_due_items(db_session)

    assert result == {"reminders_scanned": 2, "followups_scanned": 0, "processed": 1, "failed": 1}
    poison = await db_session.get(Reminder, poison_id)
    valid = await db_session.get(Reminder, valid_id)
    assert poison.title == original_title and poison.status == "scheduled"
    assert valid.status == "due"
    # Rollback left the SQL session usable for subsequent work.
    assert len((await db_session.exec(select(Reminder))).all()) == 2


async def test_reminder_due_worker_job_does_not_raise_on_item_error(db_session, monkeypatch):
    class SessionContext:
        async def __aenter__(self):
            return db_session

        async def __aexit__(self, *args):
            return False

    async def broken_process(*args, **kwargs):
        raise RuntimeError("sensitive payload must not be logged")

    monkeypatch.setattr(worker, "async_session_factory", lambda: SessionContext())
    monkeypatch.setattr(worker, "process_reminder_due", broken_process)

    result = await worker.reminder_due_job({}, str(uuid.uuid4()), "2030-01-01T00:00:00+00:00")
    assert result == {"ok": False, "error": "processing_failed"}


def test_worker_registers_delayed_jobs_and_unique_reconciliation_cron():
    names = {function.__name__ for function in WorkerSettings.functions}
    assert {"reminder_due_job", "followup_due_job", "reconcile_due_items"} <= names
    cron_jobs = WorkerSettings.cron_jobs
    assert any(job.coroutine is reconcile_worker and job.unique for job in cron_jobs)
    # The existing two-minute legacy Note reminder cron remains registered.
    assert any(job.coroutine.__name__ == "check_scheduled_reminders" for job in cron_jobs)
