from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlmodel import select

from app import worker
from app.core.config import settings
from app.models.gmail_watch_state import GmailWatchState
from app.models.integration_connection import IntegrationConnection
from app.models.source_policy import SourceMonitoringPolicy
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMembership
from app.services import gmail_watch_jobs
from app.services import nango_client


async def _watch(session, *, policy=True, status="active", history_id="cursor", updated_at=None):
    user = User(name="Reconcile", email=f"{uuid.uuid4()}@example.com", hashed_password="unused")
    workspace = Workspace(name="Reconcile", timezone="UTC")
    session.add(workspace)
    await session.flush()
    user.workspace_id = workspace.id
    session.add(user)
    await session.flush()
    session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="OWNER"))
    connection = IntegrationConnection(
        workspace_id=workspace.id, user_id=user.id, provider="google_gmail", status="active",
        provider_config_key="gmail-test", nango_connection_id=f"nango-{uuid.uuid4()}",
        account_email="person@example.com",
    )
    session.add(connection)
    await session.flush()
    if policy:
        session.add(SourceMonitoringPolicy(
            workspace_id=workspace.id, user_id=user.id, source="gmail", mode="ALL",
        ))
    state = GmailWatchState(
        integration_connection_id=connection.id,
        status=status,
        history_id=history_id,
        updated_at=updated_at or datetime.now(timezone.utc) - timedelta(days=1),
    )
    session.add(state)
    await session.flush()
    return connection, state


def _setup(monkeypatch):
    monkeypatch.setattr(gmail_watch_jobs, "async_session_factory", worker.async_session_factory)
    monkeypatch.setattr(settings, "NANGO_ENV_KEY", "test-key")
    monkeypatch.setattr(settings, "NANGO_BASE_URL", "https://nango.example")
    client_args = []
    monkeypatch.setattr(nango_client, "NangoClient", lambda **kwargs: client_args.append(kwargs) or object())
    return client_args


@pytest.mark.asyncio
async def test_reconcile_isolates_account_failure_and_rotates_successful_watch(worker_db, monkeypatch):
    failed, failed_state = await _watch(worker_db)
    succeeded, success_state = await _watch(worker_db)
    failed_id, success_id = failed.id, succeeded.id
    original_success_update = success_state.updated_at
    await worker_db.commit()
    client_args = _setup(monkeypatch)
    seen = []

    class Provider:
        async def process_history(self, session, connection):
            seen.append(connection.id)
            if connection.id == failed_id:
                raise RuntimeError("private Gmail content")
            return 0  # Empty history still advances updated_at for fairness.

    monkeypatch.setattr(gmail_watch_jobs, "GmailSourceProvider", lambda client: Provider())
    result = await gmail_watch_jobs.reconcile_gmail_histories_job({})

    assert result == {"scanned": 2, "processed": 1, "failed": 1, "skipped": 0}
    assert set(seen) == {failed_id, success_id}
    assert client_args == [{"api_key": "test-key", "base_url": "https://nango.example"}]
    refreshed_success = (await worker_db.exec(select(GmailWatchState).where(
        GmailWatchState.integration_connection_id == success_id,
    ))).one()
    assert refreshed_success.updated_at > original_success_update
    refreshed_failed = (await worker_db.exec(select(GmailWatchState).where(
        GmailWatchState.integration_connection_id == failed_id,
    ))).one()
    assert refreshed_failed.history_id == "cursor"


@pytest.mark.asyncio
async def test_no_policy_and_needs_resync_are_never_processed(worker_db, monkeypatch):
    no_policy, _ = await _watch(worker_db, policy=False)
    resync, _ = await _watch(worker_db, status="needs_resync", history_id="keep-this-cursor")
    resync_id = resync.id
    await worker_db.commit()
    client_args = _setup(monkeypatch)
    monkeypatch.setattr(gmail_watch_jobs, "GmailSourceProvider", lambda *_: pytest.fail("unexpected provider"))

    result = await gmail_watch_jobs.reconcile_gmail_histories_job({})
    assert result == {"scanned": 0, "processed": 0, "failed": 0, "skipped": 0}
    assert client_args == []
    stored = (await worker_db.exec(select(GmailWatchState).where(
        GmailWatchState.integration_connection_id == resync_id,
    ))).one()
    assert stored.status == "needs_resync"
    assert stored.history_id == "keep-this-cursor"
    assert no_policy.id != resync_id


@pytest.mark.asyncio
async def test_reconcile_missing_config_fails_closed_before_client(worker_db, monkeypatch):
    await _watch(worker_db)
    monkeypatch.setattr(gmail_watch_jobs, "async_session_factory", worker.async_session_factory)
    monkeypatch.setattr(settings, "NANGO_ENV_KEY", "")
    monkeypatch.setattr(nango_client, "NangoClient", lambda **_: pytest.fail("unexpected client"))
    monkeypatch.setattr(gmail_watch_jobs, "GmailSourceProvider", lambda *_: pytest.fail("unexpected provider"))

    result = await gmail_watch_jobs.reconcile_gmail_histories_job({})
    assert result == {"scanned": 0, "processed": 0, "failed": 0, "skipped": 0}


@pytest.mark.asyncio
async def test_reconcile_preserves_resync_transition_and_cursor(worker_db, monkeypatch):
    connection, state = await _watch(worker_db, history_id="durable-cursor")
    connection_id = connection.id
    await worker_db.commit()
    _setup(monkeypatch)
    calls = []

    class Provider:
        async def process_history(self, session, connection):
            calls.append(connection.id)
            watch = (await session.exec(select(GmailWatchState).where(
                GmailWatchState.integration_connection_id == connection.id,
            ))).one()
            watch.status = "needs_resync"  # B1's 404 transition; no rebaseline.
            return 0

    monkeypatch.setattr(gmail_watch_jobs, "GmailSourceProvider", lambda _: Provider())
    result = await gmail_watch_jobs.reconcile_gmail_histories_job({})
    assert result["processed"] == 1
    assert calls == [connection_id]
    stored = (await worker_db.exec(select(GmailWatchState).where(
        GmailWatchState.integration_connection_id == connection_id,
    ))).one()
    assert stored.status == "needs_resync"
    assert stored.history_id == "durable-cursor"


def test_reconciliation_job_is_registered_with_arq():
    assert gmail_watch_jobs.reconcile_gmail_histories_job in worker.WorkerSettings.functions
    assert any(
        job.coroutine is gmail_watch_jobs.reconcile_gmail_histories_job and job.unique
        for job in worker.WorkerSettings.cron_jobs
    )
