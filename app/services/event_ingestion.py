"""Policy-first ingestion for the bounded set of supported event sources."""

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parseaddr
from enum import Enum
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.normalized_event import NormalizedEvent
from app.models.source_policy import SourceMonitoringPolicy
from app.models.workspace import WorkspaceMembership


class EventSource(str, Enum):
    AUDIO = "audio"
    WHATSAPP = "whatsapp"
    GMAIL = "gmail"


class UnsupportedEventSource(ValueError):
    """Raised when a provider attempts to ingest an unsupported source."""


class UnauthorizedWorkspace(PermissionError):
    """Raised when the user does not belong to the requested workspace."""


@dataclass(frozen=True, slots=True)
class EventCandidate:
    workspace_id: UUID
    user_id: UUID
    source: str
    type: str
    occurred_at: datetime
    content: str
    external_id: str | None = None
    conversation_id: str | None = None
    sender: str | None = None
    subject: str | None = None
    from_me: bool = False
    is_group: bool = False
    gmail_label_ids: tuple[str, ...] = ()


async def _existing_event(
    session: AsyncSession, candidate: EventCandidate, source: EventSource,
) -> NormalizedEvent | None:
    if candidate.external_id is None:
        return None
    return (await session.exec(select(NormalizedEvent).where(
        NormalizedEvent.workspace_id == candidate.workspace_id,
        NormalizedEvent.source == source.value,
        NormalizedEvent.external_id == candidate.external_id,
    ))).first()


def _policy_allows(policy: SourceMonitoringPolicy, candidate: EventCandidate, source: EventSource) -> bool:
    mode = policy.mode.upper()
    if mode not in {"ALL", "ALLOWLIST", "BLOCKLIST"}:
        return False
    if source is not EventSource.GMAIL:
        direction = "outgoing" if candidate.from_me else "incoming"
        policy_direction = policy.direction.lower()
        if policy_direction not in {"incoming", "outgoing", "both"}:
            return False
        if policy_direction != "both" and policy_direction != direction:
            return False

    if source is EventSource.WHATSAPP:
        if candidate.is_group and not policy.allow_groups:
            return False
        if not candidate.is_group and not policy.allow_individuals:
            return False
    if source is EventSource.GMAIL:
        labels = {label.upper() for label in candidate.gmail_label_ids}
        if labels.intersection({"SPAM", "TRASH"}):
            return False
        allowed_labels = {label.upper() for label in (policy.gmail_label_ids or ["INBOX"])}
        if allowed_labels and not allowed_labels.intersection(labels):
            return False
        sender = parseaddr(candidate.sender or "")[1].strip().lower()
        if not sender:
            return False
        if policy.gmail_allowed_senders:
            allowed_senders = {address.strip().lower() for address in policy.gmail_allowed_senders}
            if sender not in allowed_senders:
                return False

    selected = set(policy.selected_ids)
    conversation_id = candidate.conversation_id
    if source is EventSource.GMAIL:
        selected_values = {address.strip().lower() for address in selected}
        sender = parseaddr(candidate.sender or "")[1].strip().lower()
        if mode == "ALLOWLIST" and sender not in selected_values:
            return False
        if mode == "BLOCKLIST" and sender in selected_values:
            return False
    else:
        if mode == "ALLOWLIST" and (conversation_id is None or conversation_id not in selected):
            return False
        if mode == "BLOCKLIST" and conversation_id is not None and conversation_id in selected:
            return False
    return True


async def gmail_policy_allows(candidate: EventCandidate, session: AsyncSession) -> bool:
    """Check Gmail opt-in and existing source filters without persisting data."""
    if candidate.source.lower() != EventSource.GMAIL.value:
        return False
    policy = (await session.exec(select(SourceMonitoringPolicy).where(
        SourceMonitoringPolicy.workspace_id == candidate.workspace_id,
        SourceMonitoringPolicy.user_id == candidate.user_id,
        SourceMonitoringPolicy.source == EventSource.GMAIL.value,
    ))).first()
    return policy is not None and _policy_allows(policy, candidate, EventSource.GMAIL)


