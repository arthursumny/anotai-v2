"""Security and binding tests for the Nango integration API."""

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import httpx
from sqlmodel import select

from app.api.v1 import integrations
from app.core.config import settings
from app.models.gmail_watch_state import GmailWatchState
from app.models.integration_connection import IntegrationConnection
from app.models.source_policy import SourceMonitoringPolicy
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMembership
from app.services.nango_client import ConnectionMetadata, ConnectSession
from app.services.nango_client import NangoClient


def _configure_nango(monkeypatch):
    monkeypatch.setattr(settings, "NANGO_ENV_KEY", "test-api-key")
    monkeypatch.setattr(settings, "NANGO_SIGNING_KEY", "webhook-test-secret")
    monkeypatch.setattr(settings, "NANGO_BASE_URL", "https://nango.example.test")
    monkeypatch.setattr(settings, "NANGO_ENVIRONMENT", "test-environment")
    monkeypatch.setattr(settings, "NANGO_GOOGLE_GMAIL_PROVIDER_CONFIG_KEY", "gmail-config")
    monkeypatch.setattr(settings, "NANGO_GOOGLE_CALENDAR_PROVIDER_CONFIG_KEY", "calendar-config")


def _signature(body: bytes, key: str | None = None) -> str:
    secret = key if key is not None else settings.NANGO_SIGNING_KEY
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _webhook_payload(user, workspace, *, operation="creation", success=True, **overrides):
    payload = {
        "type": "auth",
        "operation": operation,
        "success": success,
        "environment": settings.NANGO_ENVIRONMENT,
        "connectionId": "nango-connection-1",
        "providerConfigKey": "gmail-config",
        "tags": {"end_user_id": str(user.id), "organization_id": str(workspace.id)},
    }
    payload.update(overrides)
    return payload


