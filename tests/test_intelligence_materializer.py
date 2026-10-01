"""Safety, scope, and idempotency tests for materialized suggestions."""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

from app.models.action_item import ActionSuggestion, FollowUp, Reminder, Task
from app.models.normalized_event import NormalizedEvent
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMembership
from app.schemas.intelligence import IntelligenceResult, IntelligenceType
from app.services.intelligence_materializer import materialize_results


def _result(kind: IntelligenceType, *, confidence: float = 0.95, due_at=None) -> IntelligenceResult:
    return IntelligenceResult(
        type=kind,
        title=f"{kind.value} title",
        summary="Validated test summary",
        confidence=confidence,
        due_at=due_at,
    )


async def _persisted_event(session, *, add_membership: bool = True):
    workspace = Workspace(name="Materializer workspace")
    user = User(
        name="Materializer user",
        email=f"materializer-{uuid.uuid4().hex}@example.test",
        hashed_password="not-a-real-password",
        workspace_id=workspace.id,
    )
    session.add(workspace)
    session.add(user)
    await session.flush()
    if add_membership:
        session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="OWNER"))
    event = NormalizedEvent(
        workspace_id=workspace.id,
        user_id=user.id,
        source="audio",
        type="AUDIO_TRANSCRIBED",
        external_id=str(uuid.uuid4()),
        content="source content is not used by materializer",
        occurred_at=datetime.now(timezone.utc),
        metadata_json={},
    )
    session.add(event)
    await session.commit()
    await session.refresh(event)
    return workspace, user, event


async def test_multiple_result_types_materialize_safely_and_retries_are_idempotent(db_session):
    workspace, user, event = await _persisted_event(db_session)
    results = [
        _result(IntelligenceType.TASK, due_at=datetime(2026, 8, 1, 12, tzinfo=timezone.utc)),
        _result(IntelligenceType.REMINDER, due_at=datetime(2026, 8, 2, 9, tzinfo=timezone.utc)),
        _result(IntelligenceType.FOLLOW_UP),
        _result(IntelligenceType.CALENDAR_EVENT, due_at=datetime(2026, 8, 3, 10, tzinfo=timezone.utc)),
        _result(IntelligenceType.ACTION),
        _result(IntelligenceType.REMINDER),  # no due_at: no reminder created
        _result(IntelligenceType.MEMORY),
        _result(IntelligenceType.INFORMATION),
        _result(IntelligenceType.NOTE),
        _result(IntelligenceType.NOT_ACTIONABLE),
        _result(IntelligenceType.TASK, confidence=0.1),
    ]

    first = await materialize_results(
        db_session, event, results, timezone="America/Sao_Paulo", minimum_confidence=0.5
    )
    second = await materialize_results(
        db_session, event, results, timezone="America/Sao_Paulo", minimum_confidence=0.5
    )

    assert len(first) == len(second) == 5
    tasks = list((await db_session.exec(select(Task))).all())
    reminders = list((await db_session.exec(select(Reminder))).all())
    suggestions = list((await db_session.exec(select(ActionSuggestion))).all())
    follow_ups = list((await db_session.exec(select(FollowUp))).all())
    assert len(tasks) == 1 and tasks[0].status == "suggested"
    assert tasks[0].workspace_id == workspace.id and tasks[0].user_id == user.id
    assert len(reminders) == 1 and reminders[0].status == "suggested"
    assert reminders[0].remind_at == reminders[0].due_at
    assert len(suggestions) == 3
    assert {item.suggestion_type for item in suggestions} == {"follow_up", "calendar_event", "action"}
    assert all(item.status == "suggested" for item in suggestions)
    assert follow_ups == []  # no unanswered message is inferred from a single event


async def test_scope_is_taken_from_persisted_event_and_membership_is_required(db_session):
    workspace, user, event = await _persisted_event(db_session, add_membership=False)
    event.workspace_id = uuid.uuid4()
    event.user_id = uuid.uuid4()

    with pytest.raises(PermissionError):
        await materialize_results(db_session, event, [_result(IntelligenceType.TASK)], timezone="UTC")

    # No item was created using untrusted in-memory workspace/user values.
    assert list((await db_session.exec(select(Task))).all()) == []


async def test_naive_due_at_and_malformed_scope_are_rejected(db_session):
    _, _, event = await _persisted_event(db_session)
    malformed = _result(IntelligenceType.TASK).model_copy(update={"due_at": datetime(2026, 8, 1)})
    with pytest.raises(ValueError, match="due_at"):
        await materialize_results(db_session, event, [malformed], timezone="UTC")
    with pytest.raises(ValueError, match="timezone"):
        await materialize_results(db_session, event, [], timezone="Not/AZone")


async def test_materializer_does_not_commit(db_session, monkeypatch):
    _, _, event = await _persisted_event(db_session)
    commit = AsyncMock()
    monkeypatch.setattr(db_session, "commit", commit)
    await materialize_results(db_session, event, [_result(IntelligenceType.TASK)], timezone="UTC")

    commit.assert_not_awaited()
