"""Native task responses shared by task creation and Kanban."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict

KanbanStatus = Literal["todo", "in_progress", "done"]


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True, frozen=True)

    id: uuid.UUID
    title: str
    summary: str
    status: str
    kanban_status: KanbanStatus
    due_at: datetime | None
    remind_at: datetime | None
    source_note_id: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
