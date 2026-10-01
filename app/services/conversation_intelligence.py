"""Bounded WhatsApp conversation follow-up state; no provider actions."""

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Sequence

from openai import AsyncOpenAI
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.models.action_item import FollowUp
from app.models.conversation_state import ConversationState
from app.models.normalized_event import NormalizedEvent
from app.models.pending_conversation_item import PendingConversationItem
from app.models.source_policy import SourceMonitoringPolicy
from app.models.user import User
from app.models.whatsapp_instance import WhatsAppInstance
from app.models.workspace import WorkspaceMembership
from app.schemas.intelligence import IntelligenceResult, IntelligenceType
from app.services.llm import chat_create

logger = logging.getLogger(__name__)


def _utc(value: datetime) -> datetime:
    # SQLite drops timezone offsets for DateTime(timezone=True); stored naive
    # values follow the repository's UTC persistence convention.
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _db_time(value: datetime, session: AsyncSession | None = None) -> datetime:
    """Bind aware UTC values; SQLite's DateTime adapter stores them naive UTC."""
    return _utc(value)


def _policy_allows_conversation(policy: SourceMonitoringPolicy, conversation_id: str, metadata: object) -> bool:
    meta = metadata if isinstance(metadata, dict) else {}
    is_group = meta.get("is_group", conversation_id.endswith("@g.us"))
    if is_group is True and not policy.allow_groups:
        return False
    if is_group is False and not policy.allow_individuals:
        return False
    selected = set(policy.selected_ids or [])
    in_selected = conversation_id in selected
    if policy.mode == "ALLOWLIST" and not in_selected:
        return False
    if policy.mode == "BLOCKLIST" and in_selected:
        return False
    return policy.mode in {"ALL", "ALLOWLIST", "BLOCKLIST"}


async def _confirmed_answers(pending: Sequence[PendingConversationItem], outbound: str) -> set[uuid.UUID]:
    if not pending or not settings.OPENAI_API_KEY:
        return set()
    try:
        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        response = await chat_create(
            client,
            model=settings.OPENAI_ROUTER_MODEL,
            messages=[
                {"role": "system", "content": (
                    "Determine which prior questions are semantically answered by the outbound message. "
                    "Return only JSON with resolved_ids (a subset of supplied UUIDs) and confidence from 0 to 1. "
                    "Do not infer answers from unrelated messages."
                )},
                {"role": "user", "content": json.dumps({
                    "questions": [{"id": str(item.id), "summary": item.summary} for item in pending],
                    "outbound_message": outbound,
                }, ensure_ascii=False)},
            ],
            response_format={"type": "json_object"},
        )
        text = response.choices[0].message.content
        data = json.loads(text)
        if (not isinstance(data, dict) or set(data) != {"resolved_ids", "confidence"}
                or isinstance(data["confidence"], bool)
                or not isinstance(data["confidence"], (int, float))
                or not 0 <= data["confidence"] <= 1
                or data["confidence"] < settings.ROUTER_CONFIDENCE_THRESHOLD
                or not isinstance(data["resolved_ids"], list)):
            return set()
        allowed = {item.id for item in pending}
        if any(not isinstance(value, str) for value in data["resolved_ids"]):
            return set()
        selected = {uuid.UUID(value) for value in data["resolved_ids"]}
        return selected if selected.issubset(allowed) else set()
    except Exception:
        # Fail closed without logging message text or provider response.
        logger.warning("conversation answer classification failed")
        return set()


