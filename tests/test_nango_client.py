"""HTTP-mocked contract tests for the generic Nango adapter."""

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


@pytest.mark.parametrize(
    "method, endpoint, params, json_body",
    [
        ("POST", "tasks/v1/lists/@default/tasks", None, {"title": "Lembrete"}),
        ("POST", "drive/v3/files", None, {"name": "nota.md", "mimeType": "text/markdown"}),
        (
            "POST",
            "sheets/v4/spreadsheets/s1/values/Sheet1!A1:append",
            {"valueInputOption": "USER_ENTERED"},
            {"range": "Sheet1!A1", "values": [["a", "b"]]},
        ),
        ("POST", "v1/pages", None, {"parent": {"database_id": "db_1"}}),
        ("GET", "gmail/v1/users/me/messages", {"maxResults": 10}, None),
        ("GET", "anything/v1/resource", None, None),
    ],
)
async def test_generic_proxy_endpoints_attach_only_server_owned_headers(
    method, endpoint, params, json_body
):
    captured = {}

    def handler(request: httpx.Request):
        captured["request"] = request
        return httpx.Response(200, json={"ok": True})

    client, http = _client(handler)
    try:
        response = await client.proxy_request(
            method, "google-mail", "conn_123", endpoint, params=params, json=json_body,
        )
    finally:
        await http.aclose()

    request = captured["request"]
    assert request.method == method
    assert request.url.path == f"/proxy/{endpoint}"
    assert request.headers["Authorization"] == "Bearer nango-test-secret"
    assert request.headers["Connection-Id"] == "conn_123"
    assert request.headers["Provider-Config-Key"] == "google-mail"
    assert "Base-Url-Override" not in request.headers
    assert response.status_code == 200
    assert response.body == {"ok": True}


async def test_put_patch_delete_and_lowercase_methods_pass_through():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request.method)
        return httpx.Response(200, json={})

    client, http = _client(handler)
    try:
        for method in ("PUT", "PATCH", "DELETE"):
            await client.proxy_request(method.lower(), "cfg", "conn_123", "v1/resources/abc")
    finally:
        await http.aclose()

    assert seen == ["PUT", "PATCH", "DELETE"]


@pytest.mark.parametrize("method", ["OPTIONS", "HEAD", "TRACE", "", "GETS"])
async def test_unknown_methods_are_rejected(method):
    client, http = _client(lambda _request: pytest.fail("unexpected HTTP"))
    try:
        with pytest.raises(ValueError, match="method"):
            await client.proxy_request(method, "cfg", "conn_123", "v1/resources")
    finally:
        await http.aclose()


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://evil.example/x",
        "../x",
        "x/../y",
        "x?y=1",
        "x#f",
        "x\\y",
        " x",
        "",
        "a b",
        "a%2Fb",
        "a//b",
    ],
)
async def test_invalid_endpoints_are_rejected(endpoint):
    client, http = _client(lambda _request: pytest.fail("unexpected HTTP"))
    try:
        with pytest.raises(ValueError, match="endpoint"):
            await client.proxy_request("GET", "cfg", "conn_123", endpoint)
    finally:
        await http.aclose()


@pytest.mark.parametrize(
    "key",
    ["Connection-Id", "Base-Url-Override", "Provider-Config-Key", "Authorization", "Content-Length"],
)
async def test_header_scope_param_names_are_rejected(key):
    client, http = _client(lambda _request: pytest.fail("unexpected HTTP"))
    try:
        with pytest.raises(ValueError, match="params"):
            await client.proxy_request("GET", "cfg", "conn_123", "v1/resource", params={key: "x"})
    finally:
        await http.aclose()


async def test_json_body_forwarded_verbatim_and_params_become_query_string():
    captured = {}

    def handler(request: httpx.Request):
        captured["request"] = request
        return httpx.Response(200, json={"id": "x"})

    payload = {
        # Credential-shaped user content is legitimate note text and must survive.
        "note": "token=abc123",
        "nested": {"list": [1, 2, {"deep": True}]},
        "scalar": 42,
    }
    client, http = _client(handler)
    try:
        await client.proxy_request(
            "POST",
            "cfg",
            "conn_123",
            "v1/pages",
            params={"a": "b", "n": 3, "flag": True},
            json=payload,
        )
    finally:
        await http.aclose()

    request = captured["request"]
    assert json.loads(request.content) == payload
    assert request.url.params["a"] == "b"
    assert request.url.params["n"] == "3"
    assert request.url.params["flag"] == "true"


@pytest.mark.parametrize(
    "body",
    [
        [1, "two", {"three": 3}],
        "plain scalar",
        7,
    ],
)
async def test_scalar_and_list_json_bodies_pass_through(body):
    captured = {}

    def handler(request: httpx.Request):
        captured["request"] = request
        return httpx.Response(200, json={})

    client, http = _client(handler)
    try:
        await client.proxy_request("POST", "cfg", "conn_123", "anything/v1/resource", json=body)
    finally:
        await http.aclose()

    assert json.loads(captured["request"].content) == body


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
                "GET", "google-mail", "conn_123", "gmail/v1/users/me/messages"
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
