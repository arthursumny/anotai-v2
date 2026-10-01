"""Workspace-level automation mode and per-action configuration."""

import uuid
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import Boolean, CheckConstraint, Column, DateTime, ForeignKey, JSON, String, UniqueConstraint, Uuid
from sqlmodel import Field, SQLModel


AutomationMode = Literal["suggestive", "semi_automatic", "automatic", "disabled"]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AutomationSettings(SQLModel, table=True):
    __tablename__ = "automation_settings"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_automation_settings_workspace_user"),
        CheckConstraint(
            "automation_mode IN ('suggestive', 'semi_automatic', 'automatic', 'disabled')",
            name="ck_automation_settings_mode",
        ),
        CheckConstraint(
            "quiet_start_minute IS NULL OR quiet_start_minute BETWEEN 0 AND 1439",
            name="ck_automation_settings_quiet_start_minute",
        ),
        CheckConstraint(
            "quiet_end_minute IS NULL OR quiet_end_minute BETWEEN 0 AND 1439",
            name="ck_automation_settings_quiet_end_minute",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workspace_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    user_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    automation_mode: AutomationMode = Field(
        default="suggestive", sa_column=Column(String(20), nullable=False),
    )
    action_thresholds: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    action_config: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    notifications_enabled: bool = Field(
        default=True, sa_column=Column(Boolean, nullable=False, default=True, server_default="1"),
    )
    quiet_start_minute: int | None = Field(default=None, ge=0, le=1439)
    quiet_end_minute: int | None = Field(default=None, ge=0, le=1439)
    created_at: datetime = Field(default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))
    updated_at: datetime = Field(default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))
