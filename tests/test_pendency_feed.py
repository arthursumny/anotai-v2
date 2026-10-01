"""Authenticated, scoped, read-only pendency feed tests."""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app.models.action_execution import ActionExecution
from app.models.action_item import ActionSuggestion, FollowUp, Reminder, Task
from app.models.normalized_event import NormalizedEvent
from app.models.note import Note, NoteStatus
from app.models.user import User
from app.models.workspace import WorkspaceMembership


async def _audio_note(session, user, *, cards=None, reminder_at=None):
    note = Note(
        user_id=user.id,
        title="Suggested note",
        raw_transcription="PRIVATE TRANSCRIPTION SHOULD NOT LEAK",
        structured_data={"kanban_task_ids": cards} if cards is not None else {},
        status=NoteStatus.SUGGESTED,
        reminder_at=reminder_at,
    )
    session.add(note)
    await session.flush()
    event = NormalizedEvent(
        workspace_id=user.workspace_id,
        user_id=user.id,
        source="audio",
        type="AUDIO_TRANSCRIBED",
        external_id=str(note.id),
        content="PRIVATE EVENT CONTENT SHOULD NOT LEAK",
        occurred_at=datetime.now(timezone.utc),
        metadata_json={},
    )
    session.add(event)
    await session.flush()
    return note, event


def _item_by_kind(response, kind):
    return [item for item in response.json()["items"] if item["kind"] == kind]


