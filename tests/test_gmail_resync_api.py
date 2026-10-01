"""Explicit, opt-in Gmail history-gap rebaseline API tests."""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlmodel import select

from app.api.v1 import integrations
from app.core.config import settings
from app.models.gmail_watch_state import GmailWatchState
from app.models.integration_connection import IntegrationConnection
from app.models.source_policy import SourceMonitoringPolicy
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMembership
from app.services.gmail_ingress import GmailSourceProvider
from app.services.nango_client import NangoClientError


async def _gmail_binding(session, user, *, status="needs_resync", policy=True):
    row = IntegrationConnection(
        workspace_id=user.workspace_id, user_id=user.id, provider="google_gmail",
        provider_config_key="gmail-config", nango_connection_id=f"gmail-nango-{user.id}",
        account_email="owner@gmail.com", status="active",
    )
    session.add(row)
    await session.flush()
    watch = GmailWatchState(
        integration_connection_id=row.id, status=status, history_id="123456",
        expiration_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        last_renewed_at=datetime(2029, 1, 1, tzinfo=timezone.utc),
        page_token="old-page-token", last_error_kind="history_record_too_large",
    )
    session.add(watch)
    if policy:
        session.add(SourceMonitoringPolicy(
            workspace_id=user.workspace_id, user_id=user.id, source="gmail", mode="ALL",
            selected_ids=[], gmail_label_ids=["INBOX"], gmail_allowed_senders=[],
        ))
    await session.commit()
    return row, watch


def _configure(monkeypatch):
    monkeypatch.setattr(settings, "NANGO_ENV_KEY", "test-nango-key")
    monkeypatch.setattr(settings, "NANGO_BASE_URL", "https://nango.example.test")
    monkeypatch.setattr(settings, "NANGO_GOOGLE_GMAIL_PROVIDER_CONFIG_KEY", "gmail-config")
    monkeypatch.setattr(settings, "GMAIL_PUBSUB_TOPIC", "projects/test/topics/gmail")


class _FakeNango:
    response = {"historyId": "987654321", "expiration": "1893456000000"}
    error: Exception | None = None
    calls = []

    def __init__(self, *_args, **_kwargs):
        pass

    async def proxy_request(self, method, path, **kwargs):
        type(self).calls.append((method, path, kwargs))
        error = type(self).error
        if error is not None:
            raise error
        return SimpleNamespace(body=type(self).response)


def _patch_nango(monkeypatch):
    _FakeNango.response = {"historyId": "987654321", "expiration": "1893456000000"}
    _FakeNango.error = None
    _FakeNango.calls = []
    monkeypatch.setattr(integrations, "NangoClient", _FakeNango)


@pytest.mark.asyncio
async def test_gap_status_is_visible_to_owner_without_cursor_or_foreign_rows(
    client, auth_headers, db_session, monkeypatch,
):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    assert user is not None
    row, state = await _gmail_binding(db_session, user)
    other = User(name="Other", email=f"{uuid.uuid4()}@example.com", hashed_password="unused")
    db_session.add(other)
    await db_session.flush()
    other.workspace_id = user.workspace_id
    db_session.add(WorkspaceMembership(workspace_id=user.workspace_id, user_id=other.id, role="MEMBER"))
    foreign, _ = await _gmail_binding(db_session, other)
    calendar = IntegrationConnection(
        workspace_id=user.workspace_id, user_id=user.id, provider="google_calendar",
        provider_config_key="calendar-config", nango_connection_id="calendar-1", status="active",
    )
    db_session.add(calendar)
    await db_session.commit()

    response = await client.get("/api/v1/integrations/connections", headers=headers)
    assert response.status_code == 200
    by_id = {item["id"]: item for item in response.json()}
    assert str(foreign.id) not in by_id
    assert by_id[str(row.id)]["gmail_watch_status"] == "needs_resync"
    assert by_id[str(row.id)]["expiration_at"] == "2030-01-01T00:00:00Z"
    assert "history_id" not in response.text and "123456" not in response.text
    assert by_id[str(calendar.id)]["gmail_watch_status"] is None
    assert by_id[str(calendar.id)]["expiration_at"] is None
    await db_session.refresh(state)


