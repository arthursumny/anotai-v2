"""Safe, content-free audit API response schemas."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AuditEventRead(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor: str
    event: str
    action: str
    status: str
    source: str
    target_kind: str
    target_ref: str | None
    metadata: dict[str, Any]
    created_at: datetime


class AuditEventPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AuditEventRead]
    limit: int = Field(ge=1, le=100)
    offset: int = Field(ge=0)
