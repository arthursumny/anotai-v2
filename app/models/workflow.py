"""Workflow definitions, ordered steps, and idempotent execution records."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Workflow(SQLModel, table=True):
    __tablename__ = "workflows"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workspace_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    user_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    title: str = Field(max_length=200)
    description: str = Field(default="", sa_column=Column(Text(), nullable=False))
    trigger_type: str = Field(max_length=50, index=True)
    conditions: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    enabled: bool = Field(default=True, index=True)
    version: int = Field(default=1, ge=1)
    created_at: datetime = Field(default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))
    updated_at: datetime = Field(default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))


class WorkflowStep(SQLModel, table=True):
    __tablename__ = "workflow_steps"
    __table_args__ = (UniqueConstraint("workflow_id", "position", name="uq_workflow_steps_position"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workflow_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    position: int = Field(ge=0)
    action_type: str = Field(max_length=50)
    input_template: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    require_confirmation: bool = Field(default=False)
    enabled: bool = Field(default=True, index=True)
    created_at: datetime = Field(default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))
    updated_at: datetime = Field(default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))


class WorkflowExecution(SQLModel, table=True):
    __tablename__ = "workflow_executions"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "workflow_id", "idempotency_key",
            name="uq_workflow_execution_idempotency",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workspace_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    user_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    workflow_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("workflows.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    trigger_type: str = Field(max_length=50, index=True)
    source_id: str | None = Field(default=None, max_length=320)
    idempotency_key: str = Field(max_length=320)
    status: str = Field(default="pending", max_length=30, index=True)
    started_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    finished_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True, index=True))
    metadata_json: dict = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    created_at: datetime = Field(default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))
    updated_at: datetime = Field(default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))
