"""Testes dos estados de configuração e dos limites do fluxo OAuth MCP."""

import uuid
from urllib.parse import parse_qs, urlsplit

import pytest
from cryptography.fernet import Fernet
from sqlmodel import select

from app.core.config import settings
from app.models.mcp_oauth import MCPOAuthFlow, UserMCPOAuthCredential
from app.models.mcp_server import UserMCPServer
from app.services import mcp_oauth


def _configure_local_oauth(monkeypatch, *, google: bool = False) -> None:
    monkeypatch.setattr(settings, "MCP_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "MCP_PUBLIC_BASE_URL", "https://api.anotai.test")
    monkeypatch.setattr(settings, "MCP_WEB_APP_URL", "https://anotai.test")
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "client-id" if google else "")
    monkeypatch.setattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", "client-secret" if google else "")


def test_catalog_explains_missing_platform_setup(monkeypatch):
    monkeypatch.setattr(settings, "MCP_TOKEN_ENCRYPTION_KEY", "")
    providers = {item["id"]: item for item in mcp_oauth.catalog()}
    assert not providers["notion"]["is_ready"]
    assert "proteção de credenciais" in providers["notion"]["setup_hint"]
    assert not providers["google_calendar"]["is_ready"]
    assert "Google Cloud" in providers["google_calendar"]["setup_hint"]


def test_catalog_enables_oauth_providers_when_platform_is_configured(monkeypatch):
    _configure_local_oauth(monkeypatch)
    providers = {item["id"]: item for item in mcp_oauth.catalog()}
    assert providers["notion"]["is_ready"]
    assert providers["evernote"]["is_ready"]
    assert not providers["google_gmail"]["is_ready"]

    _configure_local_oauth(monkeypatch, google=True)
    providers = {item["id"]: item for item in mcp_oauth.catalog()}
    assert providers["google_gmail"]["is_ready"]
    assert providers["google_calendar"]["is_ready"]
    assert providers["google_sheets"]["is_ready"]


async def test_oauth_start_requires_encryption_before_provider_access(monkeypatch):
    monkeypatch.setattr(settings, "MCP_TOKEN_ENCRYPTION_KEY", "")
    with pytest.raises(mcp_oauth.MCPOAuthError, match="proteção das credenciais"):
        await mcp_oauth.start_oauth(None, uuid.uuid4(), "notion")


def test_oauth_discovery_rejects_unrelated_endpoint():
    with pytest.raises(mcp_oauth.MCPOAuthError, match="não reconhecido"):
        mcp_oauth._validate_endpoint("https://attacker.example/oauth", ["mcp.notion.com"])


async def test_provider_catalog_and_callback_routes(client, auth_headers):
    headers, _user_id = auth_headers
    response = await client.get("/api/v1/mcp/providers", headers=headers)
    assert response.status_code == 200
    assert {item["id"] for item in response.json()} == {
        "google_gmail", "google_calendar", "google_sheets", "notion", "evernote",
    }

    callback = await client.get("/api/v1/mcp/oauth/callback", follow_redirects=False)
    assert callback.status_code == 303
    assert callback.headers["location"].endswith("/mcp?oauth=error")


async def test_notion_oauth_flow_encrypts_tokens_and_creates_connection(
    db_session, auth_headers, monkeypatch,
):
    from app.services.mcp_oauth import finish_oauth, get_access_token, start_oauth

    _headers, user_id = auth_headers
    user_id = uuid.UUID(user_id)
    _configure_local_oauth(monkeypatch)
    metadata = {
        "authorization_endpoint": "https://mcp.notion.com/oauth/authorize",
        "token_endpoint": "https://mcp.notion.com/oauth/token",
        "registration_endpoint": "https://mcp.notion.com/oauth/register",
        "issuer": "https://mcp.notion.com",
    }

    async def discover(_provider):
        return "https://mcp.notion.com", metadata, "https://mcp.notion.com", ["default"]

    monkeypatch.setattr(mcp_oauth, "_discover_oauth", discover)

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, url, **kwargs):
            if url == metadata["registration_endpoint"]:
                return FakeResponse({"client_id": "notion-test-client"})
            if url == metadata["token_endpoint"]:
                return FakeResponse({
                    "access_token": "test-access-token",
                    "refresh_token": "test-refresh-token",
                    "expires_in": 3600,
                    "token_type": "Bearer",
                })
            raise AssertionError("URL OAuth inesperada")

    monkeypatch.setattr(mcp_oauth.httpx, "AsyncClient", FakeAsyncClient)
    started = await start_oauth(db_session, user_id, "notion")
    query = parse_qs(urlsplit(started["authorization_url"]).query)
    state = query["state"][0]
    assert query["code_challenge_method"] == ["S256"]

    redirect = await finish_oauth(db_session, "test-auth-code", state, None)
    assert redirect.endswith("/mcp?oauth=success&provider=notion")

    flow = (await db_session.exec(select(MCPOAuthFlow))).one()
    assert flow.state_hash != state
    server = (await db_session.exec(select(UserMCPServer).where(
        UserMCPServer.user_id == user_id,
        UserMCPServer.provider_id == "notion",
    ))).one()
    credential = (await db_session.exec(select(UserMCPOAuthCredential).where(
        UserMCPOAuthCredential.server_id == server.id,
    ))).one()
    assert "test-access-token" not in credential.token_ciphertext
    assert "test-refresh-token" not in credential.token_ciphertext
    assert await get_access_token(db_session, server, user_id) == "test-access-token"