@pytest.mark.asyncio
async def test_rebaseline_requires_true_consent_and_active_policy(
    client, auth_headers, db_session, monkeypatch,
):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    row, state = await _gmail_binding(db_session, user, policy=False)
    _configure(monkeypatch)
    _patch_nango(monkeypatch)

    false_consent = await client.post(
        f"/api/v1/integrations/connections/{row.id}/resume-gmail",
        headers=headers, json={"accept_gap": False},
    )
    malformed = await client.post(
        f"/api/v1/integrations/connections/{row.id}/resume-gmail",
        headers=headers, json={"accept_gap": True, "historyId": "attacker-cursor"},
    )
    assert false_consent.status_code == 422
    assert malformed.status_code == 422

    denied = await client.post(
        f"/api/v1/integrations/connections/{row.id}/resume-gmail",
        headers=headers, json={"accept_gap": True},
    )
    assert denied.status_code == 403
    foreign_user = User(
        name="Foreign owner", email=f"{uuid.uuid4()}@example.com", hashed_password="unused",
        workspace_id=user.workspace_id,
    )
    db_session.add(foreign_user)
    await db_session.flush()
    db_session.add(WorkspaceMembership(
        workspace_id=user.workspace_id, user_id=foreign_user.id, role="MEMBER",
    ))
    foreign_row, _ = await _gmail_binding(db_session, foreign_user)
    foreign = await client.post(
        f"/api/v1/integrations/connections/{foreign_row.id}/resume-gmail",
        headers=headers, json={"accept_gap": True},
    )
    assert foreign.status_code == 404
    assert _FakeNango.calls == []
    await db_session.refresh(state)
    assert state.status == "needs_resync" and state.history_id == "123456"


@pytest.mark.asyncio
async def test_rebaseline_calls_only_watch_and_commits_new_baseline_after_consent(
    client, auth_headers, db_session, monkeypatch,
):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    row, state = await _gmail_binding(db_session, user)
    row_id = row.id
    _configure(monkeypatch)
    _patch_nango(monkeypatch)

    response = await client.post(
        f"/api/v1/integrations/connections/{row_id}/resume-gmail",
        headers=headers, json={"accept_gap": True},
    )
    assert response.status_code == 200, response.text
    assert response.json()["gmail_watch_status"] == "active"
    assert len(_FakeNango.calls) == 1
    method, path, kwargs = _FakeNango.calls[0]
    assert method == "POST" and path == "gmail/v1/users/me/watch"
    assert kwargs["json_body"] == {
        "topicName": "projects/test/topics/gmail", "labelIds": ["INBOX"],
        "labelFilterBehavior": "include",
    }
    assert kwargs["connection_id"] == row.nango_connection_id
    assert kwargs["provider_config_key"] == row.provider_config_key
    assert not any("messages" in call[1] or "history" in call[1] for call in _FakeNango.calls)
    await db_session.refresh(state)
    assert state.status == "active" and state.history_id == "987654321"
    assert state.expiration_at == datetime.fromtimestamp(1893456000, timezone.utc).replace(tzinfo=None)
    assert state.last_error_kind == "history_gap_acknowledged"
    assert state.page_token is None

    duplicate = await client.post(
        f"/api/v1/integrations/connections/{row_id}/resume-gmail",
        headers=headers, json={"accept_gap": True},
    )
    assert duplicate.status_code == 409
    assert len(_FakeNango.calls) == 1


@pytest.mark.asyncio
async def test_rebaseline_nango_failure_rolls_back_resync_cursor_and_config_failure_is_503(
    client, auth_headers, db_session, monkeypatch,
):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    row, state = await _gmail_binding(db_session, user)
    row_id = row.id
    _configure(monkeypatch)
    _patch_nango(monkeypatch)
    _FakeNango.error = NangoClientError(502, "http_error")

    failed = await client.post(
        f"/api/v1/integrations/connections/{row_id}/resume-gmail",
        headers=headers, json={"accept_gap": True},
    )
    assert failed.status_code == 502
    await db_session.refresh(state)
    assert state.status == "needs_resync" and state.history_id == "123456"
    assert state.expiration_at == datetime(2030, 1, 1)
    assert state.last_error_kind == "history_record_too_large"

    _FakeNango.error = None
    monkeypatch.setattr(settings, "GMAIL_PUBSUB_TOPIC", " ")
    unavailable = await client.post(
        f"/api/v1/integrations/connections/{row_id}/resume-gmail",
        headers=headers, json={"accept_gap": True},
    )
    assert unavailable.status_code == 503
    assert len(_FakeNango.calls) == 1
    await db_session.refresh(state)
    assert state.status == "needs_resync" and state.history_id == "123456"


@pytest.mark.asyncio
async def test_history_404_marks_gap_visible_and_no_content_fetch_runs(
    client, auth_headers, db_session, monkeypatch,
):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    row, state = await _gmail_binding(db_session, user, status="active")
    class History404:
        calls = []

        async def proxy_request(self, method, path, **kwargs):
            self.calls.append((method, path))
            raise NangoClientError(404, "http_error")

    nango = History404()
    assert await GmailSourceProvider(nango).process_history(db_session, row) == 0
    listing = await client.get("/api/v1/integrations/connections", headers=headers)
    assert listing.status_code == 200
    assert next(item for item in listing.json() if item["id"] == str(row.id))["gmail_watch_status"] == "needs_resync"
    assert nango.calls == [("GET", "gmail/v1/users/me/history")]
