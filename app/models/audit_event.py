"""Minimal, content-free audit event records."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, Index, JSON, String, Uuid
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AuditEvent(SQLModel, table=True):
    __tablename__ = "audit_events"
    __table_args__ = (
        CheckConstraint("actor IN ('user', 'system')", name="ck_audit_event_actor"),
        Index("ix_audit_events_workspace_created_at", "workspace_id", "created_at"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workspace_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    user_id: uuid.UUID | None = Field(default=None, sa_column=Column(
        Uuid(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True,
    ))
    actor: str = Field(max_length=10, sa_column=Column(String(10), nullable=False))
    event: str = Field(max_length=80, sa_column=Column(String(80), nullable=False))
    action: str = Field(max_length=80, sa_column=Column(String(80), nullable=False))
    status: str = Field(max_length=40, sa_column=Column(String(40), nullable=False))
    source: str = Field(max_length=40, sa_column=Column(String(40), nullable=False))
    target_kind: str = Field(max_length=40, sa_column=Column(String(40), nullable=False))
    target_ref: str | None = Field(default=None, max_length=255, sa_column=Column(String(255), nullable=True))
    metadata_json: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    created_at: datetime = Field(
        default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False, index=True),
    )
