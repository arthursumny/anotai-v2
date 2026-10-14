"""HTTP-mocked contract tests for the pure Nango adapter."""

import base64
import json

import httpx
import pytest

from app.services.nango_client import NangoClient, NangoClientError


def _client(handler, **kwargs):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return NangoClient("nango-test-secret", client=http, **kwargs), http


async def test_create_connect_session_uses_documented_request_shape():
    captured = {}

    def handler(request: httpx.Request):
        captured["request"] = request
        return httpx.Response(201, json={
            "data": {
                "token": "short-lived-connect-token",
                "connect_link": "https://connect.nango.dev/session/abc",
                "expires_at": "2026-10-01T12:30:00Z",
            }
        })

    client, http = _client(handler)
    try:
        session = await client.create_connect_session(
            end_user_id="user-123",
            organization_id="workspace-456",
            provider_config_key="google-mail",
        )
    finally:
        await http.aclose()

    request = captured["request"]
    assert request.method == "POST"
    assert str(request.url) == "https://api.nango.dev/connect/sessions"
    assert request.headers["Authorization"] == "Bearer nango-test-secret"
    assert json.loads(request.content) == {
        "tags": {"end_user_id": "user-123", "organization_id": "workspace-456"},
        "allowed_integrations": ["google-mail"],
    }
    assert session.token == "short-lived-connect-token"
    assert "short-lived-connect-token" not in repr(session)
    assert session.connect_link.startswith("https://")


async def test_connection_metadata_redacts_credentials_and_fails_closed_on_unknown_tags():
    response_data = {
        "connection_id": "conn_123",
        "provider_config_key": "google-mail",
        "tags": {
            "end_user_id": "user-123",
            "organization_id": "workspace-456",
            "extra_tag": "extra-tag-value",
        },
        "credentials": {"access_token": "oauth-access-secret", "refresh_token": "oauth-refresh-secret"},
    }

    def handler(request: httpx.Request):
        assert request.method == "GET"
        assert str(request.url) == "https://api.nango.dev/connections/conn_123?provider_config_key=google-mail"
        return httpx.Response(200, json=response_data)

    client, http = _client(handler)
    try:
        metadata = await client.get_connection_metadata("conn_123", "google-mail")
    finally:
        await http.aclose()

    assert metadata.connection_id == "conn_123"
    assert metadata.provider_config_key == "google-mail"
    assert metadata.tags == {"end_user_id": "user-123", "organization_id": "workspace-456"}
    assert "oauth-access-secret" not in repr(metadata)
    assert "oauth-refresh-secret" not in repr(metadata)
    assert "extra-tag-value" not in repr(metadata)

    def malformed_handler(_request):
        return httpx.Response(200, json={
            "connection_id": "conn_123",
            "provider_config_key": "google-mail",
            "tags": {"end_user_id": "user-123", "extra_tag": "don't echo"},
            "credentials": {"access_token": "do-not-leak"},
        })

    malformed, malformed_http = _client(malformed_handler)
    try:
        with pytest.raises(NangoClientError) as error:
            await malformed.get_connection_metadata("conn_123", "google-mail")
    finally:
        await malformed_http.aclose()
    assert "do-not-leak" not in str(error.value)


async def test_proxy_google_paths_attach_only_server_owned_headers():
    captured = {}

    def handler(request: httpx.Request):
        captured["request"] = request
        return httpx.Response(200, json={"messages": [{"id": "message-1"}]})

    client, http = _client(handler)
    try:
        response = await client.proxy_request(
            "GET",
            "gmail/v1/users/me/messages",
            connection_id="conn_123",
            provider_config_key="google-mail",
            params={"maxResults": 10},
        )
    finally:
        await http.aclose()

    request = captured["request"]
    assert str(request.url) == "https://api.nango.dev/proxy/gmail/v1/users/me/messages?maxResults=10"
    assert request.headers["Authorization"] == "Bearer nango-test-secret"
    assert request.headers["Connection-Id"] == "conn_123"
    assert request.headers["Provider-Config-Key"] == "google-mail"
    assert "Base-Url-Override" not in request.headers
    assert response.status_code == 200
    assert response.body == {"messages": [{"id": "message-1"}]}


async def test_gmail_profile_proxy_allows_exact_get_route_only():
    captured = []

    def handler(request: httpx.Request):
        captured.append(request)
        return httpx.Response(200, json={"emailAddress": "person@example.com", "messagesTotal": 12})

    client, http = _client(handler)
    try:
        response = await client.proxy_request(
            "GET",
            "gmail/v1/users/me/profile",
            connection_id="conn_123",
            provider_config_key="google-mail",
        )
        assert response.body["emailAddress"] == "person@example.com"

        for method, bad_path in (
            ("POST", "gmail/v1/users/me/profile"),
            ("GET", "gmail/v1/users/me/profile?alt=json"),
            ("GET", "gmail/v1/users/me/../profile"),
            ("GET", "gmail/v1/users/me/profiles/anything"),
        ):
            with pytest.raises(ValueError, match="path|Método"):
                await client.proxy_request(
                    method,
                    bad_path,
                    connection_id="conn_123",
                    provider_config_key="google-mail",
                )
    finally:
        await http.aclose()

    assert len(captured) == 1
    assert captured[0].method == "GET"
    assert captured[0].url.path == "/proxy/gmail/v1/users/me/profile"
    assert captured[0].headers["Connection-Id"] == "conn_123"
    assert captured[0].headers["Provider-Config-Key"] == "google-mail"


