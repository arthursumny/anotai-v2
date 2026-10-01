"""AnotAI — schemas da inbox de triagem e do Kanban (Pydantic v2)."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

from app.schemas.note import NoteRead

KanbanStatus = Literal["todo", "in_progress", "done"]


class InboxAccept(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kanban_status: KanbanStatus = "todo"
    due_date: datetime | None = None
    reminder_at: datetime | None = None


class KanbanMove(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kanban_status: KanbanStatus
    due_date: datetime | None = None


class KanbanBoard(BaseModel):
    todo: list[NoteRead] = []
    in_progress: list[NoteRead] = []
    done: list[NoteRead] = []
