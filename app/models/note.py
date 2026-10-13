"""AnotAI — modelo Note: documento persistido pelo fluxo de gravação (STT -> LLM)."""

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, String, Text
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class NoteStatus(str, enum.Enum):
    PENDING_REVIEW = "pending_review"
    PROCESSED = "processed"
    DISCARDED = "discarded"


class Note(SQLModel, table=True):
    __tablename__ = "notes"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    template_id: uuid.UUID | None = Field(default=None, foreign_key="templates.id", index=True)
    title: str = Field(default="", max_length=300)
    body: str = Field(default="", sa_column=Column(Text, nullable=False))
    summary: str = Field(default="", sa_column=Column(Text, nullable=False))
    raw_transcription: str = Field(default="")
    structured_data: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    # VARCHAR no Postgres (migração cria String): evita CREATE TYPE de ENUM nativo.
    # A tipagem Python continua sendo NoteStatus (str-Enum) e o Pydantic coage na leitura.
    status: NoteStatus = Field(
        default=NoteStatus.PENDING_REVIEW,
        sa_column=Column(String(20), nullable=False, index=True),
    )
    due_date: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    reminder_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True, index=True)
    )
    snoozed_until: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True, index=True)
    )
    reminder_sent: bool = Field(default=False)
    created_at: datetime = Field(default_factory=_utcnow)
