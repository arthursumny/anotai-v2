"""References to provider connections held by Nango (never credential storage)."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, JSON, CheckConstraint, UniqueConstraint, Uuid
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class IntegrationConnection(SQLModel, table=True):
    __tablename__ = "integration_connections"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", "provider", name="uq_integration_connection_owner_provider"),
        UniqueConstraint("provider_config_key", "nango_connection_id", name="uq_integration_connection_nango_binding"),
        # Valores permitidos espelham o registro em app/services/provider_registry.py;
        # ampliar exige nova migration (drop + create do check, sem tocar em dados).
        CheckConstraint(
            "provider IN ('google_gmail', 'google_calendar', 'google_sheets', 'notion', 'slack')",
            name="ck_integration_connection_provider",
        ),
        CheckConstraint("status IN ('pending', 'active', 'error', 'disconnected')", name="ck_integration_connection_status"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workspace_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    user_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    provider: str = Field(max_length=32)
    provider_config_key: str = Field(max_length=120)
    nango_connection_id: str = Field(max_length=255)
    account_email: str | None = Field(default=None, max_length=320, index=True)
    display_name: str | None = Field(default=None, max_length=120)
    status: str = Field(default="pending", max_length=20)
    metadata_json: dict = Field(
        default_factory=dict, sa_column=Column("metadata", JSON, nullable=False, default=dict),
    )
    created_at: datetime = Field(default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))
    updated_at: datetime = Field(default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))
