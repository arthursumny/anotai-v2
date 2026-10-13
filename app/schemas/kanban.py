"""AnotAI — schemas do quadro Kanban (Pydantic v2)."""

from datetime import datetime

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from app.schemas.task import KanbanStatus, TaskRead


class KanbanMove(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kanban_status: KanbanStatus
    due_at: datetime | None = Field(default=None, validation_alias=AliasChoices("due_at", "due_date"))


class KanbanBoard(BaseModel):
    todo: list[TaskRead] = Field(default_factory=list)
    in_progress: list[TaskRead] = Field(default_factory=list)
    done: list[TaskRead] = Field(default_factory=list)
