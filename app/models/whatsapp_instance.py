"""Per-workspace Evolution instance binding; secrets are never displayed."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import CheckConstraint, Column, DateTime, ForeignKey, String, UniqueConstraint, Uuid
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WhatsAppInstance(SQLModel, table=True):
    __tablename__ = "whatsapp_instances"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id", "user_id", name="uq_whatsapp_instance_workspace_user"
        ),
        UniqueConstraint("go_instance_name", name="uq_whatsapp_instance_go_name"),
        CheckConstraint(
            "status IN ('pending_qr', 'connected', 'disconnected', 'error')",
            name="ck_whatsapp_instance_status",
        ),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    workspace_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    user_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True,
    ))
    go_instance_name: str = Field(max_length=120)
    go_instance_id: str | None = Field(default=None, max_length=255)
    token_ciphertext: str = Field(sa_column=Column(String, nullable=False), repr=False)
    webhook_secret_hash: str = Field(max_length=64, repr=False)
    status: str = Field(default="pending_qr", max_length=20)
    phone_jid: str | None = Field(default=None, max_length=255)
    created_at: datetime = Field(default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))
    updated_at: datetime = Field(default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))
