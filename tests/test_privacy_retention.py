import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app.models.action_item import ActionSuggestion, FollowUp, Reminder, Task
from app.models.generated_document import GeneratedDocument
from app.models.normalized_event import NormalizedEvent
from app.models.note import Note
from app.models.source_policy import SourceMonitoringPolicy
from app.models.user import User
from app.models.workspace import Workspace
from app.services.privacy_retention import purge_expired_events, purge_user_source_events


async def _owner(session, label):
    workspace = Workspace(name=label)
    user = User(name=label, email=f"{uuid.uuid4()}@example.test", hashed_password="unused")
    session.add(workspace)
    session.add(user)
    await session.flush()
    return workspace, user


def _event(workspace, user, *, created, source="audio"):
    return NormalizedEvent(
        workspace_id=workspace.id, user_id=user.id, source=source, type="capture",
        content="private", occurred_at=created, created_at=created,
    )


@pytest.mark.asyncio
async def test_expiry_policy_fairness_scope_batch_and_aware_dates(db_session):
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    wa, ua = await _owner(db_session, "long retention")
    wb, ub = await _owner(db_session, "default retention")
    db_session.add(SourceMonitoringPolicy(workspace_id=wa.id, user_id=ua.id, source="audio", retention_days=365))
    long_old = _event(wa, ua, created=now - timedelta(days=100))
    default_old = _event(wb, ub, created=now - timedelta(days=31))
    # Retention is rooted in created_at, not a source-supplied occurred_at.
    default_old.occurred_at = now - timedelta(days=900)
    db_session.add_all([long_old, default_old])
    await db_session.flush()

    result = await purge_expired_events(db_session, now=now, batch_size=1)
    assert result["events"] == 1
    assert (await db_session.get(NormalizedEvent, default_old.id)) is None
    assert await db_session.get(NormalizedEvent, long_old.id) is not None
    assert (await purge_expired_events(db_session, now=now))["events"] == 0

    with pytest.raises(ValueError):
        await purge_expired_events(db_session, now=now.replace(tzinfo=None))
    with pytest.raises(ValueError):
        await purge_expired_events(db_session, now=now, batch_size=101)


@pytest.mark.asyncio
async def test_auto_selection_remains_bounded_across_repeated_batches(db_session):
    now = datetime(2026, 9, 30, tzinfo=timezone.utc)
    workspace, user = await _owner(db_session, "many expired")
    db_session.add_all(
        [_event(workspace, user, created=now - timedelta(days=40, minutes=i)) for i in range(205)]
    )
    await db_session.flush()

    first = await purge_expired_events(db_session, now=now, batch_size=100)
    second = await purge_expired_events(db_session, now=now, batch_size=100)
    third = await purge_expired_events(db_session, now=now, batch_size=100)
    assert [first["events"], second["events"], third["events"]] == [100, 100, 5]
    assert (await purge_expired_events(db_session, now=now))["events"] == 0


@pytest.mark.asyncio
async def test_manual_purge_keyset_pages_over_more_than_one_hundred(db_session):
    now = datetime.now(timezone.utc)
    workspace, user = await _owner(db_session, "manual batch")
    db_session.add_all(
        [_event(workspace, user, created=now - timedelta(seconds=i)) for i in range(250)]
    )
    await db_session.flush()

    result = await purge_user_source_events(db_session, workspace.id, user.id, cancel_active=False)
    assert result["events"] == 250
    assert result["failed"] == 0
    assert (await purge_user_source_events(db_session, workspace.id, user.id, cancel_active=False))["events"] == 0


@pytest.mark.asyncio
async def test_auto_removes_unaccepted_children_preserves_accepted_and_notes(db_session):
    now = datetime.now(timezone.utc)
    workspace, user = await _owner(db_session, "owner")
    event = _event(workspace, user, created=now - timedelta(days=31))
    db_session.add(event)
    await db_session.flush()
    doc = GeneratedDocument(workspace_id=workspace.id, user_id=user.id, source_event_id=event.id,
                            template_version="v1", rendered_content="snapshot")
    suggested = Task(workspace_id=workspace.id, user_id=user.id, source_event_id=event.id,
                     idempotency_key="tsk", title="suggested", status="suggested")
    consumed = Task(workspace_id=workspace.id, user_id=user.id, source_event_id=event.id,
                    idempotency_key="tsk2", title="accepted", status="consumed")
    reminder = Reminder(workspace_id=workspace.id, user_id=user.id, source_event_id=event.id,
                        idempotency_key="rem", title="accepted reminder", remind_at=now,
                        status="scheduled")
    follow = FollowUp(workspace_id=workspace.id, user_id=user.id, source_event_id=event.id,
                      idempotency_key="fol", title="accepted follow", status="waiting")
    suggestion = ActionSuggestion(workspace_id=workspace.id, user_id=user.id, source_event_id=event.id,
                                  idempotency_key="sug", title="suggestion", status="suggested")
    note = Note(user_id=user.id, raw_transcription="retain this")
    db_session.add_all([doc, suggested, consumed, reminder, follow, suggestion, note])
    await db_session.flush()

    result = await purge_expired_events(db_session, now=now)
    assert result["events"] == 1
    assert await db_session.get(GeneratedDocument, doc.id) is None
    assert await db_session.get(Task, suggested.id) is None
    assert await db_session.get(ActionSuggestion, suggestion.id) is None
    assert (await db_session.get(Task, consumed.id)).source_event_id is None
    assert (await db_session.get(Reminder, reminder.id)).source_event_id is None
    assert (await db_session.get(FollowUp, follow.id)).source_event_id is None
    assert (await db_session.get(Note, note.id)).raw_transcription == "retain this"


