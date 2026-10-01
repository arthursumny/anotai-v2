"""Privacy-safe audit coverage for user initiated disconnects."""

import uuid

import pytest
from cryptography.fernet import Fernet
from sqlmodel import select

from app.api.v1 import integrations
from app.api.v1.whatsapp import get_optional_evolution_adapter
from app.core.config import settings
from app.core.whatsapp_crypto import encrypt_instance_token
from app.models.audit_event import AuditEvent
from app.models.integration_connection import IntegrationConnection
from app.models.user import User
from app.models.whatsapp_instance import WhatsAppInstance
from app.models.source_policy import SourceMonitoringPolicy
from app.services.evolution_go_client import EvolutionGoError


class FakeNango:
    deleted = []

    def __init__(self, *_args, **_kwargs):
        pass

    async def delete_connection(self, connection_id):
        type(self).deleted.append(connection_id)


class FailingEvolution:
    async def logout(self, _token):
        raise EvolutionGoError(503, "provider failure")


@pytest.mark.asyncio
async def test_integration_disconnect_audit_is_safe_and_not_duplicated(
    client, auth_headers, db_session, monkeypatch,
):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    row = IntegrationConnection(
        workspace_id=user.workspace_id, user_id=user.id, provider="google_gmail",
        provider_config_key="gmail-config", nango_connection_id="private-nango-id",
        account_email="private@example.com", status="active",
    )
    db_session.add(row)
    await db_session.commit()
    monkeypatch.setattr(settings, "NANGO_ENV_KEY", "test-api-key")
    monkeypatch.setattr(settings, "NANGO_BASE_URL", "https://nango.example.test")
    monkeypatch.setattr(settings, "NANGO_GOOGLE_GMAIL_PROVIDER_CONFIG_KEY", "gmail-config")
    FakeNango.deleted = []
    monkeypatch.setattr(integrations, "NangoClient", FakeNango)

    response = await client.delete(f"/api/v1/integrations/connections/{row.id}", headers=headers)
    repeated = await client.delete(f"/api/v1/integrations/connections/{row.id}", headers=headers)
    assert response.status_code == repeated.status_code == 204
    assert FakeNango.deleted == ["private-nango-id", "private-nango-id"]
    events = (await db_session.exec(select(AuditEvent))).all()
    assert len(events) == 1
    event = events[0]
    assert (event.event, event.action, event.status, event.source) == (
        "GOOGLE_CONNECTION_DISCONNECTED", "disconnect", "success", "gmail",
    )
    assert event.target_kind == "integration_connection" and event.target_ref == str(row.id)
    assert event.metadata_json == {"reason_code": "disconnect", "count": 1}
    serialized = repr(event.model_dump())
    assert "private@example.com" not in serialized and "private-nango-id" not in serialized


@pytest.mark.asyncio
async def test_foreign_integration_disconnect_does_not_create_audit(
    client, auth_headers, db_session, monkeypatch,
):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    foreign = IntegrationConnection(
        workspace_id=user.workspace_id, user_id=uuid.uuid4(), provider="google_calendar",
        provider_config_key="calendar-config", nango_connection_id="foreign-id", status="active",
    )
    db_session.add(foreign)
    await db_session.commit()
    result = await client.delete(f"/api/v1/integrations/connections/{foreign.id}", headers=headers)
    assert result.status_code == 404
    assert (await db_session.exec(select(AuditEvent))).all() == []


@pytest.mark.asyncio
async def test_whatsapp_disconnect_audits_local_purge_even_if_remote_logout_fails(
    client, auth_headers, db_session, monkeypatch,
):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    monkeypatch.setattr(settings, "WHATSAPP_TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    instance = WhatsAppInstance(
        workspace_id=user.workspace_id, user_id=user.id, go_instance_name="instance",
        token_ciphertext="pending", webhook_secret_hash="0" * 64, status="connected",
        phone_jid="private@s.whatsapp.net",
    )
    instance.token_ciphertext = encrypt_instance_token(instance.id, "private-token")
    db_session.add(SourceMonitoringPolicy(
        workspace_id=user.workspace_id, user_id=user.id, source="whatsapp",
    ))
    db_session.add(instance)
    await db_session.commit()

    async def adapter_dependency():
        yield FailingEvolution()

    from app.main import app
    app.dependency_overrides[get_optional_evolution_adapter] = adapter_dependency
    try:
        response = await client.delete("/api/v1/whatsapp/disconnect", headers=headers)
    finally:
        app.dependency_overrides.pop(get_optional_evolution_adapter, None)

    assert response.status_code == 200
    assert response.json() == {
        "status": "disconnected",
        "message": "Não foi possível confirmar a saída no WhatsApp; remova o aparelho vinculado nas configurações do WhatsApp.",
    }
    events = (await db_session.exec(select(AuditEvent))).all()
    assert len(events) == 1
    event = events[0]
    assert (event.event, event.action, event.status, event.source) == (
        "WHATSAPP_DISCONNECTED", "disconnect", "success", "whatsapp",
    )
    assert event.target_kind == "whatsapp_instance" and event.target_ref == str(instance.id)
    assert event.metadata_json == {"reason_code": "disconnect", "count": 1}
    assert "private@s.whatsapp.net" not in repr(event.model_dump())
    assert "private-token" not in repr(event.model_dump())
    saved = await db_session.get(WhatsAppInstance, instance.id)
    assert saved.status == "disconnected" and saved.phone_jid is None
    assert (await db_session.exec(select(SourceMonitoringPolicy))).all() == []