class _FakeNango:
    metadata: ConnectionMetadata | None = None
    profile_body = {"emailAddress": "Alice.Example@gmail.com"}
    profile_error = False
    connect_session = ConnectSession(
        token="connect-token-secret",
        connect_link="https://connect.example.test/link",
        expires_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    deleted: list[str] = []
    created_args = None

    def __init__(self, *_args, **_kwargs):
        pass

    async def create_connect_session(self, **kwargs):
        type(self).created_args = kwargs
        return type(self).connect_session

    async def get_connection_metadata(self, connection_id, provider_config_key):
        assert self.metadata is not None
        assert connection_id == self.metadata.connection_id
        assert provider_config_key == self.metadata.provider_config_key
        return self.metadata

    async def proxy_request(self, method, provider_config_key, connection_id, endpoint, params=None, json=None):
        assert method == "GET" and endpoint == "gmail/v1/users/me/profile"
        if type(self).profile_error:
            raise integrations.NangoClientError(502, "http_error")
        return SimpleNamespace(body=type(self).profile_body)

    async def delete_connection(self, connection_id):
        type(self).deleted.append(connection_id)


def _patch_nango(monkeypatch):
    _FakeNango.metadata = None
    _FakeNango.profile_body = {"emailAddress": "Alice.Example@gmail.com"}
    _FakeNango.profile_error = False
    _FakeNango.deleted = []
    _FakeNango.created_args = None
    monkeypatch.setattr(integrations, "NangoClient", _FakeNango)


@pytest.mark.asyncio
async def test_auth_and_missing_configuration_fail_closed(client, auth_headers, monkeypatch):
    headers, _ = auth_headers
    assert (await client.post("/api/v1/integrations/sessions", json={"provider": "google_gmail"})).status_code == 401
    assert (await client.get("/api/v1/integrations/connections")).status_code == 401
    _configure_nango(monkeypatch)
    monkeypatch.setattr(settings, "NANGO_SIGNING_KEY", "")
    response = await client.post("/api/v1/integrations/sessions", headers=headers, json={"provider": "google_gmail"})
    assert response.status_code == 503
    webhook = await client.post("/api/v1/integrations/webhooks/nango", content=b"not-json")
    assert webhook.status_code == 503


@pytest.mark.asyncio
async def test_connect_session_uses_server_owned_tags_and_only_returns_token(client, auth_headers, monkeypatch):
    headers, user_id = auth_headers
    _configure_nango(monkeypatch)
    _patch_nango(monkeypatch)
    response = await client.post("/api/v1/integrations/sessions", headers=headers, json={"provider": "google_gmail"})
    assert response.status_code == 200, response.text
    assert response.json() == {
        "session_token": "connect-token-secret", "expires_at": "2030-01-01T00:00:00Z",
    }
    body = response.text.lower()
    assert "connect_link" not in body and "connection_id" not in body
    assert _FakeNango.created_args is not None
    assert _FakeNango.created_args["end_user_id"] == user_id
    assert "organization_id" in _FakeNango.created_args
    assert _FakeNango.created_args["provider_config_key"] == "gmail-config"


@pytest.mark.asyncio
async def test_authenticated_connection_listing_is_masked_and_payload_minimal(client, auth_headers, db_session):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    row = IntegrationConnection(
        workspace_id=user.workspace_id, user_id=user.id, provider="google_gmail",
        provider_config_key="gmail-config", nango_connection_id="connection-secret-id",
        account_email="alice@example.com", display_name="Alice", status="active",
        metadata_json={"opaque": "value"},
    )
    db_session.add(row)
    await db_session.commit()
    response = await client.get("/api/v1/integrations/connections", headers=headers)
    assert response.status_code == 200
    assert response.json() == [{
        "id": str(row.id), "provider": "google_gmail", "account_email": "a***@example.com",
        "display_name": "Alice", "status": "active", "monitor_enabled": False,
    }]
    assert "gmail-config" not in response.text and "connection-secret-id" not in response.text
    assert "opaque" not in response.text and "token" not in response.text.lower()


@pytest.mark.asyncio
async def test_webhook_hmac_raw_unicode_binding_idempotency_and_paused_watch(client, auth_headers, db_session, monkeypatch):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    _configure_nango(monkeypatch)
    _patch_nango(monkeypatch)
    _FakeNango.metadata = ConnectionMetadata(
        connection_id="nango-connection-1", provider_config_key="gmail-config",
        tags={"end_user_id": str(user.id), "organization_id": str(user.workspace_id)},
    )
    workspace = await db_session.get(Workspace, user.workspace_id)
    payload = _webhook_payload(user, workspace)
    payload["extra_unicode"] = "Olá, mundo 🌎"
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    signed_headers = {"X-Nango-Hmac-Sha256": _signature(raw), "Content-Type": "application/json"}

    created = await client.post("/api/v1/integrations/webhooks/nango", content=raw, headers=signed_headers)
    assert created.status_code == 204
    saved_watch = (await db_session.exec(select(GmailWatchState))).one()
    saved_watch.status = "active"
    saved_watch.history_id = "987654"
    saved_watch.expiration_at = datetime(2030, 1, 1, tzinfo=timezone.utc)
    policy = SourceMonitoringPolicy(
        workspace_id=user.workspace_id, user_id=user.id, source="gmail", mode="ALL",
        selected_ids=[], gmail_label_ids=["INBOX"], gmail_allowed_senders=[],
    )
    db_session.add(policy)
    await db_session.commit()
    repeated = await client.post("/api/v1/integrations/webhooks/nango", content=raw, headers=signed_headers)
    assert repeated.status_code == 204
    rows = (await db_session.exec(select(IntegrationConnection))).all()
    watches = (await db_session.exec(select(GmailWatchState))).all()
    assert len(rows) == len(watches) == 1
    assert rows[0].user_id == user.id and rows[0].workspace_id == user.workspace_id
    assert rows[0].status == "active" and rows[0].metadata_json == {}
    assert rows[0].account_email == "alice.example@gmail.com"
    assert watches[0].status == "active" and watches[0].history_id == "987654"
    assert (await db_session.exec(select(SourceMonitoringPolicy))).one().id == policy.id
    assert "access_token" not in str(rows[0].model_dump()).lower()


@pytest.mark.asyncio
async def test_delete_then_new_nango_creation_rebinds_same_local_row_without_restoring_opt_in(
    client, auth_headers, db_session, monkeypatch,
):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    workspace = await db_session.get(Workspace, user.workspace_id)
    _configure_nango(monkeypatch)
    _patch_nango(monkeypatch)
    row = IntegrationConnection(
        workspace_id=workspace.id, user_id=user.id, provider="google_gmail",
        provider_config_key="gmail-config", nango_connection_id="old-nango-id",
        account_email="alice.example@gmail.com", status="active",
    )
    db_session.add(row)
    await db_session.flush()
    watch = GmailWatchState(
        integration_connection_id=row.id, status="active", history_id="987654",
        expiration_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        last_renewed_at=datetime(2029, 1, 1, tzinfo=timezone.utc), page_token="old-page",
    )
    policy = SourceMonitoringPolicy(
        workspace_id=workspace.id, user_id=user.id, source="gmail", mode="ALL",
        selected_ids=[], gmail_label_ids=["INBOX"], gmail_allowed_senders=[],
    )
    db_session.add(watch)
    db_session.add(policy)
    await db_session.commit()

    deleted = await client.delete(f"/api/v1/integrations/connections/{row.id}", headers=headers)
    assert deleted.status_code == 204
    await db_session.refresh(row)
    await db_session.refresh(watch)
    assert row.status == "disconnected" and watch.status == "paused"
    assert (await db_session.exec(select(SourceMonitoringPolicy))).all() == []

    new_id = "replacement-nango-id"
    _FakeNango.metadata = ConnectionMetadata(
        connection_id=new_id, provider_config_key="gmail-config",
        tags={"end_user_id": str(user.id), "organization_id": str(workspace.id)},
    )
    event = _webhook_payload(user, workspace, connectionId=new_id)
    body = json.dumps(event).encode()
    resumed = await client.post(
        "/api/v1/integrations/webhooks/nango", content=body,
        headers={"X-Nango-Hmac-Sha256": _signature(body)},
    )
    assert resumed.status_code == 204
    await db_session.refresh(row)
    await db_session.refresh(watch)
    assert row.id == watch.integration_connection_id
    assert row.nango_connection_id == new_id and row.status == "active"
    assert row.account_email == "alice.example@gmail.com"
    assert watch.status == "paused"
    assert watch.history_id is None and watch.expiration_at is None
    assert watch.last_renewed_at is None and watch.page_token is None
    assert (await db_session.exec(select(SourceMonitoringPolicy))).all() == []
    assert new_id not in resumed.text and "access_token" not in resumed.text


@pytest.mark.asyncio
async def test_new_binding_with_changed_mailbox_resets_watch_and_removes_policy(
    client, auth_headers, db_session, monkeypatch,
):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    workspace = await db_session.get(Workspace, user.workspace_id)
    _configure_nango(monkeypatch)
    _patch_nango(monkeypatch)
    row = IntegrationConnection(
        workspace_id=workspace.id, user_id=user.id, provider="google_gmail",
        provider_config_key="gmail-config", nango_connection_id="old-mailbox-connection",
        account_email="old@example.com", status="active",
    )
    db_session.add(row)
    await db_session.flush()
    watch = GmailWatchState(
        integration_connection_id=row.id, status="active", history_id="555",
        expiration_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
    )
    policy = SourceMonitoringPolicy(
        workspace_id=workspace.id, user_id=user.id, source="gmail", mode="ALL",
        selected_ids=[], gmail_label_ids=["INBOX"], gmail_allowed_senders=[],
    )
    db_session.add(watch)
    db_session.add(policy)
    await db_session.commit()
    _FakeNango.metadata = ConnectionMetadata(
        connection_id="new-mailbox-connection", provider_config_key="gmail-config",
        tags={"end_user_id": str(user.id), "organization_id": str(workspace.id)},
    )
    _FakeNango.profile_body = {"emailAddress": "different@example.com"}
    event = _webhook_payload(user, workspace, operation="override", connectionId="new-mailbox-connection")
    body = json.dumps(event).encode()
    response = await client.post(
        "/api/v1/integrations/webhooks/nango", content=body,
        headers={"X-Nango-Hmac-Sha256": _signature(body)},
    )
    assert response.status_code == 204
    await db_session.refresh(row)
    await db_session.refresh(watch)
    assert row.nango_connection_id == "new-mailbox-connection"
    assert row.account_email == "different@example.com"
    assert watch.status == "paused" and watch.history_id is None and watch.expiration_at is None
    assert (await db_session.exec(select(SourceMonitoringPolicy))).all() == []


@pytest.mark.asyncio
async def test_active_same_mailbox_rebind_preserves_watch_cursor_expiration_and_policy(
    client, auth_headers, db_session, monkeypatch,
):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    workspace = await db_session.get(Workspace, user.workspace_id)
    _configure_nango(monkeypatch)
    _patch_nango(monkeypatch)
    row = IntegrationConnection(
        workspace_id=workspace.id, user_id=user.id, provider="google_gmail",
        provider_config_key="gmail-config", nango_connection_id="active-old-id",
        account_email="same.mailbox@gmail.com", status="active",
    )
    db_session.add(row)
    await db_session.flush()
    expiration = datetime(2030, 1, 1, tzinfo=timezone.utc)
    watch = GmailWatchState(
        integration_connection_id=row.id, status="active", history_id="778899",
        expiration_at=expiration, last_renewed_at=datetime(2029, 1, 1, tzinfo=timezone.utc),
        page_token="cursor-page",
    )
    policy = SourceMonitoringPolicy(
        workspace_id=workspace.id, user_id=user.id, source="gmail", mode="ALL",
        selected_ids=[], gmail_label_ids=["INBOX"], gmail_allowed_senders=[],
    )
    db_session.add(watch)
    db_session.add(policy)
    await db_session.commit()
    _FakeNango.metadata = ConnectionMetadata(
        connection_id="active-new-id", provider_config_key="gmail-config",
        tags={"end_user_id": str(user.id), "organization_id": str(workspace.id)},
    )
    _FakeNango.profile_body = {"emailAddress": "Same.Mailbox@gmail.com"}
    event = _webhook_payload(user, workspace, operation="override", connectionId="active-new-id")
    body = json.dumps(event).encode()
    response = await client.post(
        "/api/v1/integrations/webhooks/nango", content=body,
        headers={"X-Nango-Hmac-Sha256": _signature(body)},
    )
    assert response.status_code == 204
    await db_session.refresh(row)
    await db_session.refresh(watch)
    assert row.id == watch.integration_connection_id
    assert row.status == "active" and row.nango_connection_id == "active-new-id"
    assert row.account_email == "same.mailbox@gmail.com"
    assert watch.status == "active" and watch.history_id == "778899"
    assert watch.expiration_at == datetime(2030, 1, 1)
    assert watch.page_token == "cursor-page"
    assert (await db_session.exec(select(SourceMonitoringPolicy))).one().id == policy.id


@pytest.mark.asyncio
async def test_signed_cross_tenant_rebind_attempt_cannot_change_existing_binding(
    client, auth_headers, db_session, monkeypatch,
):
    _headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    workspace = await db_session.get(Workspace, user.workspace_id)
    foreign_workspace = Workspace(name="Foreign tenant")
    db_session.add(foreign_workspace)
    await db_session.flush()
    foreign_user = User(
        name="Foreign owner", email=f"{uuid.uuid4()}@example.com", hashed_password="unused",
        workspace_id=foreign_workspace.id,
    )
    db_session.add(foreign_user)
    await db_session.flush()
    db_session.add(WorkspaceMembership(
        workspace_id=foreign_workspace.id, user_id=foreign_user.id, role="OWNER",
    ))
    foreign_row = IntegrationConnection(
        workspace_id=foreign_workspace.id, user_id=foreign_user.id, provider="google_gmail",
        provider_config_key="gmail-config", nango_connection_id="held-by-foreign-tenant",
        account_email="private@example.com", status="active",
    )
    db_session.add(foreign_row)
    await db_session.commit()
    _configure_nango(monkeypatch)
    _patch_nango(monkeypatch)
    _FakeNango.metadata = ConnectionMetadata(
        connection_id="held-by-foreign-tenant", provider_config_key="gmail-config",
        tags={"end_user_id": str(user.id), "organization_id": str(workspace.id)},
    )
    event = _webhook_payload(user, workspace, connectionId="held-by-foreign-tenant")
    body = json.dumps(event).encode()
    response = await client.post(
        "/api/v1/integrations/webhooks/nango", content=body,
        headers={"X-Nango-Hmac-Sha256": _signature(body)},
    )
    assert response.status_code == 204
    await db_session.refresh(foreign_row)
    assert foreign_row.user_id == foreign_user.id
    assert foreign_row.workspace_id == foreign_workspace.id
    assert foreign_row.nango_connection_id == "held-by-foreign-tenant"
    assert foreign_row.account_email == "private@example.com"
    assert (await db_session.exec(select(IntegrationConnection).where(
        IntegrationConnection.user_id == user.id,
    ))).all() == []
    assert _FakeNango.profile_body == {"emailAddress": "Alice.Example@gmail.com"}


@pytest.mark.asyncio
async def test_webhook_hmac_invalid_before_json_and_unknown_provider_ignored(client, auth_headers, monkeypatch):
    _configure_nango(monkeypatch)
    invalid = await client.post(
        "/api/v1/integrations/webhooks/nango", content=b"{malformed",
        headers={"X-Nango-Hmac-Sha256": "0" * 64},
    )
    assert invalid.status_code == 401
    unknown_payload = {
        "type": "auth", "operation": "creation", "success": True,
        "environment": settings.NANGO_ENVIRONMENT, "connectionId": "connection-1",
        "providerConfigKey": "unknown-provider", "tags": {},
    }
    raw = json.dumps(unknown_payload).encode()
    ignored = await client.post(
        "/api/v1/integrations/webhooks/nango", content=raw,
        headers={"X-Nango-Hmac-Sha256": _signature(raw)},
    )
    assert ignored.status_code == 204


@pytest.mark.asyncio
async def test_real_metadata_adapter_discards_upstream_credentials(client, auth_headers, db_session, monkeypatch, caplog):
    _headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    workspace = await db_session.get(Workspace, user.workspace_id)
    _configure_nango(monkeypatch)
    credential_values = {"access_token": "access-secret-marker", "refresh_token": "refresh-secret-marker"}

    async def responder(request):
        if request.url.path == "/connections/nango-connection-1":
            assert request.method == "GET"
            return httpx.Response(200, json={
                "connection_id": "nango-connection-1",
                "provider_config_key": "gmail-config",
                "tags": {"end_user_id": str(user.id), "organization_id": str(workspace.id)},
                "credentials": credential_values,
                "access_token": credential_values["access_token"],
            })
        assert request.method == "GET" and request.url.path == "/proxy/gmail/v1/users/me/profile"
        return httpx.Response(200, json={"emailAddress": "Alice.Example@gmail.com"})

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(responder))
    adapter = NangoClient("test-api-key", base_url="https://nango.example.test", client=upstream)
    monkeypatch.setattr(integrations, "NangoClient", lambda *_args, **_kwargs: adapter)
    payload = _webhook_payload(user, workspace)
    body = json.dumps(payload).encode()
    result = await client.post(
        "/api/v1/integrations/webhooks/nango", content=body,
        headers={"X-Nango-Hmac-Sha256": _signature(body)},
    )
    assert result.status_code == 204
    row = (await db_session.exec(select(IntegrationConnection))).one()
    listing = await client.get("/api/v1/integrations/connections", headers=_headers)
    assert listing.status_code == 200
    persisted = json.dumps(row.model_dump(), default=str)
    for secret in credential_values.values():
        assert secret not in persisted and secret not in listing.text and secret not in caplog.text
    assert row.metadata_json == {}
    assert row.account_email == "alice.example@gmail.com"
    await upstream.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_body,profile_error", [
    (None, False), ({"emailAddress": "not-an-email"}, False), ({"emailAddress": "a" * 321}, False),
    ({"emailAddress": "alice@example.com"}, True),
])
async def test_gmail_profile_failure_never_binds_or_leaks_profile(
    client, auth_headers, db_session, monkeypatch, profile_body, profile_error,
):
    _headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    workspace = await db_session.get(Workspace, user.workspace_id)
    _configure_nango(monkeypatch)
    _patch_nango(monkeypatch)
    _FakeNango.metadata = ConnectionMetadata(
        connection_id="nango-connection-1", provider_config_key="gmail-config",
        tags={"end_user_id": str(user.id), "organization_id": str(workspace.id)},
    )
    _FakeNango.profile_body = profile_body
    _FakeNango.profile_error = profile_error
    payload = _webhook_payload(user, workspace)
    body = json.dumps(payload).encode()
    response = await client.post(
        "/api/v1/integrations/webhooks/nango", content=body,
        headers={"X-Nango-Hmac-Sha256": _signature(body)},
    )
    assert response.status_code == 502
    assert "not-an-email" not in response.text and "alice@example.com" not in response.text
    assert (await db_session.exec(select(IntegrationConnection))).all() == []
    assert (await db_session.exec(select(GmailWatchState))).all() == []


