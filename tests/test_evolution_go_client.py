import json

import httpx
import pytest

from app.services.evolution_go_client import EvolutionGoAdapter, EvolutionGoError


GLOBAL_KEY = "global-secret-key"
INSTANCE_TOKEN = "instance-secret-token"


def make_adapter(handler):
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return EvolutionGoAdapter(GLOBAL_KEY, base_url="https://evolution.example", client=client), client


@pytest.mark.asyncio
async def test_create_instance_uses_global_apikey_and_only_documented_body():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json={"data": {"token": "must-not-be-returned", "id": "opaque"}})

    adapter, client = make_adapter(handler)
    try:
        result = await adapter.create_instance("team-1", INSTANCE_TOKEN, webhook="https://hooks.example/whatsapp")
    finally:
        await client.aclose()
    request = requests[0]
    assert result is None
    assert request.method == "POST"
    assert request.url.path == "/instance/create"
    assert request.headers["apikey"] == GLOBAL_KEY
    assert "authorization" not in request.headers
    assert json.loads(request.content) == {
        "name": "team-1",
        "token": INSTANCE_TOKEN,
        "webhook": "https://hooks.example/whatsapp",
    }


@pytest.mark.asyncio
async def test_qr_status_delete_and_logout_use_correct_credentials_and_allowlisted_values():
    requests = []
    fixture_by_path = {
        "/instance/qr": {"data": {"qrcode": "pairing-value", "code": "data:image/png;base64,abc123"}},
        "/instance/status": {
            "data": {"connected": True, "loggedIn": True, "name": "team-1", "myJid": "123@s.whatsapp.net", "secret": "discard"}
        },
        "/instance/delete/opaque-id": {"data": {"deleted": True}},
        "/instance/logout": {"data": {"loggedOut": True}},
    }

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=fixture_by_path[request.url.path])

    adapter, client = make_adapter(handler)
    try:
        qr = await adapter.get_qr(INSTANCE_TOKEN)
        status = await adapter.get_status(INSTANCE_TOKEN)
        await adapter.delete_instance("opaque-id")
        await adapter.logout(INSTANCE_TOKEN)
    finally:
        await client.aclose()

    assert qr.qrcode == "pairing-value"
    assert qr.code == "data:image/png;base64,abc123"
    assert "pairing-value" not in repr(qr)
    assert status.connected is True and status.logged_in is True
    assert status.name == "team-1" and status.my_jid == "123@s.whatsapp.net"
    assert not hasattr(status, "secret")
    assert [request.url.path for request in requests] == list(fixture_by_path)
    assert [request.headers["apikey"] for request in requests] == [
        INSTANCE_TOKEN, INSTANCE_TOKEN, GLOBAL_KEY, INSTANCE_TOKEN
    ]
    assert all("authorization" not in request.headers for request in requests)


@pytest.mark.asyncio
async def test_rejects_bad_url_and_credentials_without_network():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"data": {}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    for url, opts in (
        ("http://evolution.example", {}),
        ("https://user:pass@evolution.example", {}),
        ("https://evolution.example/path", {}),
        ("http://127.0.0.1", {}),
    ):
        with pytest.raises(ValueError):
            EvolutionGoAdapter(GLOBAL_KEY, base_url=url, client=client, **opts)
    with pytest.raises(ValueError):
        EvolutionGoAdapter("bad\nkey", base_url="https://evolution.example", client=client)
    adapter = EvolutionGoAdapter(GLOBAL_KEY, base_url="https://evolution.example", client=client)
    with pytest.raises(ValueError):
        await adapter.get_status("bad\rtoken")
    with pytest.raises(ValueError):
        await adapter.create_instance("../arbitrary", INSTANCE_TOKEN)
    with pytest.raises(ValueError):
        await adapter.delete_instance("name with spaces")
    assert calls == []
    await client.aclose()


@pytest.mark.asyncio
async def test_allows_explicit_private_http_development_url():
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200)))
    adapter = EvolutionGoAdapter(
        GLOBAL_KEY,
        base_url="http://127.0.0.1:8080",
        client=client,
        allow_insecure_private=True,
    )
    try:
        await adapter.logout(INSTANCE_TOKEN)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_http_error_does_not_expose_response_body_or_credentials():
    secret_error = f"{INSTANCE_TOKEN} {GLOBAL_KEY} raw response detail"
    adapter, client = make_adapter(lambda request: httpx.Response(500, text=secret_error))
    try:
        with pytest.raises(EvolutionGoError) as caught:
            await adapter.get_status(INSTANCE_TOKEN)
    finally:
        await client.aclose()
    error = caught.value
    assert error.status_code == 500
    assert error.kind == "http_error"
    assert str(error) == "Evolution Go HTTP 500 (http_error)"
    assert INSTANCE_TOKEN not in repr(error)
    assert GLOBAL_KEY not in repr(error)
    assert "raw response detail" not in repr(error)