async def test_proxy_calendar_post_and_no_external_or_path_injection():
    captured = {}

    def handler(request: httpx.Request):
        captured["request"] = request
        return httpx.Response(200, json={"id": "event-1", "summary": "Meeting"})

    client, http = _client(handler)
    try:
        response = await client.proxy_request(
            "POST",
            "calendar/v3/calendars/primary/events",
            connection_id="conn_123",
            provider_config_key="google-calendar",
            json_body={"summary": "Review", "start": {"dateTime": "2030-01-01T10:00:00Z"}},
        )
        assert response.body["id"] == "event-1"
        for bad_path in (
            "https://evil.example/gmail/v1/users/me/messages",
            "gmail/v1/../../connections/conn_123",
            "gmail/v1/users/me/messages%2f..",
            "gmail/v1/users/me/users/other/messages",
        ):
            with pytest.raises(ValueError, match="path"):
                await client.proxy_request(
                    "GET", bad_path, connection_id="conn_123", provider_config_key="google-mail"
                )
        with pytest.raises(ValueError, match="Proxy params"):
            await client.proxy_request(
                "GET",
                "gmail/v1/users/me/messages",
                connection_id="conn_123",
                provider_config_key="google-mail",
                params={"Base-Url-Override": "https://evil.example"},
            )
        with pytest.raises(ValueError, match="Proxy body"):
            await client.proxy_request(
                "POST",
                "calendar/v3/calendars/primary/events",
                connection_id="conn_123",
                provider_config_key="google-calendar",
                json_body={"nangoConnectionId": "attacker-id"},
            )
    finally:
        await http.aclose()

    assert captured["request"].method == "POST"
    assert captured["request"].url.path == "/proxy/calendar/v3/calendars/primary/events"
    assert json.loads(captured["request"].content) == {
        "summary": "Review", "start": {"dateTime": "2030-01-01T10:00:00Z"}
    }


async def test_gmail_messages_send_is_post_only_and_accepts_raw_payload():
    captured = []

    def handler(request: httpx.Request):
        captured.append(request)
        return httpx.Response(200, json={"id": "msg-123", "threadId": "thread-1"})

    client, http = _client(handler)
    try:
        raw = base64.urlsafe_b64encode(
            b"To: dest@example.test\r\nSubject: hi\r\n\r\nHello"
        ).rstrip(b"=").decode("ascii")
        response = await client.proxy_request(
            "POST",
            "gmail/v1/users/me/messages/send",
            connection_id="conn_123",
            provider_config_key="google-mail",
            json_body={"raw": raw},
        )
        assert response.body == {"id": "msg-123", "threadId": "thread-1"}

        # POST is allowed on send only: reads stay GET, message ids stay read-only.
        for method, bad_path in (
            ("GET", "gmail/v1/users/me/messages/send"),
            ("POST", "gmail/v1/users/me/messages/msg-123"),
            ("POST", "gmail/v1/users/me/profile"),
            ("POST", "gmail/v1/users/me/history"),
            ("POST", "gmail/v1/users/me/messages/send/../send"),
        ):
            with pytest.raises(ValueError, match="path|Método"):
                await client.proxy_request(
                    method, bad_path, connection_id="conn_123", provider_config_key="google-mail"
                )
        # The credential guard still rejects credential-shaped raw payloads.
        with pytest.raises(ValueError, match="Proxy body"):
            await client.proxy_request(
                "POST",
                "gmail/v1/users/me/messages/send",
                connection_id="conn_123",
                provider_config_key="google-mail",
                json_body={"raw": "token=abc123"},
            )
    finally:
        await http.aclose()

    assert len(captured) == 1
    request = captured[0]
    assert request.method == "POST"
    assert request.url.path == "/proxy/gmail/v1/users/me/messages/send"
    assert json.loads(request.content) == {"raw": raw}
    assert request.headers["Connection-Id"] == "conn_123"
    assert request.headers["Provider-Config-Key"] == "google-mail"


