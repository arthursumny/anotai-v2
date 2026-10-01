"""Normalized, workspace-scoped events accepted from connected sources."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, JSON, String, UniqueConstraint, Uuid
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class NormalizedEvent(SQLModel, table=True):
    __tablename__ = "normalized_events"
    __table_args__ = (
        UniqueConstraint("workspace_id", "source", "external_id", name="uq_normalized_event_external"),
        CheckConstraint(
            "source NOT IN ('whatsapp', 'gmail') OR external_id IS NOT NULL",
            name="ck_normalized_event_external_id",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workspace_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    user_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    source: str = Field(sa_column=Column(String(20), nullable=False, index=True))
    type: str = Field(sa_column=Column(String(80), nullable=False))
    external_id: str | None = Field(default=None, max_length=320)
    conversation_id: str | None = Field(default=None, max_length=320, index=True)
    sender: str | None = Field(default=None, max_length=320)
    subject: str | None = Field(default=None, max_length=500)
    content: str = Field(default="")
    occurred_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    metadata_json: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    status: str = Field(default="accepted", max_length=30)
    created_at: datetime = Field(default_factory=_utcnow)
