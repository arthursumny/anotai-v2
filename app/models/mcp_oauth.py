"""OAuth remoto para conexões MCP de cada usuário."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, String, Text, UniqueConstraint
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class UserMCPOAuthCredential(SQLModel, table=True):
    __tablename__ = "user_mcp_oauth_credentials"
    __table_args__ = (UniqueConstraint("server_id", name="uq_mcp_oauth_server"),)

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    server_id: uuid.UUID = Field(foreign_key="user_mcp_servers.id", index=True, ondelete="CASCADE")
    provider_id: str = Field(max_length=80, index=True)
    token_ciphertext: str = Field(sa_column=Column(Text, nullable=False))
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))


class MCPOAuthFlow(SQLModel, table=True):
    __tablename__ = "mcp_oauth_flows"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    user_id: uuid.UUID = Field(foreign_key="users.id", index=True, ondelete="CASCADE")
    provider_id: str = Field(max_length=80, index=True)
    state_hash: str = Field(max_length=64, unique=True, index=True)
    payload_ciphertext: str = Field(sa_column=Column(Text, nullable=False))
    expires_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False, index=True))
    consumed_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    created_at: datetime = Field(default_factory=_utcnow)
