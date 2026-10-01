"""ARQ wake-up scheduling and authoritative SQL reminder/follow-up reconciliation."""

import uuid
import logging
from datetime import datetime, timezone
from typing import Any

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.queue import enqueue_delayed_wakeup
from app.models.action_item import FollowUp, Reminder
from app.models.workspace import WorkspaceMembership
from app.services.workflow_engine import emit_trigger

logger = logging.getLogger("anotai.reminder_scheduler")


def _as_utc(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            value = datetime.fromisoformat(text)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        # SQLite returns timezone=True columns as naive datetimes; persisted values
        # are UTC and must be interpreted using the same convention.
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _item_uuid(value: uuid.UUID | str) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError):
        return None


async def schedule_reminder(
    reminder_id: uuid.UUID | str,
    remind_at: datetime,
    *,
    redis: Any | None = None,
) -> str | None:
    """Best-effort deferred wake-up; SQL Reminder state is the source of truth."""
    item_id = _item_uuid(reminder_id)
    target = _as_utc(remind_at)
    if item_id is None or target is None:
        raise ValueError("reminder_id e remind_at válidos são obrigatórios")
    return await enqueue_delayed_wakeup(
        "reminder_due_job", str(item_id), target, redis=redis,
    )


async def schedule_followup(
    followup_id: uuid.UUID | str,
    remind_at: datetime | None,
    *,
    redis: Any | None = None,
) -> str | None:
    """Best-effort deferred wake-up for a follow-up with a scheduled target."""
    item_id = _item_uuid(followup_id)
    target = _as_utc(remind_at)
    if item_id is None:
        raise ValueError("followup_id válido é obrigatório")
    if target is None:
        return None
    return await enqueue_delayed_wakeup(
        "followup_due_job", str(item_id), target, redis=redis,
    )


async def _has_membership(session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID) -> bool:
    membership = (await session.exec(select(WorkspaceMembership.id).where(
        WorkspaceMembership.workspace_id == workspace_id,
        WorkspaceMembership.user_id == user_id,
    ))).first()
    return membership is not None


async def process_reminder_due(
    session: AsyncSession,
    reminder_id: uuid.UUID | str,
    expected_target: datetime | str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate a delayed wake-up, emit a trigger, and mark the reminder due atomically."""
    item_id = _item_uuid(reminder_id)
    expected = _as_utc(expected_target)
    current_time = _as_utc(now or datetime.now(timezone.utc))
    if item_id is None or expected is None or current_time is None:
        return {"status": "noop", "reason": "invalid_target"}

    reminder = await session.get(Reminder, item_id)
    if reminder is None or reminder.status != "scheduled":
        return {"status": "noop", "reason": "not_scheduled"}
    target = _as_utc(reminder.remind_at)
    if target is None or target != expected:
        return {"status": "noop", "reason": "stale_target"}
    if target > current_time:
        return {"status": "noop", "reason": "early"}
    if not await _has_membership(session, reminder.workspace_id, reminder.user_id):
        return {"status": "noop", "reason": "membership_missing"}

    try:
        executions = await emit_trigger(
            session,
            reminder.workspace_id,
            reminder.user_id,
            "REMINDER_DUE",
            source_id=reminder.id,
            context={"event_type": "REMINDER_DUE", "confidence": reminder.confidence},
        )
        reminder.status = "due"
        session.add(reminder)
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return {"status": "due", "workflow_executions": len(executions)}


async def process_followup_due(
    session: AsyncSession,
    followup_id: uuid.UUID | str,
    expected_target: datetime | str,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Validate a follow-up wake-up; resolved/cancelled responses are no-ops."""
    item_id = _item_uuid(followup_id)
    expected = _as_utc(expected_target)
    current_time = _as_utc(now or datetime.now(timezone.utc))
    if item_id is None or expected is None or current_time is None:
        return {"status": "noop", "reason": "invalid_target"}

    followup = await session.get(FollowUp, item_id)
    if (
        followup is None
        or followup.status != "waiting"
        or followup.response_status != "pending"
    ):
        return {"status": "noop", "reason": "not_waiting"}
    target = _as_utc(followup.remind_at)
    if target is None or target != expected:
        return {"status": "noop", "reason": "stale_target"}
    if target > current_time:
        return {"status": "noop", "reason": "early"}
    if not await _has_membership(session, followup.workspace_id, followup.user_id):
        return {"status": "noop", "reason": "membership_missing"}

    try:
        executions = await emit_trigger(
            session,
            followup.workspace_id,
            followup.user_id,
            "FOLLOW_UP_DUE",
            source_id=followup.id,
            context={"event_type": "FOLLOW_UP_DUE", "confidence": followup.confidence},
        )
        followup.status = "due"
        session.add(followup)
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return {"status": "due", "workflow_executions": len(executions)}


async def reconcile_due_items(session: AsyncSession, *, batch_size: int = 100) -> dict[str, int]:
    """Reconcile only durable reminders/follow-ups due in SQL, never suggestions or notes."""
    now = datetime.now(timezone.utc)
    reminder_rows = (await session.exec(select(Reminder).where(
        Reminder.status == "scheduled",
        Reminder.remind_at <= now,
    ).order_by(Reminder.remind_at).limit(batch_size))).all()
    followup_rows = (await session.exec(select(FollowUp).where(
        FollowUp.status == "waiting",
        FollowUp.response_status == "pending",
        FollowUp.remind_at.is_not(None),
        FollowUp.remind_at <= now,
    ).order_by(FollowUp.remind_at).limit(batch_size))).all()

    processed = 0
    failed = 0
    # Snapshot scalar arguments before processing: rollback expires ORM rows in
    # async sessions, but later items in this bounded batch must still run.
    reminder_items = [(item.id, item.remind_at) for item in reminder_rows]
    followup_items = [(item.id, item.remind_at) for item in followup_rows]
    for item_id, target in reminder_items:
        try:
            result = await process_reminder_due(session, item_id, target, now=now)
            processed += result["status"] == "due"
        except Exception as exc:
            await session.rollback()
            session.expunge_all()
            failed += 1
            logger.warning("Reminder due item %s failed (%s)", item_id, type(exc).__name__)
    for item_id, target in followup_items:
        try:
            result = await process_followup_due(session, item_id, target, now=now)
            processed += result["status"] == "due"
        except Exception as exc:
            await session.rollback()
            session.expunge_all()
            failed += 1
            logger.warning("Follow-up due item %s failed (%s)", item_id, type(exc).__name__)
    return {
        "reminders_scanned": len(reminder_rows),
        "followups_scanned": len(followup_rows),
        "processed": processed,
        "failed": failed,
    }
