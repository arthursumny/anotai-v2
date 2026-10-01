"""Workspace- and owner-scoped retention for normalized source events.

Note.raw_transcription is deliberately never purged here: legacy Kanban/mobile
notes are user-owned records, not disposable event-derived snapshots.
"""

from datetime import datetime, timedelta, timezone
import logging
import uuid

from sqlalchemy import and_, delete, distinct, func, or_, select, update
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.models.action_item import ActionSuggestion, FollowUp, Reminder, Task
from app.models.generated_document import GeneratedDocument
from app.models.normalized_event import NormalizedEvent
from app.models.pending_conversation_item import PendingConversationItem
from app.models.source_policy import SourceMonitoringPolicy

logger = logging.getLogger("anotai.privacy")

_SOURCES = {"audio", "gmail", "whatsapp"}
_AUTO_DELETE = {
    "tasks": (Task, ("suggested", "dismissed", "superseded")),
    "reminders": (Reminder, ("suggested", "dismissed", "superseded")),
    "action_suggestions": (ActionSuggestion, ("suggested", "dismissed", "consumed")),
    "follow_ups": (FollowUp, ("resolved", "cancelled", "dismissed")),
}


def _counts() -> dict[str, int]:
    return {
        "events": 0,
        "generated_documents": 0,
        "tasks": 0,
        "reminders": 0,
        "action_suggestions": 0,
        "pending_conversation_items": 0,
        "follow_ups": 0,
        "failed": 0,
    }