async def process_authorized_conversation_event(
    session: AsyncSession,
    event: NormalizedEvent,
    instance: WhatsAppInstance,
    results: list[IntelligenceResult] | None = None,
) -> ConversationState | None:
    """Process an already-persisted and authorized event in its own tenant scope."""
    event_id = getattr(event, "id", None)
    if not isinstance(event_id, uuid.UUID):
        raise ValueError("event must be persisted")
    with session.no_autoflush:
        row = (await session.exec(select(NormalizedEvent).where(NormalizedEvent.id == event_id))).first()
    if row is None or row.status not in {"accepted", "processed"}:
        raise ValueError("event is not an accepted persisted event")
    if row.source != "whatsapp" or not row.conversation_id or not row.external_id:
        raise ValueError("conversation event must be WhatsApp with message and conversation IDs")
    if (row.workspace_id != instance.workspace_id or row.user_id != instance.user_id
            or instance.workspace_id is None or instance.user_id is None):
        raise PermissionError("event and WhatsApp instance ownership mismatch")
    persisted_instance = (await session.exec(select(WhatsAppInstance.id).where(
        WhatsAppInstance.id == instance.id,
        WhatsAppInstance.workspace_id == row.workspace_id,
        WhatsAppInstance.user_id == row.user_id,
    ))).first()
    if persisted_instance is None:
        raise PermissionError("WhatsApp instance is not persisted in the event scope")
    owner = (await session.exec(select(User.id).where(
        User.id == row.user_id, User.workspace_id == row.workspace_id,
    ))).first()
    member = (await session.exec(select(WorkspaceMembership.id).where(
        WorkspaceMembership.workspace_id == row.workspace_id,
        WorkspaceMembership.user_id == row.user_id,
    ))).first()
    if owner is None or member is None:
        raise PermissionError("event owner is not a workspace member")
    policy = (await session.exec(select(SourceMonitoringPolicy).where(
        SourceMonitoringPolicy.workspace_id == row.workspace_id,
        SourceMonitoringPolicy.user_id == row.user_id,
        SourceMonitoringPolicy.source == "whatsapp",
    ))).first()
    if policy is None:
        raise PermissionError("WhatsApp source policy is required")

    if row.status == "processed":
        return (await session.exec(select(ConversationState).where(
            ConversationState.workspace_id == row.workspace_id,
            ConversationState.user_id == row.user_id,
            ConversationState.instance_id == instance.id,
            ConversationState.conversation_id == row.conversation_id,
        ))).first()

    occurred = _utc(row.occurred_at)
    direction = row.metadata_json.get("direction") if isinstance(row.metadata_json, dict) else None
    if direction not in {"incoming", "outgoing"}:
        raise ValueError("event direction missing or invalid")
    state = (await session.exec(select(ConversationState).where(
        ConversationState.workspace_id == row.workspace_id,
        ConversationState.user_id == row.user_id,
        ConversationState.instance_id == instance.id,
        ConversationState.conversation_id == row.conversation_id,
    ))).first()
    if state and (state.last_incoming_message_id == row.external_id
                  or state.last_outgoing_message_id == row.external_id
                  or state.last_processed_message_id == row.external_id):
        row.status = "processed"
        await session.flush()
        return state
    if state is None:
        state = ConversationState(
            workspace_id=row.workspace_id, user_id=row.user_id, instance_id=instance.id,
            conversation_id=row.conversation_id,
        )
        session.add(state)
        try:
            await session.flush()
        except IntegrityError:
            state = (await session.exec(select(ConversationState).where(
                ConversationState.workspace_id == row.workspace_id,
                ConversationState.user_id == row.user_id,
                ConversationState.instance_id == instance.id,
                ConversationState.conversation_id == row.conversation_id,
            ))).one()

    # Timestamp/message cursors are monotonic even for out-of-order deliveries.
    if state.last_processed_at is None or occurred > _utc(state.last_processed_at):
        state.last_processed_at = _db_time(occurred, session)
        state.last_processed_message_id = row.external_id
    if direction == "incoming":
        if state.last_incoming_at is None or occurred > _utc(state.last_incoming_at):
            state.last_incoming_at = _db_time(occurred, session)
            state.last_incoming_message_id = row.external_id
    else:
        if state.last_outgoing_at is None or occurred > _utc(state.last_outgoing_at):
            state.last_outgoing_at = _db_time(occurred, session)
            state.last_outgoing_message_id = row.external_id

    # Policy filters are re-evaluated at processing time; a blocked conversation
    # only advances the cursor and cannot contribute summaries to an LLM call.
    conversation_allowed = _policy_allows_conversation(policy, row.conversation_id, row.metadata_json)
    if not conversation_allowed or policy.direction != "both":
        row.status = "processed"
        await session.flush()
        return state

    if direction == "incoming":
        threshold = settings.ROUTER_CONFIDENCE_THRESHOLD
        follow_up = next((IntelligenceResult.model_validate(result) for result in (results or [])
                          if IntelligenceResult.model_validate(result).type is IntelligenceType.FOLLOW_UP
                          and IntelligenceResult.model_validate(result).confidence >= threshold), None)
        existing = (await session.exec(select(PendingConversationItem).where(
            PendingConversationItem.workspace_id == row.workspace_id,
            PendingConversationItem.source_event == str(event_id),
        ))).first()
        if follow_up is not None and existing is None:
            session.add(PendingConversationItem(
                workspace_id=row.workspace_id, user_id=row.user_id, instance_id=instance.id,
                conversation_id=row.conversation_id, source_event=str(event_id),
                message_id=row.external_id, summary=follow_up.summary[:500],
                asked_at=_db_time(occurred, session),
                remind_at=_db_time(occurred + timedelta(hours=settings.FOLLOW_UP_DELAY_HOURS), session),
                status="WAITING_FOR_RESPONSE", confidence=follow_up.confidence,
            ))
    else:
        pending = list((await session.exec(select(PendingConversationItem).where(
            PendingConversationItem.workspace_id == row.workspace_id,
            PendingConversationItem.user_id == row.user_id,
            PendingConversationItem.instance_id == instance.id,
            PendingConversationItem.conversation_id == row.conversation_id,
            or_(PendingConversationItem.__table__.c.status == "WAITING_FOR_RESPONSE",
                PendingConversationItem.__table__.c.status == "DUE"),
            PendingConversationItem.asked_at <= _db_time(occurred, session),
        ))).all())
        pending = [item for item in pending if _policy_allows_conversation(
            policy, item.conversation_id, row.metadata_json,
        )]
        ids = await _confirmed_answers(pending, row.content)
        for item in pending:
            if item.id not in ids:
                continue
            item.status = "RESOLVED"
            item.resolved_at = _db_time(occurred, session)
            item.updated_at = _db_time(datetime.now(timezone.utc), session)
            key = f"conversation_pending:{item.id}"
            followup = (await session.exec(select(FollowUp).where(
                FollowUp.workspace_id == row.workspace_id,
                FollowUp.idempotency_key == key,
            ))).first()
            if followup is not None:
                followup.status = "resolved"
                followup.response_status = "answered"
                followup.updated_at = _db_time(datetime.now(timezone.utc), session)
    row.status = "processed"
    await session.flush()
    return state


