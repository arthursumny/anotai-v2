"""Security-focused API tests for WhatsApp connection bootstrap."""

import uuid

import pytest
from cryptography.fernet import Fernet
from sqlmodel import select

from app.api.v1.whatsapp import get_evolution_adapter, get_optional_evolution_adapter
from app.core.config import settings
from app.core.whatsapp_crypto import decrypt_instance_token, hash_webhook_secret
from app.models import ConversationState, PendingConversationItem, WhatsAppInstance
from app.models.source_policy import SourceMonitoringPolicy
from app.models.user import User
from app.models.workspace import WorkspaceMembership


class FakeEvolution:
    def __init__(self):
        self.created = []
        self.logged_out = []
        self.fail_create = False
        self.fail_logout = False

    async def create_instance(self, name, token, *, webhook=None):
        if self.fail_create:
            from app.services.evolution_go_client import EvolutionGoError
            raise EvolutionGoError(500, "http_error")
        self.created.append((name, token, webhook))

    async def get_qr(self, token):
        return type("Qr", (), {"code": "data:image/png;base64,abc"})()

    async def get_status(self, token):
        return type("Status", (), {"connected": True, "logged_in": True, "my_jid": "123456789@s.whatsapp.net"})()

    async def logout(self, token):
        if self.fail_logout:
            from app.services.evolution_go_client import EvolutionGoError
            raise EvolutionGoError(503, "http_error")
        self.logged_out.append(token)


@pytest.fixture
def evolution(client):
    fake = FakeEvolution()

    async def dependency():
        yield fake

    from app.main import app
    app.dependency_overrides[get_evolution_adapter] = dependency
    app.dependency_overrides[get_optional_evolution_adapter] = dependency
    yield fake
    app.dependency_overrides.pop(get_evolution_adapter, None)
    app.dependency_overrides.pop(get_optional_evolution_adapter, None)


