from datetime import datetime, timezone
import uuid

import pytest

from app.core.config import settings
from app.models.gmail_watch_state import GmailWatchState
from app.models.integration_connection import IntegrationConnection
from app.models.source_policy import SourceMonitoringPolicy
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMembership
from app import worker


async def _setup(session, *, policy=True, watch_status="active"):
    user = User(name="Gmail job", email=f"{uuid.uuid4()}@example.com", hashed_password="unused")
    workspace = Workspace(name="Gmail job test", timezone="UTC")
    session.add(workspace)
    await session.flush()
    user.workspace_id = workspace.id
    session.add(user)
    await session.flush()
    session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="OWNER"))
    connection = IntegrationConnection(
        workspace_id=workspace.id, user_id=user.id, provider="google_gmail",
        status="active", provider_config_key="gmail-test", nango_connection_id="nango-1",
    )
    session.add(connection)
    await session.flush()
    session.add(GmailWatchState(
        integration_connection_id=connection.id, status=watch_status, history_id="10",
        expiration_at=datetime.now(timezone.utc),
    ))
    if policy:
        session.add(SourceMonitoringPolicy(
            workspace_id=workspace.id, user_id=user.id, source="gmail", mode="ALL",
        ))
    await session.flush()
    return connection


class _Provider:
    def __init__(self, *, raises=False):
        self.calls = []
        self.raises = raises

    async def process_history(self, session, connection):
        self.calls.append((session, connection))
        if self.raises:
            raise RuntimeError("sensitive payload must not appear in output")
        return 3


@pytest.mark.asyncio
async def test_gmail_history_job_does_not_create_client_without_policy_or_active_watch(
    worker_db, monkeypatch
):
    connection = await _setup(worker_db, policy=False)
    monkeypatch.setattr(settings, "NANGO_ENV_KEY", "test-key")
    monkeypatch.setattr(settings, "NANGO_BASE_URL", "https://nango.example")
    monkeypatch.setattr(worker, "NangoClient", lambda **kwargs: pytest.fail("unexpected Nango client"))

    result = await worker.gmail_history_job({}, str(connection.id))
    assert result["ok"] is False
    assert result["error"] == "gmail_monitoring_unavailable"


@pytest.mark.asyncio
async def test_gmail_history_job_paused_watch_does_not_create_client(worker_db, monkeypatch):
    connection = await _setup(worker_db, watch_status="paused")
    monkeypatch.setattr(settings, "NANGO_ENV_KEY", "test-key")
    monkeypatch.setattr(settings, "NANGO_BASE_URL", "https://nango.example")
    monkeypatch.setattr(worker, "NangoClient", lambda **kwargs: pytest.fail("unexpected Nango client"))

    result = await worker.gmail_history_job({}, str(connection.id))
    assert result["ok"] is False
    assert result["error"] == "gmail_monitoring_unavailable"


@pytest.mark.asyncio
async def test_gmail_history_job_active_connection_processes_durable_cursor(worker_db, monkeypatch):
    connection = await _setup(worker_db)
    provider = _Provider()
    client_options = []
    monkeypatch.setattr(settings, "NANGO_ENV_KEY", "test-key")
    monkeypatch.setattr(settings, "NANGO_BASE_URL", "https://nango.example")
    monkeypatch.setattr(worker, "NangoClient", lambda **kwargs: client_options.append(kwargs) or object())
    monkeypatch.setattr(worker, "GmailSourceProvider", lambda nango: provider)

    result = await worker.gmail_history_job({}, str(connection.id), history_id="999999")
    assert result == {"ok": True, "processed": 3}
    assert client_options == [{"api_key": "test-key", "base_url": "https://nango.example"}]
    assert provider.calls == [(worker_db, connection)]


@pytest.mark.asyncio
async def test_gmail_history_job_invalid_id_or_missing_config_does_not_create_client(monkeypatch):
    monkeypatch.setattr(worker, "NangoClient", lambda **kwargs: pytest.fail("unexpected Nango client"))
    monkeypatch.setattr(settings, "NANGO_ENV_KEY", "test-key")
    monkeypatch.setattr(settings, "NANGO_BASE_URL", "https://nango.example")
    assert (await worker.gmail_history_job({}, "invalid"))["error"] == "invalid_connection_id"

    monkeypatch.setattr(settings, "NANGO_ENV_KEY", "")
    assert (await worker.gmail_history_job({}, str(uuid.uuid4())))["error"] == "nango_not_configured"


@pytest.mark.asyncio
async def test_gmail_history_job_failure_is_sanitized_and_never_raises(worker_db, monkeypatch):
    connection = await _setup(worker_db)
    provider = _Provider(raises=True)
    monkeypatch.setattr(settings, "NANGO_ENV_KEY", "test-key")
    monkeypatch.setattr(settings, "NANGO_BASE_URL", "https://nango.example")
    monkeypatch.setattr(worker, "NangoClient", lambda **kwargs: object())
    monkeypatch.setattr(worker, "GmailSourceProvider", lambda nango: provider)

    result = await worker.gmail_history_job({}, str(connection.id))
    assert result == {"ok": False, "error": "processing_failed"}


def test_gmail_history_job_is_registered_with_arq():
    assert worker.gmail_history_job in worker.WorkerSettings.functions
