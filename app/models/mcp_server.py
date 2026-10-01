"""AnotAI — modelo UserMCPServer (conexões MCP por usuário)."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import UniqueConstraint
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UserMCPServer(SQLModel, table=True):
    __tablename__ = "user_mcp_servers"
    __table_args__ = (UniqueConstraint("user_id", "provider_id", name="uq_user_mcp_provider"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True)
    name: str = Field(min_length=1, max_length=120)
    server_url: str = Field(min_length=1, max_length=2000)
    auth_header_key: str | None = Field(default=None, max_length=100)
    auth_secret_name: str | None = Field(default=None, max_length=100)
    provider_id: str | None = Field(default=None, max_length=80, index=True)
    is_active: bool = Field(default=True)
    created_at: datetime = Field(default_factory=_utcnow)