def setup_config(monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_EVOLUTION_BASE_URL", "https://evolution.example")
    monkeypatch.setattr(settings, "WHATSAPP_EVOLUTION_GLOBAL_API_KEY", "global-private-key")
    monkeypatch.setattr(settings, "WHATSAPP_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "WHATSAPP_WEBHOOK_BASE_URL", "https://hooks.example")


async def connect_instance(client, auth_headers, db_session, monkeypatch):
    setup_config(monkeypatch)
    response = await client.post("/api/v1/whatsapp/connect", headers=auth_headers[0])
    assert response.status_code == 201, response.text
    instance = (await db_session.exec(select(WhatsAppInstance))).first()
    return response, instance


async def test_connect_protects_token_and_never_returns_secrets(client, auth_headers, db_session, evolution, monkeypatch, caplog):
    response, instance = await connect_instance(client, auth_headers, db_session, monkeypatch)
    token = evolution.created[0][1]
    webhook = evolution.created[0][2]
    secret = webhook.rsplit("/", 1)[1]
    assert response.json() == {"status": "pending_qr", "instance_id": str(instance.id)}
    assert token not in instance.token_ciphertext and token not in repr(instance)
    assert secret not in instance.webhook_secret_hash and secret not in repr(instance)
    assert decrypt_instance_token(instance.id, instance.token_ciphertext) == token
    assert token not in response.text and secret not in response.text
    assert "global-private-key" not in caplog.text and secret not in caplog.text


async def test_config_missing_fails_closed(client, auth_headers, monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_EVOLUTION_BASE_URL", "")
    result = await client.post("/api/v1/whatsapp/connect", headers=auth_headers[0])
    assert result.status_code == 503


async def test_provider_create_failure_is_sanitized_and_marks_row_error(client, auth_headers, db_session, evolution, monkeypatch, caplog):
    setup_config(monkeypatch)
    evolution.fail_create = True
    result = await client.post("/api/v1/whatsapp/connect", headers=auth_headers[0])
    assert result.status_code == 502
    assert "global-private-key" not in result.text
    instance = (await db_session.exec(select(WhatsAppInstance))).first()
    assert instance.status == "error"
    assert "global-private-key" not in caplog.text


async def test_owner_qr_status_are_ephemeral_and_phone_masked(client, auth_headers, db_session, evolution, monkeypatch):
    _, instance = await connect_instance(client, auth_headers, db_session, monkeypatch)
    qr = await client.get("/api/v1/whatsapp/qr", headers=auth_headers[0])
    assert qr.status_code == 200 and qr.json()["qr"].startswith("data:image/png;base64,")
    assert qr.headers["cache-control"] == "no-store"
    status_response = await client.get("/api/v1/whatsapp/status", headers=auth_headers[0])
    assert status_response.json() == {"status": "connected", "phone": "12***89"}
    assert "123456789" not in status_response.text
    assert (await db_session.get(WhatsAppInstance, instance.id)).status == "connected"


async def test_foreign_owner_cannot_get_qr(client, auth_headers, db_session, evolution, monkeypatch):
    _, instance = await connect_instance(client, auth_headers, db_session, monkeypatch)
    other = await client.post("/api/v1/auth/register", json={
        "name": "Other", "email": f"{uuid.uuid4()}@anotai.com", "password": "senha-forte-123",
    })
    assert other.status_code == 201
    login = await client.post("/api/v1/auth/login", data={"username": other.json()["email"], "password": "senha-forte-123"})
    foreign = {"Authorization": f"Bearer {login.json()['access_token']}"}
    response = await client.get("/api/v1/whatsapp/qr", headers=foreign)
    assert response.status_code == 404
    disconnect = await client.delete("/api/v1/whatsapp/disconnect", headers=foreign)
    assert disconnect.status_code == 404
    assert instance.user_id != uuid.UUID(other.json()["id"])
    assert (await db_session.get(WhatsAppInstance, instance.id)).status == "pending_qr"


async def test_webhook_authentication_precedes_body_and_valid_body_is_not_persisted(client, auth_headers, db_session, evolution, monkeypatch):
    _, instance = await connect_instance(client, auth_headers, db_session, monkeypatch)
    webhook = evolution.created[0][2]
    secret = webhook.rsplit("/", 1)[1]
    response = await client.post(
        f"/api/v1/whatsapp/webhook/{instance.id}/wrong", content=b"not-json-body",
    )
    assert response.status_code == 404
    before = (await db_session.exec(select(ConversationState))).all()
    accepted = await client.post(
        f"/api/v1/whatsapp/webhook/{instance.id}/{secret}",
        json={"messages": [{"text": "must not persist"}]},
    )
    assert accepted.status_code == 204
    assert (await db_session.exec(select(ConversationState))).all() == before == []
    assert (await db_session.exec(select(PendingConversationItem))).all() == []


async def test_disconnect_logs_out_and_purges_owned_policy_and_conversation_state(client, auth_headers, db_session, evolution, monkeypatch):
    _, instance = await connect_instance(client, auth_headers, db_session, monkeypatch)
    user_id = instance.user_id
    db_session.add(SourceMonitoringPolicy(
        workspace_id=instance.workspace_id, user_id=user_id, source="whatsapp",
    ))
    db_session.add(ConversationState(
        workspace_id=instance.workspace_id, user_id=user_id, instance_id=instance.id,
        conversation_id="person@s.whatsapp.net",
    ))
    from datetime import datetime, timezone
    db_session.add(PendingConversationItem(
        workspace_id=instance.workspace_id, user_id=user_id, instance_id=instance.id,
        conversation_id="person@s.whatsapp.net", source_event="ev-1", message_id="m-1",
        summary="private summary", asked_at=datetime.now(timezone.utc), confidence=0.9,
    ))
    await db_session.commit()
    response = await client.delete("/api/v1/whatsapp/disconnect", headers=auth_headers[0])
    assert response.status_code == 200
    assert "pode precisar de limpeza" in response.json()["message"]
    assert len(evolution.logged_out) == 1
    assert (await db_session.get(WhatsAppInstance, instance.id)).status == "disconnected"
    assert (await db_session.exec(select(SourceMonitoringPolicy))).all() == []
    assert (await db_session.exec(select(ConversationState))).all() == []
    assert (await db_session.exec(select(PendingConversationItem))).all() == []


async def test_disconnect_is_local_first_when_remote_logout_fails_and_reconnects(
    client, auth_headers, db_session, evolution, monkeypatch
):
    from app.models.action_item import FollowUp
    from app.models.normalized_event import NormalizedEvent
    from datetime import datetime, timezone

    _, instance = await connect_instance(client, auth_headers, db_session, monkeypatch)
    instance.status = "connected"
    db_session.add(instance)
    event = NormalizedEvent(
        workspace_id=instance.workspace_id, user_id=instance.user_id, source="whatsapp",
        type="MESSAGE_RECEIVED", external_id="wa:one", occurred_at=datetime.now(timezone.utc),
    )
    db_session.add(event)
    db_session.add(SourceMonitoringPolicy(
        workspace_id=instance.workspace_id, user_id=instance.user_id, source="whatsapp",
    ))
    db_session.add(ConversationState(
        workspace_id=instance.workspace_id, user_id=instance.user_id, instance_id=instance.id,
        conversation_id="person@s.whatsapp.net",
    ))
    db_session.add(PendingConversationItem(
        workspace_id=instance.workspace_id, user_id=instance.user_id, instance_id=instance.id,
        conversation_id="person@s.whatsapp.net", source_event="ev-2", message_id="m-2",
        summary="private summary", asked_at=datetime.now(timezone.utc), confidence=0.9,
    ))
    followup = FollowUp(
        workspace_id=instance.workspace_id, user_id=instance.user_id, source_event_id=event.id,
        idempotency_key="wa-follow-up", title="private followup", status="waiting",
        response_status="pending",
    )
    db_session.add(followup)
    await db_session.commit()
    evolution.fail_logout = True
    response = await client.delete("/api/v1/whatsapp/disconnect", headers=auth_headers[0])
    assert response.status_code == 200
    assert response.json()["status"] == "disconnected"
    assert response.json()["message"] == (
        "Não foi possível confirmar a saída no WhatsApp; remova o aparelho vinculado nas configurações do WhatsApp."
    )
    assert (await db_session.get(WhatsAppInstance, instance.id)).status == "disconnected"
    assert (await db_session.exec(select(SourceMonitoringPolicy))).all() == []
    assert (await db_session.exec(select(ConversationState))).all() == []
    assert (await db_session.exec(select(PendingConversationItem))).all() == []
    saved_followup = await db_session.get(FollowUp, followup.id)
    assert (saved_followup.status, saved_followup.response_status) == ("cancelled", "dismissed")
    reconnect = await client.post("/api/v1/whatsapp/connect", headers=auth_headers[0])
    assert reconnect.status_code == 201


@pytest.mark.parametrize("missing_setting", [
    "WHATSAPP_EVOLUTION_GLOBAL_API_KEY", "WHATSAPP_TOKEN_ENCRYPTION_KEY",
])
async def test_disconnect_with_missing_global_config_or_encryption_key_still_revokes_local(
    client, auth_headers, db_session, evolution, monkeypatch, missing_setting
):
    _, instance = await connect_instance(client, auth_headers, db_session, monkeypatch)
    db_session.add(SourceMonitoringPolicy(
        workspace_id=instance.workspace_id, user_id=instance.user_id, source="whatsapp",
    ))
    db_session.add(ConversationState(
        workspace_id=instance.workspace_id, user_id=instance.user_id, instance_id=instance.id,
        conversation_id="person@s.whatsapp.net",
    ))
    await db_session.commit()
    monkeypatch.setattr(settings, missing_setting, "")
    if missing_setting == "WHATSAPP_EVOLUTION_GLOBAL_API_KEY":
        from app.main import app
        app.dependency_overrides.pop(get_optional_evolution_adapter, None)
    response = await client.delete("/api/v1/whatsapp/disconnect", headers=auth_headers[0])
    assert response.status_code == 200
    assert "Não foi possível confirmar" in response.json()["message"]
    assert (await db_session.get(WhatsAppInstance, instance.id)).status == "disconnected"
    assert (await db_session.exec(select(SourceMonitoringPolicy))).all() == []
    assert (await db_session.exec(select(ConversationState))).all() == []


async def test_disconnected_webhook_rejected_before_body_is_read(client, auth_headers, db_session, evolution, monkeypatch):
    _, instance = await connect_instance(client, auth_headers, db_session, monkeypatch)
    secret = evolution.created[0][2].rsplit("/", 1)[1]
    instance.status = "disconnected"
    db_session.add(instance)
    await db_session.commit()
    response = await client.post(
        f"/api/v1/whatsapp/webhook/{instance.id}/{secret}", content=b"not-json-body",
    )
    assert response.status_code == 404
