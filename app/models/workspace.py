"""Workspaces and per-user membership."""

import uuid
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import Column, ForeignKey, UniqueConstraint, Uuid
from sqlalchemy.orm import validates
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Workspace(SQLModel, table=True):
    __tablename__ = "workspaces"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    name: str = Field(max_length=120)
    is_personal: bool = Field(default=True)
    timezone: str = Field(default="UTC", max_length=64)
    locale: str = Field(default="pt-BR", max_length=20)
    created_at: datetime = Field(default_factory=_utcnow)

    @validates("timezone")
    def validate_timezone(self, _key: str, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("timezone deve ser um identificador IANA válido") from exc
        return value


class WorkspaceMembership(SQLModel, table=True):
    __tablename__ = "workspace_memberships"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id", name="uq_workspace_membership_user"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workspace_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    user_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    role: str = Field(default="OWNER", max_length=20)
    created_at: datetime = Field(default_factory=_utcnow)
