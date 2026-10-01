"""Workspace-scoped preferences for routing incoming events to owned templates."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, String, Uuid
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TemplateRoutingRule(SQLModel, table=True):
    __tablename__ = "template_routing_rules"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workspace_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    user_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    template_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("templates.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    context: str | None = Field(default=None, max_length=120)
    capture_mode: str | None = Field(default=None, max_length=20)
    priority: int = Field(default=100, ge=0, le=1000, index=True)
    enabled: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
