"""Public schemas for Nango-backed Google integrations."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StrictBool


IntegrationProvider = Literal["google_gmail", "google_calendar", "google_sheets", "notion", "slack"]
# Provedores que a tela de conexão pode abrir HOJE via POST /integrations/sessions
# (os demais valores de IntegrationProvider ainda são somente mapeamento — ver
# app/services/provider_registry.py).
ConnectableIntegrationProvider = Literal["google_gmail", "google_calendar", "slack", "notion"]
IntegrationStatus = Literal["pending", "active", "error", "disconnected"]


class IntegrationSessionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: ConnectableIntegrationProvider


class IntegrationSessionRead(BaseModel):
    session_token: str
    expires_at: datetime


class GmailResumeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    accept_gap: StrictBool


class IntegrationConnectionRead(BaseModel):
    id: UUID
    provider: IntegrationProvider
    account_email: str | None
    display_name: str | None
    status: IntegrationStatus
    monitor_enabled: bool
    gmail_watch_status: Literal["paused", "active", "needs_resync"] | None = None
    expiration_at: datetime | None = None