async def test_gmail_history_list_allows_only_get_and_supported_pagination_params():
    captured = []

    def handler(request: httpx.Request):
        captured.append(request)
        return httpx.Response(200, json={"history": [{"id": "100"}]})

    client, http = _client(handler)
    try:
        response = await client.proxy_request(
            "GET",
            "gmail/v1/users/me/history",
            connection_id="conn_123",
            provider_config_key="google-mail",
            params={
                "startHistoryId": "100",
                "pageToken": "opaque/page+token",
                "historyTypes": ["messageAdded", "labelRemoved"],
            },
        )
        assert response.body["history"][0]["id"] == "100"
        with pytest.raises(ValueError, match="Método"):
            await client.proxy_request(
                "POST", "gmail/v1/users/me/history", connection_id="conn_123", provider_config_key="google-mail"
            )
        with pytest.raises(ValueError, match="historyTypes"):
            await client.proxy_request(
                "GET",
                "gmail/v1/users/me/history",
                connection_id="conn_123",
                provider_config_key="google-mail",
                params={"historyTypes": ["arbitrary"]},
            )
    finally:
        await http.aclose()

    assert len(captured) == 1
    assert captured[0].url.path == "/proxy/gmail/v1/users/me/history"
    assert captured[0].url.params["startHistoryId"] == "100"
    assert captured[0].url.params["pageToken"] == "opaque/page+token"
    assert captured[0].url.params.get_list("historyTypes") == ["messageAdded", "labelRemoved"]


async def test_proxy_paths_are_allowlisted_per_provider_and_post_only():
    """Só endpoints usados de fato passam; o resto é barrado na validação.

    A allowlist é por provedor (regex justa) e não-Google é somente POST. Isso
    impede que um caller aponte o proxy para um endpoint arbitrário da conta do
    usuário — inclusive em apps já conectados.
    """
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return httpx.Response(200, json={"id": "page_1"})

    client, http = _client(handler)
    try:
        await client.proxy_request(
            "POST", "v1/pages", connection_id="conn_123", provider_config_key="notion",
        )
        await client.proxy_request(
            "POST", "chat.postMessage", connection_id="conn_123", provider_config_key="slack",
        )
        assert len(seen) == 2
        assert seen[0].url.path == "/proxy/v1/pages"
        assert seen[1].url.path == "/proxy/chat.postMessage"
        assert seen[0].headers["Connection-Id"] == "conn_123"
        assert seen[1].headers["Provider-Config-Key"] == "slack"
    finally:
        await http.aclose()

    # Nao-Google e somente escrita: GET numa rota de criacao nao passa.
    with pytest.raises(ValueError, match="Método"):
        await _client(lambda _r: httpx.Response(200, json={}))[0].proxy_request(
            "GET", "v1/pages", connection_id="conn_123", provider_config_key="notion",
        )

    # Endpoint do mesmo app que a acao nao usa: barrado.
    for method, path in (
        ("POST", "v1/blocks/abc/children"),
        ("POST", "v1/databases"),
        ("POST", "chat.postEphemeral"),
        ("POST", "conversations.list"),
    ):
        with pytest.raises(ValueError, match="Proxy path"):
            await _client(lambda _r: httpx.Response(200, json={}))[0].proxy_request(
                method, path, connection_id="conn_123", provider_config_key="cfg",
            )


async def test_notion_body_rejects_credential_like_values_without_weakening_guard():
    """O guard de corpo continua fail-closed: credenciais em valores não passam."""
    client, http = _client(lambda _r: httpx.Response(200, json={}))
    try:
        with pytest.raises(ValueError, match="credencial"):
            await client.proxy_request(
                "POST",
                "v1/pages",
                connection_id="conn_123",
                provider_config_key="notion",
                json_body={"parent": {"database_id": "db_1"}, "properties": {"note": "token=abc123"}},
            )
    finally:
        await http.aclose()


async def test_http_errors_are_sanitized_and_delete_uses_backend_bearer():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        if request.method == "GET":
            return httpx.Response(401, json={"error": "oauth secret should never appear"})
        return httpx.Response(204)

    client, http = _client(handler)
    try:
        with pytest.raises(NangoClientError) as error:
            await client.proxy_request(
                "GET", "gmail/v1/users/me/messages", connection_id="conn_123", provider_config_key="google-mail"
            )
        await client.delete_connection("conn_123")
    finally:
        await http.aclose()

    assert error.value.status_code == 401
    assert "oauth secret" not in str(error.value)
    assert seen[1].method == "DELETE"
    assert seen[1].url.path == "/connections/conn_123"
    assert seen[1].headers["Authorization"] == "Bearer nango-test-secret"


@pytest.mark.parametrize("bad_id", ["../connections", "conn/123", "conn%2F123", "https://evil"])
async def test_malformed_connection_id_is_blocked_before_http(bad_id):
    http = httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: pytest.fail("unexpected HTTP")))
    client = NangoClient("test-key", client=http)
    try:
        with pytest.raises(ValueError, match="connection_id"):
            await client.get_connection_metadata(bad_id, "google-mail")
    finally:
        await http.aclose()


async def test_http_localhost_requires_explicit_dev_permission():
    with pytest.raises(ValueError, match="HTTPS"):
        NangoClient("test-key", base_url="http://localhost:3003")
    local = NangoClient("test-key", base_url="http://localhost:3003", allow_insecure_localhost=True)
    assert local is not None


def test_api_key_is_required_explicitly_without_environment_fallback(monkeypatch):
    monkeypatch.setenv("NANGO_SECRET_KEY", "must-not-be-used")
    with pytest.raises(TypeError):
        NangoClient()  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="api_key"):
        NangoClient("")
