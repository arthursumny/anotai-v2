"""Shared local cleanup for disabling WhatsApp monitoring."""

from sqlalchemy import delete, update
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.action_item import FollowUp
from app.models.conversation_state import ConversationState
from app.models.normalized_event import NormalizedEvent
from app.models.pending_conversation_item import PendingConversationItem
from app.models.whatsapp_instance import WhatsAppInstance


async def purge_whatsapp_data(session: AsyncSession, workspace_id, user_id) -> None:
    """Delete WA conversation state and dismiss unresolved WhatsApp follow-ups."""
    instance_ids = select(WhatsAppInstance.id).where(
        WhatsAppInstance.workspace_id == workspace_id,
        WhatsAppInstance.user_id == user_id,
    )
    await session.exec(delete(ConversationState).where(
        ConversationState.workspace_id == workspace_id,
        ConversationState.user_id == user_id,
        ConversationState.instance_id.in_(instance_ids),
    ))
    await session.exec(delete(PendingConversationItem).where(
        PendingConversationItem.workspace_id == workspace_id,
        PendingConversationItem.user_id == user_id,
        PendingConversationItem.instance_id.in_(instance_ids),
    ))
    whatsapp_event_ids = select(NormalizedEvent.id).where(
        NormalizedEvent.workspace_id == workspace_id,
        NormalizedEvent.user_id == user_id,
        NormalizedEvent.source == "whatsapp",
    )
    await session.exec(update(FollowUp).where(
        FollowUp.workspace_id == workspace_id,
        FollowUp.user_id == user_id,
        FollowUp.source_event_id.in_(whatsapp_event_ids),
        FollowUp.status.in_(["waiting", "due"]),
        FollowUp.response_status == "pending",
    ).values(status="cancelled", response_status="dismissed"))
