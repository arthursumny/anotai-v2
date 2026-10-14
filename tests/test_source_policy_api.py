"""Authentication, opt-in and user isolation for source-policy endpoints."""

import uuid

import pytest
from sqlmodel import select
from datetime import datetime, timezone

from app.models.action_item import FollowUp
from app.models.conversation_state import ConversationState
from app.models.source_policy import SourceMonitoringPolicy
from app.models.normalized_event import NormalizedEvent
from app.models.pending_conversation_item import PendingConversationItem
from app.models.gmail_watch_state import GmailWatchState
from app.models.integration_connection import IntegrationConnection
from app.models.user import User
from app.models.workspace import WorkspaceMembership
from app.models.whatsapp_instance import WhatsAppInstance
from app.core.config import settings
from app.services.nango_client import ProxyResponse
from app.api.v1 import source_policies


class _WatchNango:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    async def proxy_request(self, method, provider_config_key, connection_id, endpoint, params=None, json=None):
        self.calls.append((method, endpoint, {
            "connection_id": connection_id,
            "provider_config_key": provider_config_key,
            "params": params,
            "json": json,
        }))
        if self.fail:
            raise RuntimeError("Nango unavailable")
        return ProxyResponse(200, {"historyId": "42", "expiration": "1760000000000"})


async def _active_gmail(db_session, auth_user_id, *, state="paused", email=True):
    user = await db_session.get(User, uuid.UUID(auth_user_id))
    connection = IntegrationConnection(
        workspace_id=user.workspace_id, user_id=user.id, provider="google_gmail", status="active",
        provider_config_key="gmail-test", nango_connection_id="nango-test",
        account_email=user.email if email else None,
    )
    db_session.add(connection)
    await db_session.flush()
    watch = GmailWatchState(integration_connection_id=connection.id, status=state, history_id="10")
    db_session.add(watch)
    await db_session.commit()
    return connection, watch


def _configure_watch(monkeypatch):
    monkeypatch.setattr(settings, "NANGO_ENV_KEY", "server-secret")
    monkeypatch.setattr(settings, "NANGO_BASE_URL", "https://nango.example")
    monkeypatch.setattr(settings, "GMAIL_PUBSUB_TOPIC", "projects/p/topics/t")


@pytest.mark.asyncio
async def test_source_policy_requires_auth_and_explicit_opt_in(client, auth_headers):
    headers, _user_id = auth_headers
    assert (await client.get("/api/v1/source-policies/gmail")).status_code == 401
    absent = await client.get("/api/v1/source-policies/gmail", headers=headers)
    assert absent.status_code == 200
    assert absent.json()["monitor_enabled"] is False

    missing_flag = await client.put("/api/v1/source-policies/gmail", headers=headers, json={"mode": "ALL"})
    assert missing_flag.status_code == 422
    unknown = await client.get("/api/v1/source-policies/nango", headers=headers)
    assert unknown.status_code == 422


@pytest.mark.asyncio
async def test_source_policy_put_validates_gmail_emails_and_extra_fields(client, auth_headers):
    headers, _user_id = auth_headers
    invalid_email = await client.put("/api/v1/source-policies/gmail", headers=headers, json={
        "monitor_enabled": True, "mode": "ALLOWLIST", "selected_ids": ["thread-123"],
    })
    assert invalid_email.status_code == 422
    extra = await client.put("/api/v1/source-policies/whatsapp", headers=headers, json={
        "monitor_enabled": True, "selected_ids": [], "workspace_id": str(uuid.uuid4()),
    })
    assert extra.status_code == 422
    too_many = await client.put("/api/v1/source-policies/whatsapp", headers=headers, json={
        "monitor_enabled": True, "selected_ids": [f"chat-{i}" for i in range(201)],
    })
    assert too_many.status_code == 422


