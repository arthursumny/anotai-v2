"""AnotAI — modelo UserSecret (segredos por usuário p/ o dispatcher)."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UserSecret(SQLModel, table=True):
    __tablename__ = "user_secrets"
    __table_args__ = (UniqueConstraint("user_id", "key", name="uq_user_secrets_user_key"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    key: str = Field(min_length=1, max_length=100, index=True)
    value: str = Field(sa_column=Column(Text, nullable=False), repr=False)
    created_at: datetime = Field(default_factory=_utcnow)