@pytest.mark.asyncio
async def test_gmail_mailbox_override_resets_watch_and_requires_new_opt_in(
    client, auth_headers, db_session, monkeypatch,
):
    _headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    workspace = await db_session.get(Workspace, user.workspace_id)
    row = IntegrationConnection(
        workspace_id=user.workspace_id, user_id=user.id, provider="google_gmail",
        provider_config_key="gmail-config", nango_connection_id="nango-connection-1",
        account_email="old@gmail.com", status="active",
    )
    db_session.add(row)
    await db_session.flush()
    watch = GmailWatchState(
        integration_connection_id=row.id, status="active", history_id="123456",
        expiration_at=datetime(2030, 1, 1, tzinfo=timezone.utc),
        last_renewed_at=datetime(2029, 1, 1, tzinfo=timezone.utc), page_token="page-token",
    )
    policy = SourceMonitoringPolicy(
        workspace_id=user.workspace_id, user_id=user.id, source="gmail", mode="ALL",
        selected_ids=[], gmail_label_ids=["INBOX"], gmail_allowed_senders=[],
    )
    db_session.add(watch)
    db_session.add(policy)
    await db_session.commit()
    _configure_nango(monkeypatch)
    _patch_nango(monkeypatch)
    _FakeNango.metadata = ConnectionMetadata(
        connection_id="nango-connection-1", provider_config_key="gmail-config",
        tags={"end_user_id": str(user.id), "organization_id": str(workspace.id)},
    )
    _FakeNango.profile_body = {"emailAddress": "New.Mailbox@gmail.com"}
    payload = _webhook_payload(user, workspace, operation="override")
    body = json.dumps(payload).encode()
    response = await client.post(
        "/api/v1/integrations/webhooks/nango", content=body,
        headers={"X-Nango-Hmac-Sha256": _signature(body)},
    )
    assert response.status_code == 204
    await db_session.refresh(row)
    await db_session.refresh(watch)
    assert row.account_email == "new.mailbox@gmail.com"
    assert watch.status == "paused"
    assert watch.history_id is None and watch.expiration_at is None
    assert watch.last_renewed_at is None and watch.page_token is None
    assert (await db_session.exec(select(SourceMonitoringPolicy))).all() == []


