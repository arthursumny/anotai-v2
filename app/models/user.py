"""AnotAI — modelo User."""

import uuid
from datetime import datetime, timezone

from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True, index=True)
    name: str = Field(min_length=1, max_length=120)
    email: str = Field(unique=True, index=True, max_length=320)
    hashed_password: str
    created_at: datetime = Field(default_factory=_utcnow)
    workspace_id: uuid.UUID | None = Field(default=None, foreign_key="workspaces.id", index=True)
