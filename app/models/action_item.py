"""Distinct task, reminder, follow-up, and action suggestion records."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, Float, String, Text, UniqueConstraint, Uuid
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Task(SQLModel, table=True):
    __tablename__ = "tasks"
    __table_args__ = (UniqueConstraint("workspace_id", "idempotency_key", name="uq_tasks_workspace_key"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workspace_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    user_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    source_event_id: uuid.UUID | None = Field(default=None, sa_column=Column(
        Uuid(), ForeignKey("normalized_events.id", ondelete="SET NULL"), nullable=True, index=True,
    ))
    idempotency_key: str = Field(max_length=320)
    title: str = Field(max_length=300)
    summary: str = Field(default="", sa_column=Column(Text(), nullable=False))
    confidence: float | None = Field(default=None, sa_column=Column(Float(), nullable=True))
    status: str = Field(default="open", max_length=30)
    due_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True, index=True))
    remind_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True, index=True))
    snoozed_until: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True, index=True))
    timezone: str = Field(default="UTC", max_length=64)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class Reminder(SQLModel, table=True):
    __tablename__ = "reminders"
    __table_args__ = (UniqueConstraint("workspace_id", "idempotency_key", name="uq_reminders_workspace_key"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workspace_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    user_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    source_event_id: uuid.UUID | None = Field(default=None, sa_column=Column(
        Uuid(), ForeignKey("normalized_events.id", ondelete="SET NULL"), nullable=True, index=True,
    ))
    idempotency_key: str = Field(max_length=320)
    title: str = Field(max_length=300)
    summary: str = Field(default="", sa_column=Column(Text(), nullable=False))
    confidence: float | None = Field(default=None, sa_column=Column(Float(), nullable=True))
    status: str = Field(default="scheduled", max_length=30)
    due_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True, index=True))
    remind_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, index=True))
    snoozed_until: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True, index=True))
    timezone: str = Field(default="UTC", max_length=64)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class FollowUp(SQLModel, table=True):
    __tablename__ = "follow_ups"
    __table_args__ = (UniqueConstraint("workspace_id", "idempotency_key", name="uq_follow_ups_workspace_key"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workspace_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    user_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    source_event_id: uuid.UUID | None = Field(default=None, sa_column=Column(
        Uuid(), ForeignKey("normalized_events.id", ondelete="SET NULL"), nullable=True, index=True,
    ))
    idempotency_key: str = Field(max_length=320)
    title: str = Field(max_length=300)
    summary: str = Field(default="", sa_column=Column(Text(), nullable=False))
    confidence: float | None = Field(default=None, sa_column=Column(Float(), nullable=True))
    status: str = Field(default="open", max_length=30)
    due_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True, index=True))
    remind_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True, index=True))
    snoozed_until: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True, index=True))
    timezone: str = Field(default="UTC", max_length=64)
    conversation_id: str | None = Field(default=None, max_length=320, index=True)
    contact: str | None = Field(default=None, max_length=320)
    reason: str = Field(default="", sa_column=Column(Text(), nullable=False))
    response_status: str = Field(default="pending", max_length=30)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)


class ActionSuggestion(SQLModel, table=True):
    __tablename__ = "action_suggestions"
    __table_args__ = (UniqueConstraint(
        "workspace_id", "idempotency_key", name="uq_action_suggestions_workspace_key",
    ),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workspace_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    user_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    source_event_id: uuid.UUID | None = Field(default=None, sa_column=Column(
        Uuid(), ForeignKey("normalized_events.id", ondelete="SET NULL"), nullable=True, index=True,
    ))
    idempotency_key: str = Field(max_length=320)
    title: str = Field(max_length=300)
    summary: str = Field(default="", sa_column=Column(Text(), nullable=False))
    confidence: float | None = Field(default=None, sa_column=Column(Float(), nullable=True))
    status: str = Field(default="suggested", max_length=30)
    due_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True, index=True))
    remind_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True, index=True))
    snoozed_until: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True, index=True))
    timezone: str = Field(default="UTC", max_length=64)
    suggestion_type: str = Field(default="task", max_length=40)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
