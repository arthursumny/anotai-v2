"""Safe read model for the workspace pendency feed."""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


PendencyKind = Literal[
    "task", "reminder", "follow_up", "action", "approval",
]
PendencySubkind = Literal["suggestion", "active"]


class PendencyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    kind: PendencyKind
    subkind: PendencySubkind | None = None
    title: str
    summary: str | None = None
    confidence: float | None = None
    created_at: datetime
    due_at: datetime | None = None
    source: str | None = None


class PendencyFeed(BaseModel):
    items: list[PendencyRead]
    limit: int
    offset: int
    total: int


class PendencyAcceptRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=300)
    due_at: AwareDatetime | None = None
    remind_at: AwareDatetime | None = None


class PendencyActionResult(BaseModel):
    kind: PendencyKind
    id: uuid.UUID
    status: str


class PendencySnoozeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    until: AwareDatetime


class PendencyEditRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=300)
    due_at: AwareDatetime | None = None
    remind_at: AwareDatetime | None = None
