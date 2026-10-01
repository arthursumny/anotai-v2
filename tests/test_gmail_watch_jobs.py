from datetime import datetime, timedelta, timezone
import uuid

import pytest
from sqlmodel import select

from app.core.config import settings
from app.models.gmail_watch_state import GmailWatchState
from app.models.integration_connection import IntegrationConnection
from app.models.source_policy import SourceMonitoringPolicy
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMembership
from app.services import gmail_watch_jobs
from app import worker


async def _connection(session, *, policy=True, account_email: str | None = "person@example.com", watch=None):
    user = User(name="Watch test", email=f"{uuid.uuid4()}@example.com", hashed_password="unused")
    workspace = Workspace(name="Watch test", timezone="UTC")
    session.add(workspace)
    await session.flush()
    user.workspace_id = workspace.id
    session.add(user)
    await session.flush()
    session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="OWNER"))
    connection = IntegrationConnection(
        workspace_id=workspace.id, user_id=user.id, provider="google_gmail", status="active",
        provider_config_key="gmail-test", nango_connection_id=f"nango-{uuid.uuid4()}",
        account_email=account_email,
    )
    session.add(connection)
    await session.flush()
    if policy:
        session.add(SourceMonitoringPolicy(
            workspace_id=workspace.id, user_id=user.id, source="gmail", mode="ALL",
        ))
    if watch is not None:
        session.add(GmailWatchState(integration_connection_id=connection.id, **watch))
    await session.flush()
    return connection


def _configure(monkeypatch):
    monkeypatch.setattr(gmail_watch_jobs, "async_session_factory", worker.async_session_factory)
    monkeypatch.setattr(settings, "NANGO_ENV_KEY", "test-key")
    monkeypatch.setattr(settings, "NANGO_BASE_URL", "https://nango.example")
    monkeypatch.setattr(settings, "GMAIL_PUBSUB_TOPIC", "projects/test/topics/gmail")


@pytest.mark.asyncio
async def test_gating_prevents_client_creation_without_opt_in_email_or_config(worker_db, monkeypatch):
    without_policy = await _connection(worker_db, policy=False)
    without_email = await _connection(worker_db, account_email=None)
    monkeypatch.setattr(gmail_watch_jobs, "async_session_factory", worker.async_session_factory)
    _configure(monkeypatch)
    monkeypatch.setattr(gmail_watch_jobs, "GmailSourceProvider", lambda *_: pytest.fail("unexpected provider"))

    from app.services import nango_client
    monkeypatch.setattr(nango_client, "NangoClient", lambda **_: pytest.fail("unexpected client"))
    result = await gmail_watch_jobs.renew_gmail_watches_job({})
    assert result["renewed"] == 0
    assert result["scanned"] == 0
    assert without_policy.id != without_email.id

    monkeypatch.setattr(settings, "NANGO_ENV_KEY", "")
    result = await gmail_watch_jobs.renew_gmail_watches_job({})
    assert result == {"scanned": 0, "renewed": 0, "failed": 0, "skipped": 0}


@pytest.mark.asyncio
async def test_paused_watch_starts_and_active_due_watch_renews_without_cursor_reset(worker_db, monkeypatch):
    paused = await _connection(worker_db, watch={"status": "paused", "history_id": None})
    due = await _connection(worker_db, watch={
        "status": "active", "history_id": "cursor-keep",
        "expiration_at": datetime.now(timezone.utc) + timedelta(hours=1),
        "last_renewed_at": datetime.now(timezone.utc) - timedelta(days=2),
    })
    _configure(monkeypatch)
    calls = []

    class Provider:
        async def renew_watch(self, session, connection):
            calls.append(connection.id)
            state = (await session.exec(select(GmailWatchState).where(
                GmailWatchState.integration_connection_id == connection.id,
            ))).first()
            state.status = "active"
            state.history_id = state.history_id or "initial-cursor"

    monkeypatch.setattr(gmail_watch_jobs, "GmailSourceProvider", lambda _: Provider())
    result = await gmail_watch_jobs.renew_gmail_watches_job({})

    assert result["renewed"] == 2
    assert set(calls) == {paused.id, due.id}
    state = (await worker_db.exec(
        select(GmailWatchState).where(
            GmailWatchState.integration_connection_id == due.id,
        )
    )).one()
    assert state.history_id == "cursor-keep"


@pytest.mark.asyncio
async def test_active_not_due_and_needs_resync_are_skipped(worker_db, monkeypatch):
    now = datetime.now(timezone.utc)
    await _connection(worker_db, watch={
        "status": "active", "history_id": "cursor",
        "expiration_at": now + timedelta(days=5), "last_renewed_at": now,
    })
    await _connection(worker_db, watch={"status": "needs_resync", "history_id": None})
    _configure(monkeypatch)
    monkeypatch.setattr(gmail_watch_jobs, "GmailSourceProvider", lambda *_: pytest.fail("unexpected renew"))

    result = await gmail_watch_jobs.renew_gmail_watches_job({})
    assert result["renewed"] == 0
    assert result["scanned"] == 0
    assert result["skipped"] == 0


@pytest.mark.asyncio
async def test_poison_connection_does_not_block_other_renewal(worker_db, monkeypatch):
    bad = await _connection(worker_db, watch={"status": "paused"})
    good = await _connection(worker_db, watch={"status": "paused"})
    bad_id, good_id = bad.id, good.id
    await worker_db.commit()
    _configure(monkeypatch)
    calls = []

    class Provider:
        async def renew_watch(self, session, connection):
            calls.append(connection.id)
            state = (await session.exec(select(GmailWatchState).where(
                GmailWatchState.integration_connection_id == connection.id,
            ))).first()
            if connection.id == bad_id:
                raise RuntimeError("secret should not be logged")
            state.status = "active"
            state.history_id = "new-cursor"

    monkeypatch.setattr(gmail_watch_jobs, "GmailSourceProvider", lambda _: Provider())
    result = await gmail_watch_jobs.renew_gmail_watches_job({})
    assert result["failed"] == 1
    assert result["renewed"] == 1
    assert set(calls) == {bad_id, good_id}
    bad_state = (await worker_db.exec(select(GmailWatchState).where(
        GmailWatchState.integration_connection_id == bad_id,
    ))).one()
    assert bad_state.status == "paused"


@pytest.mark.asyncio
async def test_due_watch_after_first_25_healthy_is_selected_by_sql(worker_db, monkeypatch):
    now = datetime.now(timezone.utc)
    for index in range(26):
        await _connection(worker_db, watch={
            "status": "active", "history_id": str(index + 1),
            "expiration_at": now + timedelta(days=10), "last_renewed_at": now,
        })
    due = await _connection(worker_db, watch={
        "status": "active", "history_id": "due-cursor",
        "expiration_at": now + timedelta(hours=1), "last_renewed_at": now,
    })
    _configure(monkeypatch)
    seen = []

    class Provider:
        async def renew_watch(self, session, connection):
            seen.append(connection.id)

    monkeypatch.setattr(gmail_watch_jobs, "GmailSourceProvider", lambda _: Provider())
    result = await gmail_watch_jobs.renew_gmail_watches_job({})
    assert result["scanned"] == 1
    assert result["renewed"] == 1
    assert seen == [due.id]


def test_watch_renewal_job_is_registered_with_arq():
    assert gmail_watch_jobs.renew_gmail_watches_job in worker.WorkerSettings.functions
    assert any(job.coroutine is gmail_watch_jobs.renew_gmail_watches_job for job in worker.WorkerSettings.cron_jobs)