async def whatsapp_policy_allows(candidate: EventCandidate, session: AsyncSession) -> bool:
    """Revalidate current WhatsApp membership and policy without persisting.

    Call this after any potentially long-running work and before materializing
    derived data. Missing policy or membership is always a denial.
    """
    if candidate.source.lower() != EventSource.WHATSAPP.value:
        return False
    membership = await session.exec(select(WorkspaceMembership.id).where(
        WorkspaceMembership.workspace_id == candidate.workspace_id,
        WorkspaceMembership.user_id == candidate.user_id,
    ))
    if membership.first() is None:
        return False
    policy = (await session.exec(select(SourceMonitoringPolicy).where(
        SourceMonitoringPolicy.workspace_id == candidate.workspace_id,
        SourceMonitoringPolicy.user_id == candidate.user_id,
        SourceMonitoringPolicy.source == EventSource.WHATSAPP.value,
    ))).first()
    return policy is not None and _policy_allows(policy, candidate, EventSource.WHATSAPP)


async def ingest(candidate: EventCandidate, session: AsyncSession) -> NormalizedEvent | None:
    """Apply membership and opt-in/filter policy before storing any event content.

    Returns the new or existing idempotent event; returns ``None`` when policy
    filters it. The function flushes within a savepoint but never commits the
    caller's transaction.
    """
    try:
        source = EventSource(candidate.source.lower())
    except (AttributeError, ValueError) as exc:
        raise UnsupportedEventSource("Fonte de evento não suportada") from exc
    if source in {EventSource.WHATSAPP, EventSource.GMAIL} and not candidate.external_id:
        raise ValueError("external_id é obrigatório para fontes externas")
    if source is EventSource.WHATSAPP and (
        candidate.conversation_id is None or not candidate.conversation_id.strip()
    ):
        return None
    if not candidate.type or len(candidate.type) > 80:
        raise ValueError("type deve conter de 1 a 80 caracteres")
    if candidate.occurred_at.tzinfo is None or candidate.occurred_at.utcoffset() is None:
        raise ValueError("occurred_at deve incluir um timezone explícito")

    membership = await session.exec(select(WorkspaceMembership.id).where(
        WorkspaceMembership.workspace_id == candidate.workspace_id,
        WorkspaceMembership.user_id == candidate.user_id,
    ))
    if membership.first() is None:
        raise UnauthorizedWorkspace("Usuário não pertence ao workspace informado")

    policy = (await session.exec(select(SourceMonitoringPolicy).where(
        SourceMonitoringPolicy.workspace_id == candidate.workspace_id,
        SourceMonitoringPolicy.user_id == candidate.user_id,
        SourceMonitoringPolicy.source == source.value,
    ))).first()
    if policy is None:
        if source in {EventSource.WHATSAPP, EventSource.GMAIL}:
            return None
    elif not _policy_allows(policy, candidate, source):
        return None

    existing = await _existing_event(session, candidate, source)
    if existing is not None:
        return existing if existing.user_id == candidate.user_id else None

    occurred_at = candidate.occurred_at
    zone = getattr(occurred_at.tzinfo, "key", str(occurred_at.tzinfo))
    event = NormalizedEvent(
        workspace_id=candidate.workspace_id,
        user_id=candidate.user_id,
        source=source.value,
        type=candidate.type,
        external_id=candidate.external_id,
        conversation_id=candidate.conversation_id,
        sender=candidate.sender,
        subject=candidate.subject,
        content=candidate.content,
        occurred_at=occurred_at.astimezone(timezone.utc),
        metadata_json={
            "direction": "outgoing" if candidate.from_me and source is not EventSource.GMAIL else "incoming",
            "is_group": candidate.is_group,
            "timezone": zone,
            "gmail_label_ids": list(candidate.gmail_label_ids) if source is EventSource.GMAIL else [],
        },
        status="accepted",
    )
    try:
        async with session.begin_nested():
            session.add(event)
            await session.flush()
    except IntegrityError:
        duplicate = await _existing_event(session, candidate, source)
        if duplicate is not None and duplicate.user_id == candidate.user_id:
            return duplicate
        if duplicate is not None:
            return None
        raise
    return event