def _aware_utc(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


async def _delete_event_children(
    session: AsyncSession,
    event: NormalizedEvent,
    counts: dict[str, int],
    *,
    manual: bool,
) -> None:
    # Child lookups are always additionally scoped by both workspace and user.
    scope = and_(
        GeneratedDocument.workspace_id == event.workspace_id,
        GeneratedDocument.user_id == event.user_id,
        GeneratedDocument.source_event_id == event.id,
    )
    result = await session.execute(delete(GeneratedDocument).where(scope))
    counts["generated_documents"] += result.rowcount or 0

    for key, (model, statuses) in _AUTO_DELETE.items():
        predicates = [
            model.workspace_id == event.workspace_id,
            model.user_id == event.user_id,
            model.source_event_id == event.id,
        ]
        if manual:
            manual_statuses = {
                Reminder: ("scheduled", "due", "cancelled"),
                FollowUp: ("waiting", "due", "cancelled"),
            }.get(model)
            if manual_statuses is not None:
                result = await session.execute(
                    delete(model).where(*predicates, model.status.in_(manual_statuses))
                )
                counts[key] += result.rowcount or 0
            elif model is Task:
                # Consumed tasks are accepted/user-owned and survive manual purge.
                pass
        result = await session.execute(
            delete(model).where(
                *predicates,
                model.status.in_(
                    tuple(status for status in statuses if not (manual and status == "cancelled"))
                ),
            )
        )
        counts[key] += result.rowcount or 0

    pending_scope = and_(
        PendingConversationItem.workspace_id == event.workspace_id,
        PendingConversationItem.user_id == event.user_id,
        PendingConversationItem.source_event == str(event.id),
    )
    result = await session.execute(delete(PendingConversationItem).where(pending_scope))
    counts["pending_conversation_items"] += result.rowcount or 0

    # Preserve accepted/active user-owned records, detaching their event reference.
    for model in (Task, Reminder, FollowUp, ActionSuggestion):
        await session.execute(
            update(model).where(
                model.workspace_id == event.workspace_id,
                model.user_id == event.user_id,
                model.source_event_id == event.id,
            ).values(source_event_id=None)
        )


async def _purge_one(
    session: AsyncSession, event: NormalizedEvent, counts: dict[str, int], *, manual: bool
) -> None:
    await _delete_event_children(session, event, counts, manual=manual)
    result = await session.execute(
        delete(NormalizedEvent).where(
            NormalizedEvent.id == event.id,
            NormalizedEvent.workspace_id == event.workspace_id,
            NormalizedEvent.user_id == event.user_id,
            NormalizedEvent.source == event.source,
        )
    )
    if result.rowcount:
        counts["events"] += result.rowcount


async def purge_expired_events(
    session: AsyncSession, *, now: datetime, batch_size: int = 100
) -> dict[str, int]:
    """Purge up to ``batch_size`` expired events, isolating each row for retries.

    Expiration is based on NormalizedEvent.created_at (UTC), never occurred_at.
    The query applies each same-owner/source policy cutoff, with the configured
    default for missing or revoked policies. The bounded selection spans all
    cutoffs so long-retention rows cannot starve shorter-retention policies.
    This function deliberately does not commit; the caller owns the transaction.
    """
    now = _aware_utc(now, "now")
    if isinstance(batch_size, bool) or not 1 <= batch_size <= 100:
        raise ValueError("batch_size must be between 1 and 100")

    events = NormalizedEvent.__table__
    policies = SourceMonitoringPolicy.__table__
    order = (events.c.created_at, events.c.id)
    default_cutoff = now - timedelta(days=settings.DEFAULT_EVENT_RETENTION_DAYS)
    default_rows = (
        await session.execute(
            select(NormalizedEvent)
            .outerjoin(
                policies,
                and_(
                    policies.c.workspace_id == events.c.workspace_id,
                    policies.c.user_id == events.c.user_id,
                    policies.c.source == events.c.source,
                ),
            )
            .where(
                events.c.source.in_(_SOURCES),
                events.c.created_at < default_cutoff,
                or_(policies.c.id.is_(None), policies.c.retention_days.is_(None)),
            )
            .order_by(*order)
            .limit(batch_size)
        )
    ).scalars().all()

    # At most 365 distinct configured day values. Each policy cohort is queried
    # independently with its own SQL cutoff and bounded candidate limit.
    day_values = (
        await session.execute(
            select(distinct(policies.c.retention_days)).where(
                policies.c.retention_days.is_not(None)
            ).limit(365)
        )
    ).scalars().all()
    candidates = list(default_rows)
    for days in day_values:
        cutoff = now - timedelta(days=int(days))
        group = (
            await session.execute(
                select(NormalizedEvent)
                .join(
                    policies,
                    and_(
                        policies.c.workspace_id == events.c.workspace_id,
                        policies.c.user_id == events.c.user_id,
                        policies.c.source == events.c.source,
                    ),
                )
                .where(
                    events.c.source.in_(_SOURCES),
                    policies.c.retention_days == days,
                    events.c.created_at < cutoff,
                )
                .order_by(*order)
                .limit(batch_size)
            )
        ).scalars().all()
        candidates.extend(group)

    # Merge the bounded per-cohort candidate sets; no unexpired event content is
    # read, and the eventual delete count never exceeds batch_size.
    expired = sorted(candidates, key=lambda event: (event.created_at, event.id))[:batch_size]

    counts = _counts()
    for event in expired:
        row_counts = _counts()
        try:
            async with session.begin_nested():
                await _purge_one(session, event, row_counts, manual=False)
            for key, value in row_counts.items():
                counts[key] += value
        except Exception:
            # No row contents or exception details are emitted. Failed rows remain
            # eligible on a later invocation; other candidates continue processing.
            counts["failed"] += 1
            continue
    return counts


async def purge_user_source_events(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    source: str = "audio",
    *,
    cancel_active: bool,
) -> dict[str, int]:
    """Manually purge this owner's selected source events without committing."""
    if source not in {*_SOURCES, "all"}:
        raise ValueError("source must be audio, gmail, whatsapp, or all")
    statement = select(NormalizedEvent).where(
        NormalizedEvent.workspace_id == workspace_id,
        NormalizedEvent.user_id == user_id,
    )
    if source != "all":
        statement = statement.where(NormalizedEvent.source == source)
    counts = _counts()
    last_created_at: datetime | None = None
    last_id: uuid.UUID | None = None
    while True:
        page_query = statement.order_by(NormalizedEvent.created_at, NormalizedEvent.id).limit(100)
        if last_created_at is not None and last_id is not None:
            page_query = page_query.where(
                or_(
                    NormalizedEvent.created_at > last_created_at,
                    and_(NormalizedEvent.created_at == last_created_at, NormalizedEvent.id > last_id),
                )
            )
        page = (await session.execute(page_query)).scalars().all()
        if not page:
            break
        for event in page:
            row_counts = _counts()
            try:
                async with session.begin_nested():
                    await _purge_one(session, event, row_counts, manual=cancel_active)
                for key, value in row_counts.items():
                    counts[key] += value
            except Exception:
                # Advance the cursor despite errors so one bad record cannot pin the page.
                counts["failed"] += 1
                logger.warning("purge_row_failed id=%s", str(event.id))
        last_created_at, last_id = page[-1].created_at, page[-1].id
        if len(page) < 100:
            break
    return counts
