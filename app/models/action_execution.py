"""Idempotent, leaseable executions of individual actions."""

import uuid
from datetime import datetime, timezone
from typing import Literal

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    JSON,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlmodel import Field, SQLModel


ActionExecutionStatus = Literal[
    "pending", "claimed", "succeeded", "failed", "awaiting_confirmation",
]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ActionExecution(SQLModel, table=True):
    __tablename__ = "action_executions"
    __table_args__ = (
        UniqueConstraint("workspace_id", "idempotency_key", name="uq_action_execution_workspace_key"),
        CheckConstraint(
            "status IN ('pending', 'claimed', 'succeeded', 'failed', 'awaiting_confirmation')",
            name="ck_action_execution_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_action_execution_attempts_nonnegative"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workspace_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    user_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    workflow_execution_id: uuid.UUID | None = Field(default=None, sa_column=Column(
        Uuid(), ForeignKey("workflow_executions.id", ondelete="SET NULL"), nullable=True, index=True,
    ))
    workflow_step_id: uuid.UUID | None = Field(default=None, sa_column=Column(
        Uuid(), ForeignKey("workflow_steps.id", ondelete="SET NULL"), nullable=True, index=True,
    ))
    trigger_type: str = Field(max_length=50, index=True)
    source_id: str | None = Field(default=None, max_length=320)
    action_type: str = Field(max_length=50, index=True)
    idempotency_key: str = Field(max_length=320)
    policy_snapshot: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    # Canonical output of validate_action only; writers must not put secrets or
    # provider credentials here, and this input must never be included in logs.
    action_input: dict | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    workflow_version: int | None = Field(default=None, ge=1)
    status: ActionExecutionStatus = Field(default="pending", sa_column=Column(String(30), nullable=False, index=True))
    lease_expires_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True, index=True),
    )
    attempts: int = Field(default=0, ge=0)
    last_error: str | None = Field(default=None, max_length=2000)
    created_at: datetime = Field(default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))
    updated_at: datetime = Field(default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))
