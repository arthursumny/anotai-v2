"""Rendered documents generated from events, notes, and template versions."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, JSON, String, Text, UniqueConstraint, Uuid
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class GeneratedDocument(SQLModel, table=True):
    __tablename__ = "generated_documents"
    __table_args__ = (UniqueConstraint(
        "workspace_id", "note_id", name="uq_generated_documents_workspace_note",
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
    note_id: uuid.UUID | None = Field(default=None, sa_column=Column(
        Uuid(), ForeignKey("notes.id", ondelete="SET NULL"), nullable=True, index=True,
    ))
    template_id: uuid.UUID | None = Field(default=None, sa_column=Column(
        Uuid(), ForeignKey("templates.id", ondelete="SET NULL"), nullable=True, index=True,
    ))
    template_version: str = Field(max_length=80)
    structured_data: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    rendered_content: str = Field(sa_column=Column(Text(), nullable=False))
    status: str = Field(default="generated", max_length=30)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