@pytest.mark.asyncio
async def test_policy_is_user_scoped_put_get_and_delete_deactivates(client, auth_headers, db_session):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    assert user is not None and user.workspace_id is not None
    other = User(name="Other", email=f"{uuid.uuid4()}@example.com", hashed_password="unused")
    db_session.add(other)
    await db_session.flush()
    db_session.add(SourceMonitoringPolicy(
        workspace_id=user.workspace_id, user_id=other.id, source="gmail", mode="ALL",
        selected_ids=[], gmail_label_ids=["INBOX"], gmail_allowed_senders=[],
    ))
    await db_session.commit()

    own = await client.put("/api/v1/source-policies/gmail", headers=headers, json={
        "monitor_enabled": True,
        "mode": "ALLOWLIST",
        "selected_ids": ["Alice@Example.com"],
        "gmail_label_ids": ["INBOX", "Label_1"],
        "gmail_allowed_senders": ["extra@example.com"],
    })
    assert own.status_code == 200, own.text
    assert own.json()["monitor_enabled"] is True
    assert own.json()["selected_ids"] == ["alice@example.com"]
    assert own.json()["gmail_label_ids"] == ["INBOX", "Label_1"]

    fetched = await client.get("/api/v1/source-policies/gmail", headers=headers)
    assert fetched.status_code == 200
    assert fetched.json()["selected_ids"] == ["alice@example.com"]

    disabled = await client.delete("/api/v1/source-policies/gmail", headers=headers)
    assert disabled.status_code == 200
    assert disabled.json()["monitor_enabled"] is False
    own_rows = (await db_session.exec(select(SourceMonitoringPolicy).where(
        SourceMonitoringPolicy.user_id == user.id,
        SourceMonitoringPolicy.source == "gmail",
    ))).all()
    other_rows = (await db_session.exec(select(SourceMonitoringPolicy).where(
        SourceMonitoringPolicy.user_id == other.id,
        SourceMonitoringPolicy.source == "gmail",
    ))).all()
    assert own_rows == []
    assert len(other_rows) == 1


@pytest.mark.asyncio
async def test_put_false_removes_policy_and_workspace_is_derived_from_current_user(client, auth_headers):
    headers, _user_id = auth_headers
    created = await client.put("/api/v1/source-policies/whatsapp", headers=headers, json={
        "monitor_enabled": True, "mode": "ALLOWLIST", "selected_ids": ["chat-1"],
    })
    assert created.status_code == 200
    disabled = await client.put("/api/v1/source-policies/whatsapp", headers=headers, json={
        "monitor_enabled": False, "workspace_id": str(uuid.uuid4()),
    })
    assert disabled.status_code == 422  # payload cannot choose another workspace
    disabled = await client.put("/api/v1/source-policies/whatsapp", headers=headers, json={
        "monitor_enabled": False,
    })
    assert disabled.status_code == 200
    assert disabled.json()["monitor_enabled"] is False


@pytest.mark.asyncio
async def test_source_policy_rejects_user_without_workspace_membership(client, auth_headers, db_session):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    assert user is not None and user.workspace_id is not None
    membership = (await db_session.exec(select(WorkspaceMembership).where(
        WorkspaceMembership.user_id == user.id,
        WorkspaceMembership.workspace_id == user.workspace_id,
    ))).one()
    await db_session.delete(membership)
    await db_session.commit()
    response = await client.get("/api/v1/source-policies/gmail", headers=headers)
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_gmail_opt_in_immediately_starts_watch_with_server_config(client, auth_headers, db_session, monkeypatch):
    headers, user_id = auth_headers
    _, watch = await _active_gmail(db_session, user_id)
    _configure_watch(monkeypatch)
    nango = _WatchNango()
    monkeypatch.setattr(source_policies, "NangoClient", lambda **kwargs: nango)
    response = await client.put("/api/v1/source-policies/gmail", headers=headers, json={
        "monitor_enabled": True, "mode": "ALL",
    })
    assert response.status_code == 200
    assert response.json()["monitor_enabled"] is True
    assert len(nango.calls) == 1
    assert nango.calls[0][2]["json"]["labelIds"] == ["INBOX"]
    await db_session.refresh(watch)
    assert watch.status == "active"


