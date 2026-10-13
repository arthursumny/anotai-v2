"""Read-only aggregation of actionable workspace pendencies."""

import uuid
from datetime import datetime, timezone

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.action_execution import ActionExecution
from app.models.action_item import ActionSuggestion, FollowUp, Reminder, Task
from app.models.normalized_event import NormalizedEvent
from app.models.note import Note
from app.models.user import User
from app.models.workspace import WorkspaceMembership
from app.schemas.pendency import PendencyRead


def _not_snoozed(snoozed_until: datetime | None, now: datetime) -> bool:
    if snoozed_until is None:
        return True
    if snoozed_until.tzinfo is None or snoozed_until.utcoffset() is None:
        snoozed_until = snoozed_until.replace(tzinfo=timezone.utc)
    return snoozed_until <= now


async def get_pendency_feed(
    session: AsyncSession,
    user: User,
    *,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[PendencyRead], int]:
    """Return active, safe feed items for the user's current workspace."""
    workspace_id = user.workspace_id
    if not isinstance(workspace_id, uuid.UUID):
        return [], 0
    membership = (await session.exec(
        select(WorkspaceMembership.id).where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == user.id,
        )
    )).first()
    if membership is None:
        return [], 0

    now = datetime.now(timezone.utc)
    items: list[PendencyRead] = []

    tasks = (await session.exec(select(Task).where(
        Task.workspace_id == workspace_id,
        Task.user_id == user.id,
        Task.status == "suggested",
    ))).all()
    reminders = (await session.exec(select(Reminder).where(
        Reminder.workspace_id == workspace_id,
        Reminder.user_id == user.id,
        Reminder.status == "suggested",
    ))).all()

    # Resolve only the audio event's external note identifier. Neither event
    # content nor note transcription/structured data is ever included.
    source_event_ids = {
        row.source_event_id for row in [*tasks, *reminders]
        if row.source_event_id is not None
    }
    events_by_id: dict[uuid.UUID, NormalizedEvent] = {}
    notes_by_id: dict[uuid.UUID, Note] = {}
    if source_event_ids:
        events = (await session.exec(select(NormalizedEvent).where(
            NormalizedEvent.id.in_(list(source_event_ids)),
            NormalizedEvent.workspace_id == workspace_id,
            NormalizedEvent.user_id == user.id,
            NormalizedEvent.source == "audio",
        ))).all()
        events_by_id = {event.id: event for event in events}
        for event in events:
            try:
                note_id = uuid.UUID(event.external_id or "")
            except ValueError:
                continue
            note = await session.get(Note, note_id)
            if note is not None and note.user_id == user.id:
                notes_by_id[note.id] = note

    def has_kanban_cards(event_id: uuid.UUID | None) -> bool:
        event = events_by_id.get(event_id) if event_id else None
        if event is None:
            return False
        try:
            note_id = uuid.UUID(event.external_id or "")
        except ValueError:
            return False
        note = notes_by_id.get(note_id)
        cards = note.structured_data.get("kanban_task_ids") if note and isinstance(note.structured_data, dict) else None
        return isinstance(cards, list) and bool(cards)

    for task in tasks:
        if has_kanban_cards(task.source_event_id) or not _not_snoozed(task.snoozed_until, now):
            continue
        source_event = events_by_id.get(task.source_event_id) if task.source_event_id else None
        items.append(PendencyRead(
            id=task.id, kind="task", title=task.title, summary=task.summary,
            confidence=task.confidence, created_at=task.created_at,
            due_at=task.due_at, source=source_event.source if source_event else None,
        ))

    for reminder in reminders:
        if not _not_snoozed(reminder.snoozed_until, now):
            continue
        event = events_by_id.get(reminder.source_event_id) if reminder.source_event_id else None
        try:
            legacy_note_id = uuid.UUID(event.external_id or "") if event else None
        except ValueError:
            legacy_note_id = None
        note = notes_by_id.get(legacy_note_id) if legacy_note_id else None
        if note is not None and note.reminder_at is not None:
            continue
        items.append(PendencyRead(
            id=reminder.id, kind="reminder", title=reminder.title, summary=reminder.summary,
            confidence=reminder.confidence, created_at=reminder.created_at,
            due_at=reminder.due_at or reminder.remind_at,
            source=event.source if event else None,
        ))

    follow_ups = (await session.exec(select(FollowUp).where(
        FollowUp.workspace_id == workspace_id,
        FollowUp.user_id == user.id,
        FollowUp.status.in_(["open", "due", "waiting"]),
        FollowUp.response_status == "pending",
    ))).all()
    for follow_up in follow_ups:
        if _not_snoozed(follow_up.snoozed_until, now):
            items.append(PendencyRead(
                id=follow_up.id, kind="follow_up", title=follow_up.title,
                subkind="active",
                summary=follow_up.summary, confidence=follow_up.confidence,
                created_at=follow_up.created_at, due_at=follow_up.due_at,
                source=None,
            ))

    actions = (await session.exec(select(ActionSuggestion).where(
        ActionSuggestion.workspace_id == workspace_id,
        ActionSuggestion.user_id == user.id,
        ActionSuggestion.status == "suggested",
    ))).all()
    for action in actions:
        if _not_snoozed(action.snoozed_until, now):
            items.append(PendencyRead(
                id=action.id,
                kind="follow_up" if action.suggestion_type == "follow_up" else "action",
                subkind="suggestion" if action.suggestion_type == "follow_up" else None,
                title=action.title, summary=action.summary,
                confidence=action.confidence, created_at=action.created_at,
                due_at=action.due_at, source=None,
            ))

    approvals = (await session.exec(select(ActionExecution).where(
        ActionExecution.workspace_id == workspace_id,
        ActionExecution.user_id == user.id,
        ActionExecution.status == "awaiting_confirmation",
    ))).all()
    for approval in approvals:
        items.append(PendencyRead(
            id=approval.id, kind="approval", title=approval.action_type,
            summary="Requires confirmation", created_at=approval.created_at,
            source=approval.trigger_type,
        ))

    def sort_key(item: PendencyRead) -> tuple[float, str, str]:
        created_at = item.created_at
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        return created_at.timestamp(), item.kind, str(item.id)

    items.sort(key=sort_key, reverse=True)
    total = len(items)
    return items[offset:offset + limit], total
