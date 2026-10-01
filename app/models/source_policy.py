"""Per-user, per-source monitoring and lightweight filtering policy."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, JSON, String, UniqueConstraint, Uuid
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SourceMonitoringPolicy(SQLModel, table=True):
    __tablename__ = "source_monitoring_policies"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", "source", name="uq_source_policy_workspace_user_source"),
        CheckConstraint("retention_days IS NULL OR retention_days BETWEEN 1 AND 365", name="ck_source_policy_retention_days"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workspace_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    user_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    source: str = Field(sa_column=Column(String(20), nullable=False, index=True))
    mode: str = Field(default="ALL", max_length=20)
    selected_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    direction: str = Field(default="incoming", max_length=20)
    allow_groups: bool = Field(default=True)
    allow_individuals: bool = Field(default=True)
    gmail_label_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    gmail_allowed_senders: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    retention_days: int | None = Field(default=None, ge=1, le=365)
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
