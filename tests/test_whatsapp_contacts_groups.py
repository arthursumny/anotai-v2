"""Security and shape tests for ephemeral WhatsApp metadata selection."""

import uuid

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlmodel import select

from app.api.v1.whatsapp import get_evolution_adapter
from app.core.config import settings
from app.models import WhatsAppInstance
from app.services.evolution_go_client import EvolutionGoAdapter, EvolutionGoError


@pytest.mark.asyncio
async def test_adapter_uses_instance_apikey_and_strictly_projects_metadata():
    calls = []

    async def handler(request):
        calls.append(request)
        if request.url.path == "/user/contacts":
            return httpx.Response(200, json={"message": "success", "data": [
                {"Jid": "5511@s.whatsapp.net", "FullName": "Contact", "PushName": "ignored",
                 "Body": "secret body", "ProfilePicture": "private", "Token": "secret-token"},
                {"Jid": None, "FullName": "bad"},
                "malformed",
            ]})
        return httpx.Response(200, json={"data": [
            {"JID": "1203@g.us", "Name": "Group", "OwnerJID": "owner", "participants": ["private"]},
            {"JID": "bad@s.whatsapp.net", "Name": "bad"},
        ]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = EvolutionGoAdapter("global-secret", base_url="https://go.example", client=client)
        contacts = await adapter.list_contacts("instance-secret")
        groups = await adapter.list_groups("instance-secret")
        assert repr(adapter) == "EvolutionGoAdapter(base_url=<redacted>)"
    assert [(item.jid, item.display_name, item.is_group) for item in contacts] == [
        ("5511@s.whatsapp.net", "Contact", False)
    ]
    assert [(item.jid, item.display_name, item.is_group) for item in groups] == [
        ("1203@g.us", "Group", True)
    ]
    assert [request.url.path for request in calls] == ["/user/contacts", "/group/list"]
    assert all(request.headers["apikey"] == "instance-secret" for request in calls)
    assert all("authorization" not in request.headers for request in calls)
    assert "instance-secret" not in repr(adapter) and "global-secret" not in repr(adapter)


@pytest.mark.asyncio
async def test_adapter_caps_rows_and_bounds_names():
    rows = [
        {"Jid": f"{index}@s.whatsapp.net", "FullName": "x" * 500}
        for index in range(201)
    ]

    async def handler(request):
        return httpx.Response(200, json={"data": rows})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = EvolutionGoAdapter("key", base_url="https://go.example", client=client)
        result = await adapter.list_contacts("token")
    assert len(result) == 200
    assert all(len(item.display_name) == 120 for item in result)


class MetadataEvolution:
    async def list_contacts(self, token):
        assert token == "decrypted-instance-secret"
        return [type("Item", (), {"jid": "1@s.whatsapp.net", "display_name": "Alice", "is_group": False})()]

    async def list_groups(self, token):
        raise EvolutionGoError(503, "http_error")


@pytest.fixture
def metadata_evolution(client):
    async def dependency():
        yield MetadataEvolution()

    from app.main import app
    app.dependency_overrides[get_evolution_adapter] = dependency
    yield
    app.dependency_overrides.pop(get_evolution_adapter, None)


async def _connected_instance(client, auth_headers, db_session, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "WHATSAPP_EVOLUTION_BASE_URL", "https://go.example")
    monkeypatch.setattr(settings, "WHATSAPP_EVOLUTION_GLOBAL_API_KEY", "global-secret")
    monkeypatch.setattr(settings, "WHATSAPP_WEBHOOK_BASE_URL", "https://hooks.example")
    from app.core.whatsapp_crypto import encrypt_instance_token
    from app.models.user import User
    from app.models.workspace import WorkspaceMembership
    user = (await db_session.exec(select(User))).first()
    instance = WhatsAppInstance(
        workspace_id=user.workspace_id, user_id=user.id, go_instance_name="anotai-test",
        token_ciphertext="pending", webhook_secret_hash="0" * 64, status="connected",
    )
    instance.token_ciphertext = encrypt_instance_token(instance.id, "decrypted-instance-secret")
    db_session.add(instance)
    await db_session.commit()
    return instance


async def test_contacts_endpoint_is_private_uncached_and_projects_fields(
    client, auth_headers, db_session, monkeypatch, metadata_evolution, caplog
):
    await _connected_instance(client, auth_headers, db_session, monkeypatch)
    response = await client.get("/api/v1/whatsapp/contacts", headers=auth_headers[0])
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == [{"jid": "1@s.whatsapp.net", "display_name": "Alice", "is_group": False}]
    assert "secret" not in response.text and "participants" not in response.text
    assert "decrypted-instance-secret" not in caplog.text


async def test_foreign_owner_gets_404_and_provider_failure_is_sanitized(
    client, auth_headers, db_session, monkeypatch, metadata_evolution
):
    await _connected_instance(client, auth_headers, db_session, monkeypatch)
    register = await client.post("/api/v1/auth/register", json={
        "name": "Other", "email": f"{uuid.uuid4()}@anotai.com", "password": "senha-forte-123",
    })
    login = await client.post("/api/v1/auth/login", data={
        "username": register.json()["email"], "password": "senha-forte-123",
    })
    foreign = {"Authorization": f"Bearer {login.json()['access_token']}"}
    assert (await client.get("/api/v1/whatsapp/contacts", headers=foreign)).status_code == 404
    response = await client.get("/api/v1/whatsapp/groups", headers=auth_headers[0])
    assert response.status_code == 502
    assert "instance-secret" not in response.text
