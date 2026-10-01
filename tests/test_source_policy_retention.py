"""Per-source retention settings preserve existing source-policy behavior."""

import uuid
from datetime import datetime, timezone

import pytest
from sqlmodel import select

from app.api.v1 import source_policies
from app.core.config import settings
from app.models.conversation_state import ConversationState
from app.models.pending_conversation_item import PendingConversationItem
from app.models.source_policy import SourceMonitoringPolicy
from app.models.user import User
from app.models.whatsapp_instance import WhatsAppInstance


@pytest.mark.asyncio
async def test_retention_survives_policy_mode_edit(client, auth_headers):
    headers, _ = auth_headers
    response = await client.put("/api/v1/source-policies/whatsapp", headers=headers, json={
        "monitor_enabled": True, "retention_days": 7,
    })
    assert response.status_code == 200
    assert response.json()["retention_days"] == 7

    response = await client.put("/api/v1/source-policies/whatsapp", headers=headers, json={
        "monitor_enabled": True, "mode": "ALLOWLIST", "selected_ids": ["chat-1"],
    })
    assert response.status_code == 200
    assert response.json()["retention_days"] == 7


@pytest.mark.asyncio
async def test_explicit_null_resets_retention_to_global_default(client, auth_headers, monkeypatch):
    headers, _ = auth_headers
    monkeypatch.setattr(settings, "DEFAULT_EVENT_RETENTION_DAYS", 30)
    created = await client.put("/api/v1/source-policies/whatsapp", headers=headers, json={
        "monitor_enabled": True, "retention_days": 7,
    })
    assert created.status_code == 200
    reset = await client.put("/api/v1/source-policies/whatsapp", headers=headers, json={
        "monitor_enabled": True, "retention_days": None,
    })
    assert reset.status_code == 200
    assert reset.json()["retention_days"] is None


@pytest.mark.parametrize("days", [0, 366])
@pytest.mark.asyncio
async def test_retention_rejects_values_outside_range(client, auth_headers, days):
    headers, _ = auth_headers
    response = await client.put("/api/v1/source-policies/whatsapp", headers=headers, json={
        "monitor_enabled": True, "retention_days": days,
    })
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_whatsapp_retention_only_edit_preserves_pending_context(
    client, auth_headers, db_session,
):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    assert user is not None and user.workspace_id is not None
    instance = WhatsAppInstance(
        workspace_id=user.workspace_id, user_id=user.id, go_instance_name="retention-test",
        token_ciphertext="encrypted", webhook_secret_hash="hash",
    )
    db_session.add(instance)
    await db_session.flush()
    db_session.add_all([
        ConversationState(
            workspace_id=user.workspace_id, user_id=user.id, instance_id=instance.id,
            conversation_id="chat-1",
        ),
        PendingConversationItem(
            workspace_id=user.workspace_id, user_id=user.id, instance_id=instance.id,
            conversation_id="chat-1", source_event="event-1", message_id="message-1",
            summary="pending", asked_at=datetime.now(timezone.utc), confidence=0.9,
        ),
        SourceMonitoringPolicy(
            workspace_id=user.workspace_id, user_id=user.id, source="whatsapp", mode="ALL",
        ),
    ])
    await db_session.commit()

    response = await client.put("/api/v1/source-policies/whatsapp", headers=headers, json={
        "monitor_enabled": True, "retention_days": 7,
    })
    assert response.status_code == 200
    assert len((await db_session.exec(select(ConversationState))).all()) == 1
    assert len((await db_session.exec(select(PendingConversationItem))).all()) == 1


@pytest.mark.asyncio
async def test_gmail_retention_only_edit_does_not_renew_watch(
    client, auth_headers, monkeypatch,
):
    headers, _ = auth_headers
    assert (await client.put("/api/v1/source-policies/gmail", headers=headers, json={
        "monitor_enabled": True,
    })).status_code == 200

    def unexpected_nango(**_kwargs):
        raise AssertionError("retention-only edit must not access Nango")

    monkeypatch.setattr(source_policies, "NangoClient", unexpected_nango)
    response = await client.put("/api/v1/source-policies/gmail", headers=headers, json={
        "monitor_enabled": True, "retention_days": 7,
    })
    assert response.status_code == 200
    assert response.json()["retention_days"] == 7


@pytest.mark.asyncio
async def test_retention_update_is_scoped_to_authenticated_user(client, auth_headers, db_session):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    assert user is not None and user.workspace_id is not None
    other = User(
        name="Other retention owner", email=f"{uuid.uuid4()}@example.com", hashed_password="unused",
        workspace_id=user.workspace_id,
    )
    db_session.add(other)
    await db_session.flush()
    other_policy = SourceMonitoringPolicy(
        workspace_id=user.workspace_id, user_id=other.id, source="whatsapp", mode="ALL",
        retention_days=21,
    )
    db_session.add(other_policy)
    await db_session.commit()

    response = await client.put("/api/v1/source-policies/whatsapp", headers=headers, json={
        "monitor_enabled": True, "retention_days": 7,
    })
    assert response.status_code == 200
    await db_session.refresh(other_policy)
    assert other_policy.retention_days == 21
