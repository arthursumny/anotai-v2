"""Transactional acceptance of supported pendency suggestions."""

import uuid
import hashlib
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import update
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.action_item import ActionSuggestion, FollowUp, Reminder, Task
from app.models.normalized_event import NormalizedEvent
from app.models.note import Note
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMembership


class PendencyActionError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        # SQLite drops tzinfo for timezone=True columns; persisted instants use UTC.
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


async def _membership_and_timezone(session: AsyncSession, user: User) -> tuple[uuid.UUID, str]:
    workspace_id = user.workspace_id
    if not isinstance(workspace_id, uuid.UUID):
        raise PendencyActionError(404, "Pendency not found")
    member = (await session.exec(select(WorkspaceMembership.id).where(
        WorkspaceMembership.workspace_id == workspace_id,
        WorkspaceMembership.user_id == user.id,
    ))).first()
    workspace = await session.get(Workspace, workspace_id)
    if member is None or workspace is None:
        raise PendencyActionError(404, "Pendency not found")
    return workspace_id, workspace.timezone


async def _source_note(session: AsyncSession, item: Task | Reminder, workspace_id: uuid.UUID, user_id: uuid.UUID) -> Note | None:
    if item.source_event_id is None:
        return None
    event = (await session.exec(select(NormalizedEvent).where(
        NormalizedEvent.id == item.source_event_id,
        NormalizedEvent.workspace_id == workspace_id,
        NormalizedEvent.user_id == user_id,
        NormalizedEvent.source == "audio",
    ))).first()
    if event is None:
        return None
    try:
        note_id = uuid.UUID(event.external_id or "")
    except (ValueError, TypeError):
        return None
    note = await session.get(Note, note_id)
    return note if note is not None and note.user_id == user_id else None


async def accept_pendency(
    session: AsyncSession,
    user: User,
    kind: str,
    item_id: uuid.UUID,
    *,
    title: str | None = None,
    due_at: datetime | None = None,
    remind_at: datetime | None = None,
) -> dict[str, Any]:
    if kind == "follow_up":
        return await _accept_followup_suggestion(
            session, user, item_id, title=title, remind_at=remind_at,
        )
    if kind not in {"task", "reminder"}:
        raise PendencyActionError(422, "Unsupported pendency kind")
    workspace_id, workspace_timezone = await _membership_and_timezone(session, user)
    model = Task if kind == "task" else Reminder
    item = (await session.exec(select(model).where(
        model.id == item_id,
        model.workspace_id == workspace_id,
        model.user_id == user.id,
    ))).first()
    if item is None:
        raise PendencyActionError(404, "Pendency not found")

    # Acceptance already committed: retries only return the durable result.
    if item.status in ({"open", "in_progress", "done"} if kind == "task" else {"scheduled"}):
        return {"kind": kind, "id": item.id, "status": item.status}
    if item.status != "suggested":
        raise PendencyActionError(409, "Pendency is no longer suggested")

    source_note = await _source_note(session, item, workspace_id, user.id) if kind == "reminder" else None
    if kind == "reminder" and source_note is not None and source_note.reminder_at is not None:
        item.status = "superseded"
        item.updated_at = datetime.now(timezone.utc)
        session.add(item)
        await session.commit()
        raise PendencyActionError(409, "Audio note already has a reminder")

    if kind == "reminder" and remind_at is None:
        raise PendencyActionError(422, "remind_at is required")

    values: dict[str, Any] = {"status": "open" if kind == "task" else "scheduled",
                              "updated_at": datetime.now(timezone.utc)}
    if kind == "task":
        values["kanban_status"] = "todo"
    if title is not None:
        values["title"] = title
    if kind == "reminder":
        values["remind_at"] = _utc(remind_at)
        values["timezone"] = workspace_timezone
    elif due_at is not None:
        values["due_at"] = _utc(due_at)
    claimed = (await session.exec(update(model).where(
        model.id == item_id,
        model.workspace_id == workspace_id,
        model.user_id == user.id,
        model.status == "suggested",
    ).values(**values).returning(model.id))).first()
    if claimed is None:
        await session.rollback()
        latest = (await session.exec(select(model).where(
            model.id == item_id, model.workspace_id == workspace_id, model.user_id == user.id,
        ))).first()
        if latest is not None and latest.status in ({"open", "in_progress", "done"} if kind == "task" else {"scheduled"}):
            return {"kind": kind, "id": latest.id, "status": latest.status}
        raise PendencyActionError(409, "Pendency is no longer suggested")

    await session.commit()
    if kind == "reminder":
        return {"kind": kind, "id": item_id, "status": "scheduled", "remind_at": _utc(remind_at)}
    return {"kind": kind, "id": item_id, "status": "open"}


