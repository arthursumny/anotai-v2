"""Validated, provider-independent intelligence classification results."""

import re
from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


class IntelligenceType(StrEnum):
    NOTE = "NOTE"
    TASK = "TASK"
    REMINDER = "REMINDER"
    FOLLOW_UP = "FOLLOW_UP"
    CALENDAR_EVENT = "CALENDAR_EVENT"
    MEMORY = "MEMORY"
    ACTION = "ACTION"
    INFORMATION = "INFORMATION"
    NOT_ACTIONABLE = "NOT_ACTIONABLE"


class IntelligenceResult(BaseModel):
    """One safe classification; never an executable/provider operation."""

    model_config = ConfigDict(extra="forbid", strict=True)

    type: IntelligenceType
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=2000)
    confidence: float = Field(ge=0.0, le=1.0)
    due_at: datetime | None = None
    structured_data: dict[str, str | int | float | bool | None] = Field(default_factory=dict)

    @field_validator("due_at")
    @classmethod
    def _due_at_must_be_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("due_at deve incluir timezone")
        return value

    @field_validator("structured_data")
    @classmethod
    def _safe_structured_data(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > 20:
            raise ValueError("structured_data aceita no máximo 20 campos")
        forbidden_names = re.compile(r"url|uri|credential|password|secret|token|workspace.?id|nango.?connection.?id", re.I)
        for key, item in value.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,49}", key) or forbidden_names.search(key):
                raise ValueError("structured_data contém chave não permitida")
            if isinstance(item, str) and re.search(r"https?://|\bBearer\s+|\b(token|password|secret)\s*[:=]", item, re.I):
                raise ValueError("structured_data contém dado sensível ou URL")
        return value


class IntelligenceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    results: list[IntelligenceResult] = Field(max_length=20)