@pytest.mark.asyncio
async def test_gmail_opt_in_is_retained_when_config_missing_or_watch_fails(
    client, auth_headers, db_session, monkeypatch,
):
    headers, user_id = auth_headers
    _, watch = await _active_gmail(db_session, user_id)
    nango = _WatchNango(fail=True)
    monkeypatch.setattr(source_policies, "NangoClient", lambda **kwargs: nango)
    monkeypatch.setattr(settings, "NANGO_ENV_KEY", "")
    missing = await client.put("/api/v1/source-policies/gmail", headers=headers, json={"monitor_enabled": True})
    assert missing.status_code == 200
    assert nango.calls == []

    _configure_watch(monkeypatch)
    failed = await client.put("/api/v1/source-policies/gmail", headers=headers, json={"monitor_enabled": True})
    assert failed.status_code == 200
    assert len(nango.calls) == 1
    policy = (await db_session.exec(select(SourceMonitoringPolicy).where(
        SourceMonitoringPolicy.user_id == uuid.UUID(user_id), SourceMonitoringPolicy.source == "gmail",
    ))).one()
    assert policy is not None
    await db_session.refresh(watch)
    assert watch.status == "paused"


@pytest.mark.asyncio
async def test_gmail_opt_in_without_known_account_and_needs_resync_never_starts_watch(
    client, auth_headers, db_session, monkeypatch,
):
    headers, user_id = auth_headers
    _configure_watch(monkeypatch)
    nango = _WatchNango()
    monkeypatch.setattr(source_policies, "NangoClient", lambda **kwargs: nango)
    _, watch = await _active_gmail(db_session, user_id, state="needs_resync")
    response = await client.put("/api/v1/source-policies/gmail", headers=headers, json={"monitor_enabled": True})
    assert response.status_code == 200
    assert nango.calls == []
    await db_session.refresh(watch)
    assert watch.status == "needs_resync"


@pytest.mark.asyncio
async def test_gmail_opt_in_without_connection_keeps_policy_without_nango_call(
    client, auth_headers, monkeypatch,
):
    headers, _user_id = auth_headers
    _configure_watch(monkeypatch)
    nango = _WatchNango()
    monkeypatch.setattr(source_policies, "NangoClient", lambda **kwargs: nango)
    response = await client.put("/api/v1/source-policies/gmail", headers=headers, json={"monitor_enabled": True})
    assert response.status_code == 200
    assert response.json()["monitor_enabled"] is True
    assert nango.calls == []


@pytest.mark.asyncio
async def test_gmail_policy_delete_pauses_local_watch(client, auth_headers, db_session):
    headers, user_id = auth_headers
    _, watch = await _active_gmail(db_session, user_id, state="active")
    db_session.add(SourceMonitoringPolicy(
        workspace_id=(await db_session.get(User, uuid.UUID(user_id))).workspace_id,
        user_id=uuid.UUID(user_id), source="gmail", mode="ALL",
    ))
    await db_session.commit()
    response = await client.delete("/api/v1/source-policies/gmail", headers=headers)
    assert response.status_code == 200
    await db_session.refresh(watch)
    assert watch.status == "paused"


