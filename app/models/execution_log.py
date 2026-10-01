"""AnotAI — modelo ExecutionLog."""

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Column, String
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ExecutionStatus(str, enum.Enum):
    SUCCESS = "success"
    FAILED = "failed"


class ExecutionLog(SQLModel, table=True):
    __tablename__ = "execution_logs"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    note_id: uuid.UUID = Field(foreign_key="notes.id", index=True)
    action_type: str = Field(max_length=50)
    # VARCHAR (ver comentário em app/models/note.py): sem ENUM nativo no Postgres.
    status: ExecutionStatus = Field(
        default=ExecutionStatus.SUCCESS,
        sa_column=Column(String(20), nullable=False, index=True),
    )
    payload: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    error_message: str | None = Field(default=None, max_length=4000)
    created_at: datetime = Field(default_factory=_utcnow)