async def _accept_followup_suggestion(
    session: AsyncSession, user: User, item_id: uuid.UUID, *,
    title: str | None, remind_at: datetime | None,
) -> dict[str, Any]:
    workspace_id, workspace_timezone = await _membership_and_timezone(session, user)
    suggestion = (await session.exec(select(ActionSuggestion).where(
        ActionSuggestion.id == item_id,
        ActionSuggestion.workspace_id == workspace_id,
        ActionSuggestion.user_id == user.id,
        ActionSuggestion.suggestion_type == "follow_up",
    ))).first()
    if suggestion is None:
        raise PendencyActionError(404, "Pendency not found")
    idempotency_key = "pendency-follow-up:" + hashlib.sha256(str(item_id).encode()).hexdigest()
    if suggestion.status == "consumed":
        created = (await session.exec(select(FollowUp).where(
            FollowUp.workspace_id == workspace_id,
            FollowUp.user_id == user.id,
            FollowUp.idempotency_key == idempotency_key,
        ))).first()
        if created is None:
            raise PendencyActionError(409, "Accepted follow-up was not found")
        return {"kind": "follow_up", "id": created.id, "status": created.status}
    if suggestion.status != "suggested":
        raise PendencyActionError(409, "Follow-up suggestion is no longer available")
    if suggestion.source_event_id is None:
        raise PendencyActionError(422, "Follow-up source event is required")
    event = (await session.exec(select(NormalizedEvent).where(
        NormalizedEvent.id == suggestion.source_event_id,
        NormalizedEvent.workspace_id == workspace_id,
        NormalizedEvent.user_id == user.id,
    ))).first()
    if event is None:
        raise PendencyActionError(422, "Follow-up source event is unavailable")
    metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
    if str(metadata.get("direction", "")).lower() == "outgoing":
        raise PendencyActionError(409, "Outgoing events cannot create follow-ups")
    if not event.conversation_id or not event.sender:
        raise PendencyActionError(422, "Follow-up requires a conversation and contact")
    target = remind_at if remind_at is not None else suggestion.due_at
    if target is None:
        raise PendencyActionError(422, "remind_at or a suggested due_at is required")
    target = _utc(target)
    claimed = (await session.exec(update(ActionSuggestion).where(
        ActionSuggestion.id == item_id,
        ActionSuggestion.workspace_id == workspace_id,
        ActionSuggestion.user_id == user.id,
        ActionSuggestion.suggestion_type == "follow_up",
        ActionSuggestion.status == "suggested",
    ).values(status="consumed", updated_at=datetime.now(timezone.utc)).returning(ActionSuggestion.id))).first()
    if claimed is None:
        await session.rollback()
        latest = (await session.exec(select(ActionSuggestion).where(
            ActionSuggestion.id == item_id,
            ActionSuggestion.workspace_id == workspace_id,
            ActionSuggestion.user_id == user.id,
            ActionSuggestion.suggestion_type == "follow_up",
        ))).first()
        if latest is not None and latest.status == "consumed":
            created = (await session.exec(select(FollowUp).where(
                FollowUp.workspace_id == workspace_id, FollowUp.user_id == user.id,
                FollowUp.idempotency_key == idempotency_key,
            ))).first()
            if created is not None:
                return {"kind": "follow_up", "id": created.id, "status": created.status}
        raise PendencyActionError(409, "Follow-up suggestion is no longer available")
    follow_up = FollowUp(
        workspace_id=workspace_id,
        user_id=user.id,
        source_event_id=event.id,
        idempotency_key=idempotency_key,
        title=title or suggestion.title,
        summary=suggestion.summary,
        confidence=suggestion.confidence,
        status="waiting",
        response_status="pending",
        conversation_id=event.conversation_id,
        contact=event.sender,
        remind_at=target,
        due_at=target,
        timezone=workspace_timezone,
    )
    session.add(follow_up)
    await session.commit()
    return {"kind": "follow_up", "id": follow_up.id, "status": "waiting", "remind_at": target}


