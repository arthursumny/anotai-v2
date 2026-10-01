import base64
import json
import uuid

import pytest

from app import main
from app.api.v1 import gmail_push
from app.core.config import settings
from app.models.gmail_watch_state import GmailWatchState
from app.models.integration_connection import IntegrationConnection
from app.models.source_policy import SourceMonitoringPolicy
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMembership


_AUDIENCE = "https://api.example/gmail/push"
_SERVICE_ACCOUNT = "push@example.iam.gserviceaccount.com"


def _verified_token(monkeypatch, *, email=_SERVICE_ACCOUNT, verified=True):
    calls = []

    def verify(token, request, *, audience):
        calls.append((token, audience))
        return {
            "iss": "https://accounts.google.com",
            "aud": audience,
            "email": email,
            "email_verified": verified,
        }

    monkeypatch.setattr(gmail_push.id_token, "verify_oauth2_token", verify)
    monkeypatch.setattr(settings, "GMAIL_PUSH_AUDIENCE", _AUDIENCE)
    monkeypatch.setattr(settings, "GMAIL_PUSH_SA_EMAIL", _SERVICE_ACCOUNT)
    return calls


def _body(email="owner@example.com", history_id="12345"):
    data = base64.b64encode(json.dumps({"emailAddress": email, "historyId": history_id}).encode()).decode()
    return {"message": {"data": data}}


async def _connection(session, email, *, policy=True, watch="active"):
    user = User(name="Push", email=f"{uuid.uuid4()}@example.com", hashed_password="unused")
    workspace = Workspace(name="Push workspace", timezone="UTC")
    session.add(workspace)
    await session.flush()
    user.workspace_id = workspace.id
    session.add(user)
    await session.flush()
    session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="OWNER"))
    connection = IntegrationConnection(
        workspace_id=workspace.id, user_id=user.id, provider="google_gmail", status="active",
        provider_config_key="gmail-test", nango_connection_id=f"nango-{uuid.uuid4().hex}",
        account_email=email,
    )
    session.add(connection)
    await session.flush()
    session.add(GmailWatchState(
        integration_connection_id=connection.id, history_id="1", status=watch,
    ))
    if policy:
        session.add(SourceMonitoringPolicy(
            workspace_id=workspace.id, user_id=user.id, source="gmail", mode="ALL",
        ))
    await session.flush()
    return connection


@pytest.mark.asyncio
async def test_push_is_registered_and_public_without_application_jwt(client, monkeypatch):
    assert "/api/v1/gmail/push" in main.app.openapi()["paths"]
    _verified_token(monkeypatch)
    queued = []

    async def enqueue(connection_id, history_id):
        queued.append((connection_id, history_id))

    monkeypatch.setattr(gmail_push, "enqueue_gmail_history_job", enqueue)
    response = await client.post(
        "/api/v1/gmail/push", json=_body(), headers={"Authorization": "Bearer google-id-token"},
    )
    assert response.status_code == 204
    assert queued == []


@pytest.mark.asyncio
async def test_push_validates_token_before_parsing_or_enqueue(client, monkeypatch):
    calls = _verified_token(monkeypatch, email="attacker@example.com")
    queued = []
    monkeypatch.setattr(gmail_push, "enqueue_gmail_history_job", lambda *args: queued.append(args))
    response = await client.post(
        "/api/v1/gmail/push", content=b"not-json", headers={"Authorization": "Bearer secret-token"},
    )
    assert response.status_code == 401
    assert calls == [("secret-token", _AUDIENCE)]
    assert queued == []
    assert "secret-token" not in response.text


@pytest.mark.asyncio
async def test_push_rejects_unverified_service_account(client, monkeypatch):
    _verified_token(monkeypatch, verified=False)
    response = await client.post(
        "/api/v1/gmail/push", json=_body(), headers={"Authorization": "Bearer token"},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_push_missing_auth_configuration_is_503(client, monkeypatch):
    monkeypatch.setattr(settings, "GMAIL_PUSH_AUDIENCE", "")
    monkeypatch.setattr(settings, "GMAIL_PUSH_SA_EMAIL", _SERVICE_ACCOUNT)
    response = await client.post("/api/v1/gmail/push", json=_body())
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_push_malformed_body_after_valid_auth_is_400(client, monkeypatch):
    _verified_token(monkeypatch)
    response = await client.post(
        "/api/v1/gmail/push", content=b'{"message":{"data":"%%"}}',
        headers={"Authorization": "Bearer token"},
    )
    assert response.status_code == 400


@pytest.mark.asyncio
async def test_push_filters_policy_and_active_watch_before_enqueue(client, db_session, monkeypatch):
    _verified_token(monkeypatch)
    await _connection(db_session, "owner@example.com", policy=False)
    await _connection(db_session, "owner@example.com", watch="paused")
    queued = []

    async def enqueue(connection_id, history_id):
        queued.append((connection_id, history_id))

    monkeypatch.setattr(gmail_push, "enqueue_gmail_history_job", enqueue)
    response = await client.post(
        "/api/v1/gmail/push", json=_body(), headers={"Authorization": "Bearer token"},
    )
    assert response.status_code == 204
    assert queued == []


@pytest.mark.asyncio
async def test_push_matches_normalized_email_and_enqueues_all_authorized_users(
    client, db_session, monkeypatch
):
    _verified_token(monkeypatch)
    first = await _connection(db_session, "Owner@Example.com")
    second = await _connection(db_session, " owner@example.com ")
    await _connection(db_session, "other@example.com")
    queued = []

    async def enqueue(connection_id, history_id):
        queued.append((connection_id, history_id))

    monkeypatch.setattr(gmail_push, "enqueue_gmail_history_job", enqueue)
    response = await client.post(
        "/api/v1/gmail/push", json=_body("OWNER@example.com", "67890"),
        headers={"Authorization": "Bearer token"},
    )
    assert response.status_code == 204
    assert set(queued) == {(str(first.id), "67890"), (str(second.id), "67890")}


@pytest.mark.asyncio
async def test_push_redis_failure_returns_sanitized_503(client, db_session, monkeypatch):
    _verified_token(monkeypatch)
    await _connection(db_session, "owner@example.com")

    async def fail_enqueue(*args):
        raise RuntimeError("secret email body token")

    monkeypatch.setattr(gmail_push, "enqueue_gmail_history_job", fail_enqueue)
    response = await client.post(
        "/api/v1/gmail/push", json=_body(), headers={"Authorization": "Bearer token"},
    )
    assert response.status_code == 503
    assert "secret" not in response.text


@pytest.mark.asyncio
async def test_duplicate_pushes_enqueue_same_identity_for_arq_deduplication(client, db_session, monkeypatch):
    _verified_token(monkeypatch)
    connection = await _connection(db_session, "owner@example.com")
    queued = []

    async def enqueue(connection_id, history_id):
        queued.append((connection_id, history_id))

    monkeypatch.setattr(gmail_push, "enqueue_gmail_history_job", enqueue)
    headers = {"Authorization": "Bearer token"}
    for _ in range(2):
        response = await client.post("/api/v1/gmail/push", json=_body(history_id="77"), headers=headers)
        assert response.status_code == 204
    assert queued == [(str(connection.id), "77"), (str(connection.id), "77")]