@pytest.mark.asyncio
async def test_webhook_body_limit_and_environment_mismatch_are_ignored(client, monkeypatch):
    _configure_nango(monkeypatch)
    oversized = b" " * (64 * 1024 + 1)
    too_large = await client.post(
        "/api/v1/integrations/webhooks/nango", content=oversized,
        headers={"X-Nango-Hmac-Sha256": _signature(oversized)},
    )
    assert too_large.status_code == 413
    payload = {
        "type": "auth", "operation": "creation", "success": True,
        "environment": "wrong-environment", "connectionId": "nango-connection-1",
        "providerConfigKey": "gmail-config", "tags": {},
    }
    body = json.dumps(payload).encode()
    response = await client.post(
        "/api/v1/integrations/webhooks/nango", content=body,
        headers={"X-Nango-Hmac-Sha256": _signature(body)},
    )
    assert response.status_code == 204


@pytest.mark.asyncio
async def test_webhook_forged_metadata_or_missing_membership_never_binds(client, auth_headers, db_session, monkeypatch):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    _configure_nango(monkeypatch)
    _patch_nango(monkeypatch)
    _FakeNango.metadata = ConnectionMetadata(
        connection_id="nango-connection-1", provider_config_key="gmail-config",
        tags={"end_user_id": str(uuid.uuid4()), "organization_id": str(user.workspace_id)},
    )
    workspace = await db_session.get(Workspace, user.workspace_id)
    payload = _webhook_payload(user, workspace)
    raw = json.dumps(payload).encode()
    response = await client.post(
        "/api/v1/integrations/webhooks/nango", content=raw,
        headers={"X-Nango-Hmac-Sha256": _signature(raw)},
    )
    assert response.status_code == 204
    assert (await db_session.exec(select(IntegrationConnection))).all() == []

    membership = (await db_session.exec(select(WorkspaceMembership).where(
        WorkspaceMembership.user_id == user.id,
        WorkspaceMembership.workspace_id == user.workspace_id,
    ))).one()
    await db_session.delete(membership)
    await db_session.commit()
    _FakeNango.metadata = ConnectionMetadata(
        connection_id="nango-connection-1", provider_config_key="gmail-config",
        tags={"end_user_id": str(user.id), "organization_id": str(user.workspace_id)},
    )
    response = await client.post(
        "/api/v1/integrations/webhooks/nango", content=raw,
        headers={"X-Nango-Hmac-Sha256": _signature(raw)},
    )
    assert response.status_code == 204
    assert (await db_session.exec(select(IntegrationConnection))).all() == []


