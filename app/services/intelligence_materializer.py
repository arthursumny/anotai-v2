"""Persist safe intelligence suggestions derived from normalized events."""

import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import TypeAlias
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.models.action_item import ActionSuggestion, Reminder, Task
from app.models.normalized_event import NormalizedEvent
from app.models.workspace import WorkspaceMembership
from app.schemas.intelligence import IntelligenceResult, IntelligenceType

MaterializedItem: TypeAlias = Task | Reminder | ActionSuggestion


async def _persisted_event_scope(
    session: AsyncSession, event: NormalizedEvent
) -> tuple[uuid.UUID, uuid.UUID, str]:
    event_id = getattr(event, "id", None)
    if not isinstance(event_id, uuid.UUID):
        raise ValueError("Evento persistido inválido")
    # Scalar columns are read from the database without trusting mutable fields
    # on the caller's ORM instance or flushing untrusted in-memory changes.
    with session.no_autoflush:
        row = (await session.exec(
            select(NormalizedEvent.workspace_id, NormalizedEvent.user_id, NormalizedEvent.source)
            .where(NormalizedEvent.id == event_id)
        )).first()
    if row is None:
        raise ValueError("Evento não persistido")
    workspace_id, user_id, source = row
    if not isinstance(workspace_id, uuid.UUID) or not isinstance(user_id, uuid.UUID):
        raise ValueError("Escopo do evento inválido")
    if not isinstance(source, str) or not source or len(source) > 20:
        raise ValueError("Fonte do evento inválida")
    membership_id = (await session.exec(
        select(WorkspaceMembership.id).where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == user_id,
        )
    )).first()
    if membership_id is None:
        raise PermissionError("Usuário não pertence ao workspace do evento")
    return workspace_id, user_id, source


def _idempotency_key(source: str, event_id: uuid.UUID, index: int, kind: IntelligenceType) -> str:
    return f"{source}:{event_id}:{index}:{kind.value}"[:320]


def _to_model(
    result: IntelligenceResult,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    event_id: uuid.UUID,
    idempotency_key: str,
    timezone_name: str,
) -> MaterializedItem | None:
    due_at = result.due_at
    if due_at is not None and (due_at.tzinfo is None or due_at.utcoffset() is None):
        raise ValueError("due_at deve incluir timezone")
    due_at = due_at.astimezone(ZoneInfo(timezone_name)) if due_at is not None else None
    common = {
        "workspace_id": workspace_id,
        "user_id": user_id,
        "source_event_id": event_id,
        "idempotency_key": idempotency_key,
        "title": result.title[:300],
        "summary": result.summary,
        "confidence": result.confidence,
        "timezone": timezone_name,
    }
    if result.type is IntelligenceType.TASK:
        return Task(**common, status="suggested", due_at=due_at)
    if result.type is IntelligenceType.REMINDER:
        if due_at is None:
            return None
        return Reminder(**common, status="suggested", due_at=due_at, remind_at=due_at)
    if result.type is IntelligenceType.FOLLOW_UP:
        # A single message cannot establish that a reply is actually overdue.
        return ActionSuggestion(
            **common,
            status="suggested",
            due_at=due_at,
            suggestion_type="follow_up",
        )
    if result.type is IntelligenceType.CALENDAR_EVENT:
        return ActionSuggestion(
            **common, status="suggested", due_at=due_at, suggestion_type="calendar_event"
        )
    if result.type is IntelligenceType.ACTION:
        return ActionSuggestion(
            **common, status="suggested", due_at=due_at, suggestion_type="action"
        )
    # NOTE, MEMORY, INFORMATION and NOT_ACTIONABLE are not materialized.
    return None


async def _existing(
    session: AsyncSession,
    model_type: type[MaterializedItem],
    workspace_id: uuid.UUID,
    idempotency_key: str,
) -> MaterializedItem | None:
    return (await session.exec(
        select(model_type).where(
            model_type.workspace_id == workspace_id,  # type: ignore[attr-defined]
            model_type.idempotency_key == idempotency_key,  # type: ignore[attr-defined]
        )
    )).first()


async def materialize_results(
    session: AsyncSession,
    event: NormalizedEvent,
    results: Sequence[IntelligenceResult],
    *,
    timezone: str,
    automation_mode: str = "SUGGESTIVE",
    minimum_confidence: float | None = None,
) -> list[MaterializedItem]:
    """Store safe suggested items only; never execute actions or commit.

    Workspace and user identity are fetched from the persisted event, and the
    membership is revalidated before creating any records. ``automation_mode``
    is accepted for caller compatibility but materialization remains suggestive
    in every mode; execution belongs to a separate, explicitly authorized path.
    """
    if not isinstance(automation_mode, str) or not automation_mode.strip():
        raise ValueError("automation_mode inválido")
    try:
        zone = ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, TypeError) as exc:
        raise ValueError("timezone inválido") from exc
    threshold = settings.ROUTER_CONFIDENCE_THRESHOLD if minimum_confidence is None else minimum_confidence
    if not isinstance(threshold, (int, float)) or isinstance(threshold, bool) or not 0 <= threshold <= 1:
        raise ValueError("minimum_confidence deve estar entre 0 e 1")

    workspace_id, user_id, source = await _persisted_event_scope(session, event)
    event_id = event.id
    materialized: list[MaterializedItem] = []
    for index, raw_result in enumerate(results):
        result = IntelligenceResult.model_validate(raw_result)
        if result.confidence < threshold:
            continue
        key = _idempotency_key(source, event_id, index, result.type)
        item = _to_model(
            result,
            workspace_id=workspace_id,
            user_id=user_id,
            event_id=event_id,
            idempotency_key=key,
            timezone_name=zone.key,
        )
        if item is None:
            continue
        item_type = type(item)
        existing = await _existing(session, item_type, workspace_id, key)
        if existing is not None:
            materialized.append(existing)
            continue
        try:
            async with session.begin_nested():
                session.add(item)
                await session.flush()
        except IntegrityError:
            # Unique workspace/idempotency constraint arbitrates concurrent retries.
            existing = await _existing(session, item_type, workspace_id, key)
            if existing is None:
                raise
            item = existing
        materialized.append(item)
    return materialized
