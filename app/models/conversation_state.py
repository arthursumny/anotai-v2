"""Minimal WhatsApp conversation cursor and message timestamps."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, JSON, UniqueConstraint, Uuid
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ConversationState(SQLModel, table=True):
    __tablename__ = "conversation_states"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "user_id", "instance_id", "conversation_id",
            name="uq_conversation_state_owner_instance_conversation",
        ),
        CheckConstraint("source = 'whatsapp'", name="ck_conversation_state_source"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workspace_id: uuid.UUID = Field(sa_column=Column(Uuid(), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True))
    user_id: uuid.UUID = Field(sa_column=Column(Uuid(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True))
    instance_id: uuid.UUID = Field(sa_column=Column(Uuid(), ForeignKey("whatsapp_instances.id", ondelete="CASCADE"), nullable=False, index=True))
    source: str = Field(default="whatsapp", max_length=20)
    conversation_id: str = Field(max_length=255)
    last_incoming_message_id: str | None = Field(default=None, max_length=255)
    last_outgoing_message_id: str | None = Field(default=None, max_length=255)
    last_processed_message_id: str | None = Field(default=None, max_length=255)
    last_incoming_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    last_outgoing_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    last_processed_at: datetime | None = Field(default=None, sa_type=DateTime(timezone=True))
    metadata_json: dict = Field(default_factory=dict, sa_column=Column("metadata", JSON, nullable=False, default=dict))
    created_at: datetime = Field(default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))
    updated_at: datetime = Field(default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))