async def resolve_followup(session: AsyncSession, user: User, item_id: uuid.UUID) -> dict[str, Any]:
    workspace_id, _ = await _membership_and_timezone(session, user)
    updated = (await session.exec(update(FollowUp).where(
        FollowUp.id == item_id,
        FollowUp.workspace_id == workspace_id,
        FollowUp.user_id == user.id,
        FollowUp.status.in_(["due", "waiting", "open"]),
        FollowUp.response_status == "pending",
    ).values(status="resolved", response_status="answered", updated_at=datetime.now(timezone.utc))
      .returning(FollowUp.id))).first()
    if updated is None:
        row = (await session.exec(select(FollowUp).where(
            FollowUp.id == item_id, FollowUp.workspace_id == workspace_id, FollowUp.user_id == user.id,
        ))).first()
        if row is None:
            raise PendencyActionError(404, "Pendency not found")
        if row.status == "resolved" and row.response_status == "answered":
            return {"kind": "follow_up", "id": item_id, "status": "resolved"}
        raise PendencyActionError(409, "Follow-up is no longer resolvable")
    await session.commit()
    return {"kind": "follow_up", "id": item_id, "status": "resolved"}


async def ignore_pendency(
    session: AsyncSession, user: User, kind: str, item_id: uuid.UUID,
) -> dict[str, Any]:
    if kind == "approval":
        raise PendencyActionError(409, "Dismiss approvals via /workflows/executions/{id}/dismiss")
    workspace_id, _ = await _membership_and_timezone(session, user)
    now = datetime.now(timezone.utc)
    if kind in {"task", "reminder", "action"}:
        model = {"task": Task, "reminder": Reminder, "action": ActionSuggestion}[kind]
        scope = (model.id == item_id, model.workspace_id == workspace_id, model.user_id == user.id)
        active = model.status == "suggested"
        dismissed = "dismissed"
        status_column = model.status
        already_dismissed = model.status == "dismissed"
    elif kind == "follow_up":
        suggestion = (await session.exec(select(ActionSuggestion).where(
            ActionSuggestion.id == item_id,
            ActionSuggestion.workspace_id == workspace_id,
            ActionSuggestion.user_id == user.id,
            ActionSuggestion.suggestion_type == "follow_up",
        ))).first()
        if suggestion is not None:
            model = ActionSuggestion
            scope = (ActionSuggestion.id == item_id, ActionSuggestion.workspace_id == workspace_id,
                     ActionSuggestion.user_id == user.id, ActionSuggestion.suggestion_type == "follow_up")
            active = ActionSuggestion.status == "suggested"
            dismissed = "dismissed"
        else:
            model = FollowUp
            scope = (FollowUp.id == item_id, FollowUp.workspace_id == workspace_id, FollowUp.user_id == user.id)
            active = (FollowUp.status.in_(["due", "waiting", "open"])) & (FollowUp.response_status == "pending")
            dismissed = "dismissed"
    else:
        raise PendencyActionError(422, "Unsupported pendency kind")

    updated = (await session.exec(update(model).where(*scope, active).values(
        status=dismissed,
        updated_at=now,
        **({"response_status": "dismissed"} if kind == "follow_up" and model is FollowUp else {}),
    ).returning(model.id))).first()
    if updated is None:
        row = (await session.exec(select(model).where(*scope))).first()
        if row is None:
            raise PendencyActionError(404, "Pendency not found")
        if row.status == dismissed or (kind == "follow_up" and getattr(row, "response_status", None) == "dismissed"):
            return {"kind": kind, "id": item_id, "status": dismissed}
        raise PendencyActionError(409, "Pendency is no longer dismissible")
    await session.commit()
    return {"kind": kind, "id": item_id, "status": dismissed}


