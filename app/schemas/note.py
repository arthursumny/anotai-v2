"""AnotAI — schemas de Note / ExecutionLog / áudio."""

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from app.models.execution_log import ExecutionStatus
from app.models.note import NoteStatus


class ExecutionLogRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    note_id: uuid.UUID
    action_type: str
    status: ExecutionStatus
    payload: dict[str, Any]
    error_message: str | None
    created_at: datetime


class NoteRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    template_id: uuid.UUID | None
    title: str = ""
    raw_transcription: str
    structured_data: dict[str, Any] | None
    status: NoteStatus
    kanban_status: str | None = None
    due_date: datetime | None = None
    reminder_at: datetime | None = None
    reminder_sent: bool = False
    capture_mode: str = "intentional"
    suggested_action: str | None = None
    created_at: datetime
    logs: list[ExecutionLogRead] = []


class AudioProcessAccepted(BaseModel):
    note_id: uuid.UUID
    job_id: str
    status: str = "queued"
    message: str = "Áudio enfileirado. Consulte GET /notes/{id} para acompanhar."


class ReclassifyRequest(BaseModel):
    template_id: uuid.UUID


class NoteUpdate(BaseModel):
    """Edição human-in-the-loop: transcription e/ou JSON corrigidos à mão."""

    model_config = ConfigDict(extra="forbid")

    raw_transcription: str | None = None
    structured_data: dict[str, Any] | None = None
    template_id: uuid.UUID | None = None
    title: str | None = None
    kanban_status: str | None = None
    due_date: datetime | None = None
    reminder_at: datetime | None = None