@pytest.mark.asyncio
async def test_manual_purge_is_owner_scoped_and_cancels_active(db_session):
    now = datetime.now(timezone.utc)
    workspace, user = await _owner(db_session, "manual owner")
    other_workspace, other_user = await _owner(db_session, "other tenant")
    event = _event(workspace, user, created=now)
    foreign_event = _event(other_workspace, other_user, created=now)
    db_session.add_all([event, foreign_event])
    await db_session.flush()
    reminder = Reminder(workspace_id=workspace.id, user_id=user.id, source_event_id=event.id,
                        idempotency_key="manual-rem", title="active", remind_at=now, status="due")
    follow = FollowUp(workspace_id=workspace.id, user_id=user.id, source_event_id=event.id,
                      idempotency_key="manual-fol", title="active", status="waiting")
    task = Task(workspace_id=workspace.id, user_id=user.id, source_event_id=event.id,
                idempotency_key="manual-task", title="accepted", status="consumed")
    note = Note(user_id=user.id, raw_transcription="still here")
    db_session.add_all([reminder, follow, task, note])
    await db_session.flush()

    result = await purge_user_source_events(db_session, workspace.id, user.id, cancel_active=True)
    assert result["events"] == 1
    assert await db_session.get(NormalizedEvent, foreign_event.id) is not None
    assert await db_session.get(Reminder, reminder.id) is None
    assert await db_session.get(FollowUp, follow.id) is None
    assert (await db_session.get(Task, task.id)).source_event_id is None
    assert (await db_session.get(Note, note.id)).raw_transcription == "still here"
    assert (await purge_user_source_events(db_session, workspace.id, user.id, cancel_active=True))["events"] == 0


@pytest.mark.asyncio
async def test_manual_purge_removes_reminders_and_followups_under_apagar(db_session):
    now = datetime.now(timezone.utc)
    workspace_a, user_a = await _owner(db_session, "manual source owner")
    workspace_b, user_b = await _owner(db_session, "other source owner")
    event = _event(workspace_a, user_a, created=now)
    other_event = _event(workspace_a, user_a, created=now, source="gmail")
    other_event.external_id = str(uuid.uuid4())
    foreign_event = _event(workspace_b, user_b, created=now)
    db_session.add_all([event, other_event, foreign_event])
    await db_session.flush()

    reminders = [
        Reminder(workspace_id=workspace_a.id, user_id=user_a.id, source_event_id=event.id,
                 idempotency_key=f"manual-remove-rem-{status}", title="remove", remind_at=now, status=status)
        for status in ("scheduled", "due", "cancelled")
    ]
    follow_ups = [
        FollowUp(workspace_id=workspace_a.id, user_id=user_a.id, source_event_id=event.id,
                 idempotency_key=f"manual-remove-follow-{status}", title="remove", status=status)
        for status in ("waiting", "due", "cancelled")
    ]
    foreign_reminder = Reminder(
        workspace_id=workspace_b.id, user_id=user_b.id, source_event_id=foreign_event.id,
        idempotency_key="foreign-reminder", title="foreign", remind_at=now, status="scheduled",
    )
    detached_reminder = Reminder(
        workspace_id=workspace_a.id, user_id=user_a.id, source_event_id=None,
        idempotency_key="detached-reminder", title="detached", remind_at=now, status="scheduled",
    )
    other_event_reminder = Reminder(
        workspace_id=workspace_a.id, user_id=user_a.id, source_event_id=other_event.id,
        idempotency_key="other-event-reminder", title="other event", remind_at=now, status="scheduled",
    )
    note = Note(user_id=user_a.id, raw_transcription="retain this note")
    db_session.add_all([
        *reminders, *follow_ups, foreign_reminder, detached_reminder,
        other_event_reminder, note,
    ])
    await db_session.flush()
    reminder_ids = [reminder.id for reminder in reminders]
    follow_up_ids = [follow_up.id for follow_up in follow_ups]

    result = await purge_user_source_events(
        db_session, workspace_a.id, user_a.id, cancel_active=True,
    )

    assert result["reminders"] == 3
    assert result["follow_ups"] == 3
    for reminder_id in reminder_ids:
        assert await db_session.get(Reminder, reminder_id) is None
    for follow_up_id in follow_up_ids:
        assert await db_session.get(FollowUp, follow_up_id) is None
    assert await db_session.get(Reminder, foreign_reminder.id) is not None
    assert await db_session.get(Reminder, detached_reminder.id) is not None
    assert await db_session.get(Reminder, other_event_reminder.id) is not None
    assert (await db_session.get(Note, note.id)).raw_transcription == "retain this note"