async def snooze_pendency(
    session: AsyncSession, user: User, kind: str, item_id: uuid.UUID, until: datetime,
) -> dict[str, Any]:
    workspace_id, _ = await _membership_and_timezone(session, user)
    target = _utc(until)
    now = datetime.now(timezone.utc)
    if target is None or target <= now:
        raise PendencyActionError(422, "until must be in the future")
    if kind == "follow_up":
        suggestion = (await session.exec(select(ActionSuggestion).where(
            ActionSuggestion.id == item_id,
            ActionSuggestion.workspace_id == workspace_id,
            ActionSuggestion.user_id == user.id,
            ActionSuggestion.suggestion_type == "follow_up",
        ))).first()
        if suggestion is not None:
            model = ActionSuggestion
            scope = (ActionSuggestion.id == item_id, ActionSuggestion.workspace_id == workspace_id,
                     ActionSuggestion.user_id == user.id, ActionSuggestion.suggestion_type == "follow_up")
            active = ActionSuggestion.status == "suggested"
        else:
            model = FollowUp
            scope = (FollowUp.id == item_id, FollowUp.workspace_id == workspace_id, FollowUp.user_id == user.id)
            active = (FollowUp.status.in_(["due", "waiting", "open"])) & (FollowUp.response_status == "pending")
    elif kind in {"task", "reminder", "action"}:
        model = {"task": Task, "reminder": Reminder, "action": ActionSuggestion}[kind]
        scope = (model.id == item_id, model.workspace_id == workspace_id, model.user_id == user.id)
        active = model.status == "suggested"
    elif kind == "approval":
        raise PendencyActionError(409, "Approvals cannot be snoozed here")
    else:
        raise PendencyActionError(422, "Unsupported pendency kind")
    updated = (await session.exec(update(model).where(*scope, active).values(
        snoozed_until=target,
        updated_at=now,
    ).returning(model.id))).first()
    if updated is None:
        row = (await session.exec(select(model).where(*scope))).first()
        if row is None:
            raise PendencyActionError(404, "Pendency not found")
        raise PendencyActionError(409, "Pendency is no longer snoozable")
    await session.commit()
    return {"kind": kind, "id": item_id, "status": "suggested"}


async def edit_pendency(
    session: AsyncSession,
    user: User,
    kind: str,
    item_id: uuid.UUID,
    *,
    title: str | None = None,
    due_at: datetime | None = None,
    remind_at: datetime | None = None,
) -> dict[str, Any]:
    if kind == "approval":
        raise PendencyActionError(409, "Approval payloads cannot be edited here")
    if kind == "follow_up":
        workspace_id, _ = await _membership_and_timezone(session, user)
        model = ActionSuggestion
        scope = (ActionSuggestion.id == item_id, ActionSuggestion.workspace_id == workspace_id,
                 ActionSuggestion.user_id == user.id, ActionSuggestion.suggestion_type == "follow_up")
        active = ActionSuggestion.status == "suggested"
        time_columns = {"due_at": "due_at", "remind_at": "remind_at"}
    elif kind in {"task", "reminder", "action"}:
        workspace_id, _ = await _membership_and_timezone(session, user)
        model = {"task": Task, "reminder": Reminder, "action": ActionSuggestion}[kind]
        scope = (model.id == item_id, model.workspace_id == workspace_id, model.user_id == user.id)
        active = model.status == "suggested"
        time_columns = {"due_at": "due_at", "remind_at": "remind_at"}
    else:
        raise PendencyActionError(422, "Unsupported pendency kind")
    values: dict[str, Any] = {}
    if title is not None:
        values["title"] = title
    if due_at is not None:
        values[time_columns["due_at"]] = _utc(due_at)
    if remind_at is not None:
        values[time_columns["remind_at"]] = _utc(remind_at)
    if not values:
        raise PendencyActionError(422, "At least one edit field is required")
    values["updated_at"] = datetime.now(timezone.utc)
    updated = (await session.exec(update(model).where(*scope, active).values(**values).returning(model.id))).first()
    if updated is None:
        row = (await session.exec(select(model).where(*scope))).first()
        if row is None:
            raise PendencyActionError(404, "Pendency not found")
        raise PendencyActionError(409, "Pendency is no longer editable")
    await session.commit()
    return {"kind": kind, "id": item_id, "status": "suggested"}
