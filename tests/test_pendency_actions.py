"""Acceptance endpoints persist only scoped, safe Task and Reminder actions."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app.models.action_execution import ActionExecution
from app.models.action_item import ActionSuggestion, FollowUp, Reminder, Task
from app.models.normalized_event import NormalizedEvent
from app.models.note import Note, NoteStatus
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMembership


async def _audio_source(db_session, user, *, cards=None, reminder_at=None):
    note = Note(
        user_id=user.id,
        title="Audio note",
        body="Audio note body written by the capture flow",
        raw_transcription="SENSITIVE RAW TRANSCRIPTION",
        structured_data={"kanban_task_ids": cards} if cards is not None else {},
        status=NoteStatus.PROCESSED,
        reminder_at=reminder_at,
    )
    db_session.add(note)
    await db_session.flush()
    event = NormalizedEvent(
        workspace_id=user.workspace_id, user_id=user.id, source="audio",
        type="AUDIO_TRANSCRIBED", external_id=str(note.id),
        content="SENSITIVE EVENT CONTENT", occurred_at=datetime.now(timezone.utc), metadata_json={},
    )
    db_session.add(event)
    await db_session.flush()
    return note, event


async def _followup_suggestion(db_session, user, *, direction="incoming", conversation="thread-1",
                               sender="contact-1", due_at=None):
    event = NormalizedEvent(
        workspace_id=user.workspace_id, user_id=user.id, source="whatsapp",
        type="MESSAGE_RECEIVED", external_id=f"evt-{uuid.uuid4()}",
        conversation_id=conversation, sender=sender, content="private event content",
        occurred_at=datetime.now(timezone.utc), metadata_json={"direction": direction},
    )
    suggestion = ActionSuggestion(
        workspace_id=user.workspace_id, user_id=user.id, source_event_id=event.id,
        idempotency_key=f"follow-suggestion-{uuid.uuid4()}", title="Follow up title",
        summary="Safe summary", status="suggested", suggestion_type="follow_up", due_at=due_at,
    )
    db_session.add(event)
    await db_session.flush()
    db_session.add(suggestion)
    await db_session.flush()
    return event, suggestion


async def test_task_accept_twice_preserves_native_task_and_source(client, auth_headers, db_session):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    source_note, event = await _audio_source(db_session, user)
    task = Task(
        workspace_id=user.workspace_id, user_id=user.id, source_event_id=event.id, source_note_id=source_note.id,
        idempotency_key="accept-task", title="Suggested task", status="suggested",
    )
    db_session.add(task)
    await db_session.commit()

    url = f"/api/v1/pendencies/task/{task.id}/accept"
    first = await client.post(url, headers=headers, json={"title": "Accepted title"})
    second = await client.post(url, headers=headers, json={"title": "Changed on retry"})

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"kind": "task", "id": str(task.id), "status": "open"}
    await db_session.refresh(task)
    assert task.title == "Accepted title"
    assert task.status == "open" and task.kanban_status == "todo"
    assert task.source_note_id == source_note.id and task.source_event_id == event.id
    tasks = (await db_session.exec(select(Task).where(Task.user_id == user.id))).all()
    assert [row.id for row in tasks] == [task.id]
    notes = (await db_session.exec(select(Note).where(Note.user_id == user.id))).all()
    assert len(notes) == 1
    assert notes[0].id == source_note.id
    assert source_note.raw_transcription == "SENSITIVE RAW TRANSCRIPTION"


async def test_reminder_accept_twice_schedules_once(client, auth_headers, db_session, monkeypatch):
    from app.api.v1 import pendencies

    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    reminder = Reminder(
        workspace_id=user.workspace_id, user_id=user.id, idempotency_key="accept-reminder",
        title="Suggested reminder", status="suggested",
        remind_at=datetime.now(timezone.utc) + timedelta(days=2),
    )
    db_session.add(reminder)
    await db_session.commit()
    calls = []

    async def schedule(item_id, remind_at):
        calls.append((item_id, remind_at))

    monkeypatch.setattr(pendencies, "schedule_reminder", schedule)
    target = datetime.now(timezone.utc) + timedelta(days=3)
    url = f"/api/v1/pendencies/reminder/{reminder.id}/accept"
    first = await client.post(url, headers=headers, json={"remind_at": target.isoformat()})
    second = await client.post(url, headers=headers, json={"remind_at": target.isoformat()})

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"kind": "reminder", "id": str(reminder.id), "status": "scheduled"}
    assert len(calls) == 1
    assert calls[0][0] == reminder.id


async def test_redis_schedule_failure_does_not_undo_reminder(client, auth_headers, db_session, monkeypatch):
    from app.api.v1 import pendencies

    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    target = datetime.now(timezone.utc) + timedelta(days=1)
    reminder = Reminder(
        workspace_id=user.workspace_id, user_id=user.id, idempotency_key="redis-failure",
        title="Reminder", status="suggested", remind_at=target,
    )
    db_session.add(reminder)
    await db_session.commit()

    async def fail(*args, **kwargs):
        raise ConnectionError("redis unavailable")

    monkeypatch.setattr(pendencies, "schedule_reminder", fail)
    response = await client.post(
        f"/api/v1/pendencies/reminder/{reminder.id}/accept", headers=headers,
        json={"remind_at": target.isoformat()},
    )
    await db_session.refresh(reminder)
    assert response.status_code == 200
    assert reminder.status == "scheduled"


async def test_accept_is_scoped_and_naive_remind_at_rejected(client, auth_headers, db_session):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    foreign_workspace = Workspace(name="Foreign workspace")
    other = User(name="Other", email=f"{uuid.uuid4()}@example.test", hashed_password="unused")
    db_session.add_all([foreign_workspace, other])
    await db_session.flush()
    other.workspace_id = foreign_workspace.id
    db_session.add(WorkspaceMembership(workspace_id=foreign_workspace.id, user_id=other.id))
    foreign = Task(
        workspace_id=foreign_workspace.id, user_id=other.id, idempotency_key="foreign",
        title="Foreign task", status="suggested",
    )
    own_reminder = Reminder(
        workspace_id=user.workspace_id, user_id=user.id, idempotency_key="naive-time",
        title="Reminder", status="suggested", remind_at=datetime.now(timezone.utc),
    )
    db_session.add_all([foreign, own_reminder])
    await db_session.commit()

    cross_user = await client.post(f"/api/v1/pendencies/task/{foreign.id}/accept", headers=headers)
    naive = await client.post(
        f"/api/v1/pendencies/reminder/{own_reminder.id}/accept", headers=headers,
        json={"remind_at": "2030-01-01T12:00:00"},
    )
    assert cross_user.status_code == 404
    assert naive.status_code == 422
    await db_session.refresh(foreign)
    await db_session.refresh(own_reminder)
    assert foreign.status == own_reminder.status == "suggested"


async def test_direct_task_accept_ignores_legacy_source_card_metadata(client, auth_headers, db_session):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    _, event = await _audio_source(db_session, user, cards=["existing-card"])
    task = Task(
        workspace_id=user.workspace_id, user_id=user.id, source_event_id=event.id,
        idempotency_key="duplicate-card", title="Duplicate", status="suggested",
    )
    db_session.add(task)
    await db_session.commit()

    response = await client.post(f"/api/v1/pendencies/task/{task.id}/accept", headers=headers)
    await db_session.refresh(task)
    assert response.status_code == 200
    assert task.status == "open" and task.kanban_status == "todo"
    notes = (await db_session.exec(select(Note).where(Note.user_id == user.id))).all()
    assert len(notes) == 1


async def test_ignore_is_idempotent_and_rejects_non_suggested_task(client, auth_headers, db_session):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    task = Task(
        workspace_id=user.workspace_id, user_id=user.id, idempotency_key="ignore-task",
        title="Ignore me", status="suggested",
    )
    accepted = Task(
        workspace_id=user.workspace_id, user_id=user.id, idempotency_key="ignore-accepted",
        title="Already accepted", status="open",
    )
    db_session.add_all([task, accepted])
    await db_session.commit()

    first = await client.post(f"/api/v1/pendencies/task/{task.id}/ignore", headers=headers)
    second = await client.post(f"/api/v1/pendencies/task/{task.id}/ignore", headers=headers)
    no_longer_suggested = await client.post(
        f"/api/v1/pendencies/task/{accepted.id}/ignore", headers=headers,
    )

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"kind": "task", "id": str(task.id), "status": "dismissed"}
    assert no_longer_suggested.status_code == 409
    await db_session.refresh(task)
    assert task.status == "dismissed"


async def test_snooze_hides_item_until_expiry_and_rejects_past_or_naive(client, auth_headers, db_session):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    action = ActionSuggestion(
        workspace_id=user.workspace_id, user_id=user.id, idempotency_key="snooze-action",
        title="Later", status="suggested",
    )
    db_session.add(action)
    await db_session.commit()
    until = datetime.now(timezone.utc) + timedelta(days=1)

    accepted = await client.post(
        f"/api/v1/pendencies/action/{action.id}/snooze", headers=headers,
        json={"until": until.isoformat()},
    )
    past = await client.post(
        f"/api/v1/pendencies/action/{action.id}/snooze", headers=headers,
        json={"until": (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()},
    )
    naive = await client.post(
        f"/api/v1/pendencies/action/{action.id}/snooze", headers=headers,
        json={"until": "2030-01-01T12:00:00"},
    )
    feed = await client.get("/api/v1/pendencies", headers=headers)

    assert accepted.status_code == 200
    assert past.status_code == naive.status_code == 422
    assert all(item["id"] != str(action.id) for item in feed.json()["items"])


async def test_edit_updates_task_and_reminder_before_acceptance(client, auth_headers, db_session):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    task = Task(
        workspace_id=user.workspace_id, user_id=user.id, idempotency_key="edit-task",
        title="Original", status="suggested",
    )
    reminder = Reminder(
        workspace_id=user.workspace_id, user_id=user.id, idempotency_key="edit-reminder",
        title="Reminder original", status="suggested",
        remind_at=datetime.now(timezone.utc) + timedelta(days=2),
    )
    db_session.add_all([task, reminder])
    await db_session.commit()
    due_at = datetime.now(timezone.utc) + timedelta(days=4)

    edited = await client.patch(
        f"/api/v1/pendencies/task/{task.id}/edit", headers=headers,
        json={"title": "Updated title", "due_at": due_at.isoformat()},
    )
    reminder_edited = await client.patch(
        f"/api/v1/pendencies/reminder/{reminder.id}/edit", headers=headers,
        json={"title": "Updated reminder"},
    )
    feed = await client.get("/api/v1/pendencies", headers=headers)
    await db_session.refresh(task)
    await db_session.refresh(reminder)

    assert edited.status_code == reminder_edited.status_code == 200
    assert task.title == "Updated title"
    assert task.due_at.replace(tzinfo=timezone.utc) == due_at
    assert reminder.title == "Updated reminder"
    items = {item["id"]: item for item in feed.json()["items"]}
    assert items[str(task.id)]["title"] == "Updated title"
    assert items[str(task.id)]["due_at"] is not None
    assert items[str(reminder.id)]["title"] == "Updated reminder"

    accepted = await client.post(f"/api/v1/pendencies/task/{task.id}/accept", headers=headers)
    edited_after_accept = await client.patch(
        f"/api/v1/pendencies/task/{task.id}/edit", headers=headers, json={"title": "Too late"},
    )
    assert accepted.status_code == 200
    assert edited_after_accept.status_code == 409


async def test_approval_ignore_points_to_workflow_dismiss(client, auth_headers, db_session):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    approval = ActionExecution(
        workspace_id=user.workspace_id, user_id=user.id, trigger_type="manual",
        action_type="send_email", idempotency_key="approval-ignore",
        status="awaiting_confirmation", action_input={}, policy_snapshot={},
    )
    db_session.add(approval)
    await db_session.commit()

    response = await client.post(f"/api/v1/pendencies/approval/{approval.id}/ignore", headers=headers)
    assert response.status_code == 409
    assert "/workflows/executions/" in response.json()["detail"]


async def test_foreign_owner_cannot_ignore_or_edit(client, auth_headers, db_session):
    headers, user_id = auth_headers
    foreign_workspace = Workspace(name="Foreign")
    foreign_user = User(name="Foreign user", email=f"{uuid.uuid4()}@example.test", hashed_password="unused")
    db_session.add_all([foreign_workspace, foreign_user])
    await db_session.flush()
    foreign_user.workspace_id = foreign_workspace.id
    db_session.add(WorkspaceMembership(workspace_id=foreign_workspace.id, user_id=foreign_user.id))
    task = Task(
        workspace_id=foreign_workspace.id, user_id=foreign_user.id,
        idempotency_key="foreign-ignore", title="Foreign", status="suggested",
    )
    db_session.add(task)
    await db_session.commit()

    ignored = await client.post(f"/api/v1/pendencies/task/{task.id}/ignore", headers=headers)
    edited = await client.patch(
        f"/api/v1/pendencies/task/{task.id}/edit", headers=headers, json={"title": "No"},
    )
    await db_session.refresh(task)
    assert ignored.status_code == edited.status_code == 404
    assert task.status == "suggested"


async def test_due_follow_up_can_be_ignored_but_not_snoozed(client, auth_headers, db_session):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    follow_up = FollowUp(
        workspace_id=user.workspace_id, user_id=user.id, idempotency_key="due-followup",
        title="Follow up", status="due", response_status="pending",
    )
    db_session.add(follow_up)
    await db_session.commit()

    ignored = await client.post(f"/api/v1/pendencies/follow_up/{follow_up.id}/ignore", headers=headers)
    snoozed = await client.post(
        f"/api/v1/pendencies/follow_up/{follow_up.id}/snooze", headers=headers,
        json={"until": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()},
    )
    await db_session.refresh(follow_up)
    assert ignored.status_code == 200
    assert snoozed.status_code == 409
    assert follow_up.status == follow_up.response_status == "dismissed"


async def test_followup_suggestion_accept_creates_one_distinct_followup_and_schedules_once(
    client, auth_headers, db_session, monkeypatch,
):
    from app.api.v1 import pendencies

    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    target = datetime.now(timezone.utc) + timedelta(days=2)
    event, suggestion = await _followup_suggestion(db_session, user, due_at=target)
    await db_session.commit()
    calls = []

    async def schedule(item_id, remind_at):
        calls.append((item_id, remind_at))

    monkeypatch.setattr(pendencies, "schedule_followup", schedule)
    url = f"/api/v1/pendencies/follow_up/{suggestion.id}/accept"
    first = await client.post(url, headers=headers)
    second = await client.post(url, headers=headers)

    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert first.json()["kind"] == "follow_up"
    assert first.json()["status"] == "waiting"
    rows = (await db_session.exec(select(FollowUp).where(FollowUp.user_id == user.id))).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.id == uuid.UUID(first.json()["id"])
    assert row.status == "waiting" and row.response_status == "pending"
    assert row.source_event_id == event.id
    assert row.conversation_id == "thread-1" and row.contact == "contact-1"
    assert row.title == "Follow up title" and row.summary == "Safe summary"
    assert len(calls) == 1


async def test_followup_accept_rejects_outgoing_and_missing_context_or_time(client, auth_headers, db_session):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    _, outgoing = await _followup_suggestion(db_session, user, direction="outgoing", due_at=datetime.now(timezone.utc))
    _, no_conversation = await _followup_suggestion(db_session, user, conversation=None, due_at=datetime.now(timezone.utc))
    _, no_contact = await _followup_suggestion(db_session, user, sender=None, due_at=datetime.now(timezone.utc))
    _, no_time = await _followup_suggestion(db_session, user)
    await db_session.commit()

    responses = [
        await client.post(f"/api/v1/pendencies/follow_up/{outgoing.id}/accept", headers=headers),
        await client.post(f"/api/v1/pendencies/follow_up/{no_conversation.id}/accept", headers=headers),
        await client.post(f"/api/v1/pendencies/follow_up/{no_contact.id}/accept", headers=headers),
        await client.post(f"/api/v1/pendencies/follow_up/{no_time.id}/accept", headers=headers),
    ]
    assert [response.status_code for response in responses] == [409, 422, 422, 422]
    assert not (await db_session.exec(select(FollowUp).where(FollowUp.user_id == user.id))).all()


async def test_foreign_workspace_followup_suggestion_is_not_visible_to_accept(client, auth_headers, db_session):
    headers, _user_id = auth_headers
    workspace = Workspace(name="Follow-up private workspace")
    other = User(name="Other", email=f"{uuid.uuid4()}@example.test", hashed_password="unused")
    db_session.add_all([workspace, other])
    await db_session.flush()
    other.workspace_id = workspace.id
    db_session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=other.id))
    event = NormalizedEvent(
        workspace_id=workspace.id, user_id=other.id, source="whatsapp", type="MESSAGE_RECEIVED",
        external_id=f"foreign-{uuid.uuid4()}", conversation_id="foreign-thread", sender="foreign-contact",
        occurred_at=datetime.now(timezone.utc), metadata_json={},
    )
    db_session.add(event)
    await db_session.flush()
    suggestion = ActionSuggestion(
        workspace_id=workspace.id, user_id=other.id, source_event_id=event.id,
        idempotency_key="foreign-followup-suggestion", title="Private", status="suggested",
        suggestion_type="follow_up", due_at=datetime.now(timezone.utc) + timedelta(days=1),
    )
    db_session.add(suggestion)
    await db_session.commit()

    response = await client.post(f"/api/v1/pendencies/follow_up/{suggestion.id}/accept", headers=headers)

    assert response.status_code == 404
    assert not (await db_session.exec(select(FollowUp).where(FollowUp.source_event_id == event.id))).all()


async def test_followup_resolve_marks_answered_and_due_followup_can_be_snoozed(
    client, auth_headers, db_session,
):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    due = FollowUp(
        workspace_id=user.workspace_id, user_id=user.id, idempotency_key="resolve-due",
        title="Due", status="due", response_status="pending",
    )
    snoozable = FollowUp(
        workspace_id=user.workspace_id, user_id=user.id, idempotency_key="snooze-due",
        title="Snooze", status="due", response_status="pending",
    )
    db_session.add_all([due, snoozable])
    await db_session.commit()

    resolved = await client.post(f"/api/v1/pendencies/follow_up/{due.id}/resolve", headers=headers)
    target = datetime.now(timezone.utc) + timedelta(days=1)
    snoozed = await client.post(
        f"/api/v1/pendencies/follow_up/{snoozable.id}/snooze", headers=headers,
        json={"until": target.isoformat()},
    )
    await db_session.refresh(due)
    await db_session.refresh(snoozable)
    assert resolved.status_code == snoozed.status_code == 200
    assert due.status == "resolved" and due.response_status == "answered"
    assert snoozable.snoozed_until is not None


async def test_audio_sourced_task_accepts_once_with_due_at_and_keeps_source_note(
    client, auth_headers, db_session,
):
    """An audio-sourced task pendency is accepted once and stays a native Task."""
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    source_note, event = await _audio_source(db_session, user)
    task = Task(
        workspace_id=user.workspace_id, user_id=user.id,
        source_event_id=event.id, source_note_id=source_note.id,
        idempotency_key="audio-task", title="From audio", summary="Safe summary",
        status="suggested",
    )
    db_session.add(task)
    await db_session.commit()
    target = datetime(2030, 1, 2, 9, tzinfo=timezone.utc)
    url = f"/api/v1/pendencies/task/{task.id}/accept"
    first = await client.post(url, headers=headers, json={"due_at": target.isoformat()})
    second = await client.post(url, headers=headers, json={"due_at": target.isoformat()})

    await db_session.refresh(task)
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json() == {"kind": "task", "id": str(task.id), "status": "open"}
    assert task.status == "open" and task.kanban_status == "todo"
    assert task.due_at.replace(tzinfo=timezone.utc) == target
    assert task.source_note_id == source_note.id and task.source_event_id == event.id
    tasks = (await db_session.exec(select(Task).where(Task.user_id == user.id))).all()
    assert [row.id for row in tasks] == [task.id]
    notes = (await db_session.exec(select(Note).where(Note.user_id == user.id))).all()
    assert [note.id for note in notes] == [source_note.id]
    assert source_note.status == NoteStatus.PROCESSED
    assert source_note.raw_transcription == "SENSITIVE RAW TRANSCRIPTION"
    assert source_note.structured_data == {}


async def test_note_suggestion_kind_is_rejected_as_unsupported(client, auth_headers, db_session):
    """The retired triage-inbox kind must never silently come back (422 on every verb)."""
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    note = Note(
        user_id=user.id, title="Retired suggestion", body="Audio note body",
        status=NoteStatus.PROCESSED,
    )
    db_session.add(note)
    await db_session.commit()
    note_id = note.id
    target = datetime.now(timezone.utc) + timedelta(days=1)

    accept = await client.post(
        f"/api/v1/pendencies/note_suggestion/{note_id}/accept", headers=headers, json={},
    )
    ignore = await client.post(
        f"/api/v1/pendencies/note_suggestion/{note_id}/ignore", headers=headers,
    )
    snooze = await client.post(
        f"/api/v1/pendencies/note_suggestion/{note_id}/snooze", headers=headers,
        json={"until": target.isoformat()},
    )
    edit = await client.patch(
        f"/api/v1/pendencies/note_suggestion/{note_id}/edit", headers=headers,
        json={"title": "Nope"},
    )

    assert [r.status_code for r in (accept, ignore, snooze, edit)] == [422, 422, 422, 422]
    for response in (accept, ignore, snooze, edit):
        assert response.json()["detail"] == "Unsupported pendency kind"
    await db_session.refresh(note)
    assert note.status == NoteStatus.PROCESSED


async def test_followup_suggestion_is_dismissed_snoozed_and_edited_as_suggestion(client, auth_headers, db_session):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    _, suggestion = await _followup_suggestion(db_session, user, due_at=datetime.now(timezone.utc))
    await db_session.commit()
    later = datetime.now(timezone.utc) + timedelta(days=2)
    snoozed = await client.post(
        f"/api/v1/pendencies/follow_up/{suggestion.id}/snooze", headers=headers,
        json={"until": later.isoformat()},
    )
    hidden_feed = await client.get("/api/v1/pendencies", headers=headers)
    edited = await client.patch(
        f"/api/v1/pendencies/follow_up/{suggestion.id}/edit", headers=headers,
        json={"title": "Edited follow-up"},
    )
    ignored = await client.post(f"/api/v1/pendencies/follow_up/{suggestion.id}/ignore", headers=headers)
    await db_session.refresh(suggestion)
    assert snoozed.status_code == edited.status_code == ignored.status_code == 200
    assert all(item["id"] != str(suggestion.id) for item in hidden_feed.json()["items"])
    assert suggestion.title == "Edited follow-up" and suggestion.status == "dismissed"
