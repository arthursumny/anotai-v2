"""Public schemas for Nango-backed Google integrations."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, StrictBool


IntegrationProvider = Literal[
    "google_gmail", "google_calendar", "google_sheets", "google_tasks", "google_drive", "notion", "slack",
]
# Provedores que a tela de conexão pode abrir HOJE via POST /integrations/sessions
# (espelha app/api/v1/integrations.py::_provider_configs; ver
# app/services/provider_registry.py para status e capacidades).
ConnectableIntegrationProvider = Literal[
    "google_gmail", "google_calendar", "google_sheets", "google_tasks", "google_drive", "slack", "notion",
]
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
