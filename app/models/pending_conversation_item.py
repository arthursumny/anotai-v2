"""Separate, bounded follow-up state for WhatsApp questions awaiting a reply."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, String, UniqueConstraint, Uuid
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PendingConversationItem(SQLModel, table=True):
    __tablename__ = "pending_conversation_items"
    __table_args__ = (
        UniqueConstraint("workspace_id", "source_event", name="uq_pending_conversation_workspace_event"),
        CheckConstraint("source = 'whatsapp'", name="ck_pending_conversation_source"),
        CheckConstraint("status IN ('WAITING_FOR_RESPONSE', 'RESOLVED', 'DUE')", name="ck_pending_conversation_status"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="ck_pending_conversation_confidence"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workspace_id: uuid.UUID = Field(sa_column=Column(Uuid(), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True))
    user_id: uuid.UUID = Field(sa_column=Column(Uuid(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True))
    instance_id: uuid.UUID = Field(sa_column=Column(Uuid(), ForeignKey("whatsapp_instances.id", ondelete="CASCADE"), nullable=False, index=True))
    conversation_id: str = Field(max_length=255)
    source: str = Field(default="whatsapp", max_length=20)
    source_event: str = Field(max_length=255)
    message_id: str = Field(max_length=255)
    summary: str = Field(max_length=500)
    asked_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    remind_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True, index=True))
    status: str = Field(default="WAITING_FOR_RESPONSE", max_length=24)
    confidence: float = Field(ge=0.0, le=1.0)
    resolved_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    created_at: datetime = Field(default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))
    updated_at: datetime = Field(default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))