@pytest.mark.asyncio
async def test_refresh_failure_deletion_pause_policy_and_cross_user_delete_404(
    client, auth_headers, db_session, monkeypatch,
):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    row = IntegrationConnection(
        workspace_id=user.workspace_id, user_id=user.id, provider="google_gmail",
        provider_config_key="gmail-config", nango_connection_id="nango-connection-1", status="active",
    )
    db_session.add(row)
    await db_session.flush()
    watch = GmailWatchState(integration_connection_id=row.id, status="active")
    policy = SourceMonitoringPolicy(
        workspace_id=user.workspace_id, user_id=user.id, source="gmail", mode="ALL",
        selected_ids=[], gmail_label_ids=["INBOX"], gmail_allowed_senders=[],
    )
    db_session.add(watch)
    db_session.add(policy)
    await db_session.commit()
    _configure_nango(monkeypatch)

    cross_user = await client.delete(f"/api/v1/integrations/connections/{row.id}", headers={})
    assert cross_user.status_code == 401
    other = User(name="Other", email=f"{uuid.uuid4()}@example.com", hashed_password="unused")
    db_session.add(other)
    await db_session.commit()
    # Authenticated caller can only see/delete its own binding; another user's UUID is hidden.
    await db_session.refresh(row)
    other_row = IntegrationConnection(
        workspace_id=user.workspace_id, user_id=other.id, provider="google_calendar",
        provider_config_key="calendar-config", nango_connection_id="other-connection", status="active",
    )
    db_session.add(other_row)
    await db_session.commit()
    hidden = await client.delete(f"/api/v1/integrations/connections/{other_row.id}", headers=headers)
    assert hidden.status_code == 404

    def signed(event):
        body = json.dumps(event, ensure_ascii=False).encode()
        return body, {"X-Nango-Hmac-Sha256": _signature(body)}

    workspace = await db_session.get(Workspace, user.workspace_id)
    refresh_event = _webhook_payload(user, workspace, operation="refresh", success=False)
    body, signed_headers = signed(refresh_event)
    refreshed = await client.post("/api/v1/integrations/webhooks/nango", content=body, headers=signed_headers)
    assert refreshed.status_code == 204
    await db_session.refresh(row)
    assert row.status == "error"

    _patch_nango(monkeypatch)
    deleted = await client.delete(f"/api/v1/integrations/connections/{row.id}", headers=headers)
    assert deleted.status_code == 204
    assert _FakeNango.deleted == ["nango-connection-1"]
    await db_session.refresh(row)
    await db_session.refresh(watch)
    assert row.status == "disconnected" and watch.status == "paused"
    assert (await db_session.exec(select(SourceMonitoringPolicy).where(
        SourceMonitoringPolicy.user_id == user.id, SourceMonitoringPolicy.source == "gmail",
    ))).first() is None