async def materialize_due_conversation_followups(
    session: AsyncSession, *, now: datetime | None = None, batch_size: int = 100,
) -> int:
    """Convert eligible pending questions into idempotent scheduler waiting rows."""
    if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 100:
        raise ValueError("batch_size must be between 1 and 100")
    now_db = _db_time(now or datetime.now(timezone.utc), session)
    items = list((await session.exec(select(PendingConversationItem).where(
        PendingConversationItem.status == "WAITING_FOR_RESPONSE",
        PendingConversationItem.__table__.c.remind_at.is_not(None),
        PendingConversationItem.__table__.c.remind_at <= now_db,
    ).order_by(PendingConversationItem.__table__.c.remind_at).limit(batch_size))).all())
    created = 0
    for item in items:
        try:
            async with session.begin_nested():
                policy = (await session.exec(select(SourceMonitoringPolicy).where(
                    SourceMonitoringPolicy.workspace_id == item.workspace_id,
                    SourceMonitoringPolicy.user_id == item.user_id,
                    SourceMonitoringPolicy.source == "whatsapp",
                ))).first()
                membership = (await session.exec(select(WorkspaceMembership.id).where(
                    WorkspaceMembership.workspace_id == item.workspace_id,
                    WorkspaceMembership.user_id == item.user_id,
                ))).first()
                instance = (await session.exec(select(WhatsAppInstance).where(
                    WhatsAppInstance.id == item.instance_id,
                    WhatsAppInstance.workspace_id == item.workspace_id,
                    WhatsAppInstance.user_id == item.user_id,
                ))).first()
                source_event = (await session.exec(select(NormalizedEvent).where(
                    NormalizedEvent.id == uuid.UUID(item.source_event),
                ))).first()
                metadata = source_event.metadata_json if source_event is not None else {}
                allowed = (
                    policy is not None and policy.direction == "both"
                    and _policy_allows_conversation(policy, item.conversation_id, metadata)
                    and membership is not None and instance is not None
                    and instance.status in {"connected", "pending_qr"}
                )
                if not allowed:
                    item.status = "RESOLVED"
                    item.resolved_at = _db_time(now_db, session)
                    item.updated_at = _db_time(now_db, session)
                    continue
                key = f"conversation_pending:{item.id}"
                prior = (await session.exec(select(FollowUp).where(
                    FollowUp.workspace_id == item.workspace_id, FollowUp.idempotency_key == key,
                ))).first()
                if prior is None:
                    session.add(FollowUp(
                        workspace_id=item.workspace_id, user_id=item.user_id,
                        source_event_id=uuid.UUID(item.source_event), idempotency_key=key,
                        title=item.summary[:300], summary=item.summary, confidence=item.confidence,
                        status="waiting", remind_at=item.remind_at, conversation_id=item.conversation_id,
                        reason=item.summary, response_status="pending",
                    ))
                    await session.flush()
                    created += 1
                item.status = "DUE"
        except Exception as exc:
            logger.warning("Conversation follow-up item %s failed (%s)", item.id, type(exc).__name__)
            continue
    await session.flush()
    return created