@pytest.mark.asyncio
async def test_whatsapp_opt_out_purges_owned_state_and_cancels_followups(
    client, auth_headers, db_session,
):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    assert user is not None and user.workspace_id is not None
    other = User(name="Other WA owner", email=f"{uuid.uuid4()}@example.com", hashed_password="unused",
                 workspace_id=user.workspace_id)
    db_session.add(other)
    await db_session.flush()
    now = datetime.now(timezone.utc)
    instances = [WhatsAppInstance(
        workspace_id=user.workspace_id, user_id=owner.id, go_instance_name=f"instance-{owner.id}",
        token_ciphertext="encrypted", webhook_secret_hash="hash",
    ) for owner in (user, other)]
    db_session.add_all(instances)
    await db_session.flush()
    events = [NormalizedEvent(
        workspace_id=user.workspace_id, user_id=owner.id, source="whatsapp", type="message",
        external_id=f"message-{owner.id}", conversation_id="chat-1", content="retained by policy",
        occurred_at=now,
    ) for owner in (user, other)]
    db_session.add_all(events)
    await db_session.flush()
    for owner, instance, event in zip((user, other), instances, events):
        db_session.add(ConversationState(
            workspace_id=user.workspace_id, user_id=owner.id, instance_id=instance.id,
            conversation_id="chat-1",
        ))
        db_session.add(PendingConversationItem(
            workspace_id=user.workspace_id, user_id=owner.id, instance_id=instance.id,
            conversation_id="chat-1", source_event=str(event.id), message_id="message-1",
            summary="minimal pending summary", asked_at=now, confidence=0.9,
        ))
        db_session.add(FollowUp(
            workspace_id=user.workspace_id, user_id=owner.id, source_event_id=event.id,
            idempotency_key=f"followup-{owner.id}", title="Follow up", status="waiting",
            response_status="pending",
        ))
    db_session.add(SourceMonitoringPolicy(
        workspace_id=user.workspace_id, user_id=user.id, source="whatsapp", mode="ALL",
    ))
    await db_session.commit()

    response = await client.put("/api/v1/source-policies/whatsapp", headers=headers, json={
        "monitor_enabled": False,
    })
    assert response.status_code == 200
    await client.delete("/api/v1/source-policies/whatsapp", headers=headers)  # idempotent disable

    assert not (await db_session.exec(select(ConversationState).where(
        ConversationState.user_id == user.id,
    ))).all()
    assert not (await db_session.exec(select(PendingConversationItem).where(
        PendingConversationItem.user_id == user.id,
    ))).all()
    own_followup = (await db_session.exec(select(FollowUp).where(FollowUp.user_id == user.id))).one()
    other_followup = (await db_session.exec(select(FollowUp).where(FollowUp.user_id == other.id))).one()
    assert (own_followup.status, own_followup.response_status) == ("cancelled", "dismissed")
    assert (other_followup.status, other_followup.response_status) == ("waiting", "pending")
    assert len((await db_session.exec(select(ConversationState).where(
        ConversationState.user_id == other.id,
    ))).all()) == 1
    assert len((await db_session.exec(select(PendingConversationItem).where(
        PendingConversationItem.user_id == other.id,
    ))).all()) == 1


@pytest.mark.asyncio
async def test_whatsapp_filter_change_purges_but_identical_and_gmail_edits_do_not(
    client, auth_headers, db_session,
):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    assert user is not None and user.workspace_id is not None
    instance = WhatsAppInstance(
        workspace_id=user.workspace_id, user_id=user.id, go_instance_name="filter-test",
        token_ciphertext="encrypted", webhook_secret_hash="hash",
    )
    db_session.add(instance)
    await db_session.flush()
    db_session.add(ConversationState(
        workspace_id=user.workspace_id, user_id=user.id, instance_id=instance.id,
        conversation_id="chat-1",
    ))
    await db_session.commit()
    base = {"monitor_enabled": True, "mode": "ALLOWLIST", "selected_ids": ["chat-1"],
            "direction": "incoming", "allow_groups": True, "allow_individuals": True}
    assert (await client.put("/api/v1/source-policies/whatsapp", headers=headers, json=base)).status_code == 200
    assert len((await db_session.exec(select(ConversationState))).all()) == 1
    assert (await client.put("/api/v1/source-policies/gmail", headers=headers, json={
        "monitor_enabled": True, "mode": "ALL",
    })).status_code == 200
    assert len((await db_session.exec(select(ConversationState))).all()) == 1
    changed = {**base, "direction": "both"}
    assert (await client.put("/api/v1/source-policies/whatsapp", headers=headers, json=changed)).status_code == 200
    assert (await db_session.exec(select(ConversationState))).all() == []
