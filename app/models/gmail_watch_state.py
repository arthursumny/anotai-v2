"""Minimal Gmail push-watch state and bounded history cursor."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Column, DateTime, ForeignKey, JSON, CheckConstraint, Uuid
from sqlalchemy.orm import validates
from sqlmodel import Field, SQLModel


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class GmailWatchState(SQLModel, table=True):
    __tablename__ = "gmail_watch_states"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'needs_resync', 'paused')", name="ck_gmail_watch_state_status"),
    )

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    integration_connection_id: uuid.UUID = Field(sa_column=Column(
        Uuid(), ForeignKey("integration_connections.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
    ))
    # Google history IDs are decimal strings representing non-contiguous int64 values.
    history_id: str | None = Field(default=None, max_length=32)
    expiration_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    last_renewed_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    status: str = Field(default="paused", max_length=20)
    label_ids: list[str] = Field(default_factory=lambda: ["INBOX"], sa_column=Column(JSON, nullable=False, default=lambda: ["INBOX"]))
    retry_count: int = Field(default=0, ge=0)
    last_error_kind: str | None = Field(default=None, max_length=80)
    page_token: str | None = Field(default=None, max_length=2048)
    updated_at: datetime = Field(default_factory=_utcnow, sa_column=Column(DateTime(timezone=True), nullable=False))

    @validates("expiration_at", "last_renewed_at")
    def validate_utc_timestamp(self, _key: str, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Gmail watch timestamps must be timezone-aware")
        return value.astimezone(timezone.utc)