async def test_feed_returns_mixed_safe_workspace_items(client, auth_headers, db_session):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    assert user is not None
    note, event = await _audio_note(db_session, user)
    now = datetime.now(timezone.utc)
    db_session.add_all([
        Task(workspace_id=user.workspace_id, user_id=user.id, source_event_id=event.id,
             idempotency_key="task1", title="Task title", summary="Task summary", status="suggested"),
        Reminder(workspace_id=user.workspace_id, user_id=user.id, source_event_id=event.id,
                 idempotency_key="rem1", title="Reminder title", summary="Reminder summary",
                 status="suggested", remind_at=now + timedelta(days=1)),
        FollowUp(workspace_id=user.workspace_id, user_id=user.id, idempotency_key="follow1",
                 title="Follow-up title", summary="Follow-up summary", status="open",
                 response_status="pending"),
        ActionSuggestion(workspace_id=user.workspace_id, user_id=user.id, idempotency_key="action1",
                          title="Action title", summary="Action summary", status="suggested"),
        ActionSuggestion(workspace_id=user.workspace_id, user_id=user.id, idempotency_key="followup-suggestion1",
                          title="Suggested follow-up", summary="Suggested follow-up summary",
                          status="suggested", suggestion_type="follow_up"),
        ActionExecution(workspace_id=user.workspace_id, user_id=user.id, trigger_type="manual",
                        action_type="send_email", idempotency_key="approval1", status="awaiting_confirmation",
                        action_input={"secret": "DO NOT RETURN"},
                        policy_snapshot={"private": "DO NOT RETURN"}),
    ])
    await db_session.commit()

    response = await client.get("/api/v1/pendencies", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert {item["kind"] for item in body["items"]} == {
        "note_suggestion", "task", "reminder", "follow_up", "action", "approval",
    }
    assert body["total"] == 7
    payload = response.text
    for private in ("PRIVATE TRANSCRIPTION", "PRIVATE EVENT CONTENT", "DO NOT RETURN"):
        assert private not in payload
    approval = _item_by_kind(response, "approval")[0]
    assert approval["title"] == "send_email"
    assert approval["summary"] == "Requires confirmation"
    assert note.raw_transcription not in payload
    follow_ups = _item_by_kind(response, "follow_up")
    assert {(item["title"], item["subkind"]) for item in follow_ups} == {
        ("Follow-up title", "active"),
        ("Suggested follow-up", "suggestion"),
    }
    assert _item_by_kind(response, "action")[0]["subkind"] is None
    assert _item_by_kind(response, "task")[0]["subkind"] is None
    assert _item_by_kind(response, "task")[0]["source"] == "audio"
    assert all(item["source"] is None for item in follow_ups)


async def test_feed_excludes_cross_user_rows_and_future_snoozes(client, auth_headers, db_session):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    assert user is not None and user.workspace_id is not None
    other = User(
        name="Other member", email=f"other-{uuid.uuid4().hex}@example.test",
        hashed_password="unused", workspace_id=user.workspace_id,
    )
    db_session.add(other)
    await db_session.flush()
    db_session.add(WorkspaceMembership(workspace_id=user.workspace_id, user_id=other.id, role="MEMBER"))
    db_session.add_all([
        ActionSuggestion(workspace_id=user.workspace_id, user_id=other.id,
                         idempotency_key="other-followup-suggestion", title="Other suggestion",
                         status="suggested", suggestion_type="follow_up"),
        FollowUp(workspace_id=user.workspace_id, user_id=other.id,
                 idempotency_key="other-followup-active", title="Other active follow-up",
                 status="open", response_status="pending"),
        Task(workspace_id=user.workspace_id, user_id=other.id, idempotency_key="other-task",
             title="Cross-user", status="suggested"),
        Task(workspace_id=user.workspace_id, user_id=user.id, idempotency_key="snoozed-task",
             title="Snoozed", status="suggested", snoozed_until=datetime.now(timezone.utc) + timedelta(days=1)),
        Task(workspace_id=user.workspace_id, user_id=user.id, idempotency_key="visible-task",
             title="Visible", status="suggested"),
    ])
    await db_session.commit()

    response = await client.get("/api/v1/pendencies", headers=headers)

    assert response.status_code == 200
    titles = [item["title"] for item in response.json()["items"]]
    assert titles == ["Visible"]


async def test_feed_deduplicates_legacy_audio_rows_without_title_matching(client, auth_headers, db_session):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    assert user is not None and user.workspace_id is not None
    cards_note, cards_event = await _audio_note(db_session, user, cards=["kanban-id"])
    legacy_reminder_note, legacy_event = await _audio_note(
        db_session, user, reminder_at=datetime.now(timezone.utc) + timedelta(days=2),
    )
    reminder_with_cards_note, reminder_with_cards_event = await _audio_note(
        db_session, user, cards=["kanban-id-2"],
    )
    db_session.add_all([
        Task(workspace_id=user.workspace_id, user_id=user.id, source_event_id=cards_event.id,
             idempotency_key="legacy-task", title="Not matched by title", status="suggested"),
        Task(workspace_id=user.workspace_id, user_id=user.id, source_event_id=legacy_event.id,
             idempotency_key="unrelated-task", title="Task without cards", status="suggested"),
        Reminder(workspace_id=user.workspace_id, user_id=user.id, source_event_id=legacy_event.id,
                 idempotency_key="legacy-reminder", title="Suppressed legacy reminder", status="suggested",
                 remind_at=datetime.now(timezone.utc) + timedelta(days=2)),
        Reminder(workspace_id=user.workspace_id, user_id=user.id, source_event_id=reminder_with_cards_event.id,
                 idempotency_key="reminder-with-kanban", title="Reminder alongside Kanban", status="suggested",
                 remind_at=datetime.now(timezone.utc) + timedelta(days=2)),
    ])
    await db_session.commit()

    response = await client.get("/api/v1/pendencies", headers=headers)

    assert response.status_code == 200
    kinds_titles = {(item["kind"], item["title"]) for item in response.json()["items"]}
    assert ("task", "Not matched by title") not in kinds_titles
    assert ("task", "Task without cards") in kinds_titles
    assert ("reminder", "Suppressed legacy reminder") not in kinds_titles
    assert ("reminder", "Reminder alongside Kanban") in kinds_titles
    assert cards_note.id
    assert legacy_reminder_note.id
    assert reminder_with_cards_note.id


async def test_feed_global_pagination_and_bounds(client, auth_headers, db_session):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    assert user is not None and user.workspace_id is not None
    for index in range(3):
        db_session.add(Task(
            workspace_id=user.workspace_id, user_id=user.id, idempotency_key=f"page-{index}",
            title=f"Task {index}", status="suggested",
            created_at=datetime(2026, 1, index + 1, tzinfo=timezone.utc),
        ))
    await db_session.commit()

    first = await client.get("/api/v1/pendencies?limit=2&offset=0", headers=headers)
    second = await client.get("/api/v1/pendencies?limit=2&offset=2", headers=headers)
    invalid = await client.get("/api/v1/pendencies?limit=101", headers=headers)

    assert first.status_code == second.status_code == 200
    assert first.json()["total"] == second.json()["total"] == 3
    assert [i["title"] for i in first.json()["items"]] == ["Task 2", "Task 1"]
    assert [i["title"] for i in second.json()["items"]] == ["Task 0"]
    assert invalid.status_code == 422


async def test_feed_distinguishes_followup_suggestions_and_active_followups(
    client, auth_headers, db_session,
):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    suggestion = ActionSuggestion(
        workspace_id=user.workspace_id, user_id=user.id,
        idempotency_key="feed-followup-suggestion", title="Suggested follow-up",
        summary="Safe suggestion summary", status="suggested", suggestion_type="follow_up",
    )
    follow_up = FollowUp(
        workspace_id=user.workspace_id, user_id=user.id,
        idempotency_key="feed-due-followup", title="Due follow-up",
        summary="Safe follow-up summary", status="due", response_status="pending",
    )
    db_session.add_all([suggestion, follow_up])
    await db_session.commit()

    response = await client.get("/api/v1/pendencies", headers=headers)

    assert response.status_code == 200
    results = {item["id"]: item for item in response.json()["items"]}
    assert results[str(suggestion.id)]["kind"] == "follow_up"
    assert results[str(follow_up.id)]["kind"] == "follow_up"
    assert results[str(suggestion.id)]["subkind"] == "suggestion"
    assert results[str(follow_up.id)]["subkind"] == "active"
    assert results[str(suggestion.id)]["summary"] == "Safe suggestion summary"
