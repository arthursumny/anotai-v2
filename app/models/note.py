"""AnotAI — modelo Note (intentional + continuous + Kanban)."""

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Column, DateTime, String
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class NoteStatus(str, enum.Enum):
    PENDING_REVIEW = "pending_review"
    PROCESSED = "processed"
    # Legado: notas antigas sem template. Novas notas sem match viram
    # "nota geral" (PROCESSED + template_id=None). Mantido p/ leitura.
    NO_MATCH = "no_match"
    SUGGESTED = "suggested"
    DISCARDED = "discarded"


class KanbanColumn(str, enum.Enum):
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class CaptureMode(str, enum.Enum):
    INTENTIONAL = "intentional"
    CONTINUOUS = "continuous"


class Note(SQLModel, table=True):
    __tablename__ = "notes"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    template_id: uuid.UUID | None = Field(default=None, foreign_key="templates.id", index=True)
    title: str = Field(default="", max_length=300)
    raw_transcription: str = Field(default="")
    structured_data: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    # VARCHAR no Postgres (migração cria String): evita CREATE TYPE de ENUM nativo.
    # A tipagem Python continua sendo NoteStatus (str-Enum) e o Pydantic coage na leitura.
    status: NoteStatus = Field(
        default=NoteStatus.PENDING_REVIEW,
        sa_column=Column(String(20), nullable=False, index=True),
    )
    # --- Kanban / captura contínua (valores validados na API) ---
    kanban_status: str | None = Field(default=None, max_length=20, index=True)
    due_date: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    reminder_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True, index=True)
    )
    snoozed_until: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True, index=True)
    )
    reminder_sent: bool = Field(default=False)
    capture_mode: str = Field(default=CaptureMode.INTENTIONAL.value, max_length=20)
    suggested_action: str | None = Field(default=None, max_length=200)
    created_at: datetime = Field(default_factory=_utcnow)
