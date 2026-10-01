"""End-to-end tests for the authenticated, policy-first WhatsApp webhook."""

import uuid

import pytest
from sqlmodel import select

from app.core.whatsapp_crypto import hash_webhook_secret
from app.models import ConversationState, PendingConversationItem, WhatsAppInstance
from app.models.action_item import ActionSuggestion, Reminder, Task
from app.models.normalized_event import NormalizedEvent
from app.models.source_policy import SourceMonitoringPolicy
from app.models.user import User
from app.schemas.intelligence import IntelligenceResult, IntelligenceType


def _payload(instance: str, *, from_me: bool = False, remote_jid: str = "5511999999999@s.whatsapp.net", message_id: str = "msg-1", content: str = "Você pode enviar o relatório?"):
    return {
        "event": "MESSAGE",
        "instance": instance,
        "data": {
            "key": {"remoteJid": remote_jid, "fromMe": from_me, "id": message_id},
            "message": {"conversation": content},
            "messageTimestamp": "1699999999",
        },
    }


async def _instance(auth_headers, db_session, *, direction="both", mode="ALL", selected_ids=None, allow_groups=True, allow_individuals=True):
    user = (await db_session.exec(select(User))).first()
    instance = WhatsAppInstance(
        workspace_id=user.workspace_id,
        user_id=user.id,
        go_instance_name=f"go-{uuid.uuid4().hex}",
        token_ciphertext="not-used",
        webhook_secret_hash=hash_webhook_secret("valid-webhook-secret"),
        status="connected",
    )
    db_session.add(instance)
    db_session.add(SourceMonitoringPolicy(
        workspace_id=user.workspace_id, user_id=user.id, source="whatsapp", direction=direction,
        mode=mode, selected_ids=selected_ids or [], allow_groups=allow_groups,
        allow_individuals=allow_individuals,
    ))
    await db_session.commit()
    instance._webhook_id = instance.id
    instance._go_name = instance.go_instance_name
    return instance


async def _post(client, instance, payload, secret="valid-webhook-secret"):
    return await client.post(
        f"/api/v1/whatsapp/webhook/{instance._webhook_id}/{secret}", json=payload,
    )


@pytest.mark.asyncio
async def test_invalid_secret_is_rejected_before_invalid_body_is_parsed(client, auth_headers, db_session):
    instance = await _instance(auth_headers, db_session)
    response = await client.post(
        f"/api/v1/whatsapp/webhook/{instance.id}/wrong", content=b"not-json",
    )
    assert response.status_code == 404
    assert (await db_session.exec(select(NormalizedEvent))).all() == []


@pytest.mark.asyncio
async def test_blocked_conversation_group_and_direction_never_classify(client, auth_headers, db_session, monkeypatch):
    instance = await _instance(
        auth_headers, db_session, direction="incoming", mode="BLOCKLIST",
        selected_ids=["5511999999999@s.whatsapp.net"], allow_groups=False,
    )
    calls = []

    async def classify(*args, **kwargs):
        calls.append(args)
        return []

    monkeypatch.setattr("app.api.v1.whatsapp.classify_event", classify)
    cases = [
        _payload(instance._go_name),
        _payload(instance._go_name, remote_jid="120363012345678901@g.us"),
        _payload(instance._go_name, from_me=True, message_id="outgoing"),
    ]
    for payload in cases:
        assert (await _post(client, instance, payload)).status_code == 204
    assert calls == []
    assert (await db_session.exec(select(NormalizedEvent))).all() == []
    assert (await db_session.exec(select(ConversationState))).all() == []


@pytest.mark.asyncio
async def test_incoming_question_and_duplicate_process_once(client, auth_headers, db_session, monkeypatch):
    instance = await _instance(auth_headers, db_session)
    calls = []
    result = IntelligenceResult(
        type=IntelligenceType.FOLLOW_UP, title="Pergunta", summary="Enviar o relatório",
        confidence=0.95,
    )

    async def classify(*args, **kwargs):
        calls.append((args, kwargs))
        return [result]

    monkeypatch.setattr("app.api.v1.whatsapp.classify_event", classify)
    payload = _payload(instance._go_name)
    assert (await _post(client, instance, payload)).status_code == 204
    assert (await _post(client, instance, payload)).status_code == 204
    pending = (await db_session.exec(select(PendingConversationItem))).all()
    assert len(pending) == 1
    assert pending[0].status == "WAITING_FOR_RESPONSE"
    assert len(calls) == 1
    events = (await db_session.exec(select(NormalizedEvent))).all()
    assert len(events) == 1 and events[0].status == "processed"


@pytest.mark.asyncio
async def test_outgoing_answer_resolves_pending_question(client, auth_headers, db_session, monkeypatch):
    instance = await _instance(auth_headers, db_session)
    follow_up = IntelligenceResult(
        type=IntelligenceType.FOLLOW_UP, title="Pergunta", summary="Enviar o relatório",
        confidence=0.95,
    )

    async def classify(content, **kwargs):
        return [follow_up] if "relatório?" in content else []

    async def confirmed(pending, outbound):
        return {item.id for item in pending} if "enviei" in outbound.lower() else set()

    monkeypatch.setattr("app.api.v1.whatsapp.classify_event", classify)
    monkeypatch.setattr("app.services.conversation_intelligence._confirmed_answers", confirmed)
    assert (await _post(client, instance, _payload(instance.go_instance_name))).status_code == 204
    answer = _payload(
        instance._go_name, from_me=True, message_id="answer-1",
        content="Enviei o relatório agora.",
    )
    assert (await _post(client, instance, answer)).status_code == 204
    pending = (await db_session.exec(select(PendingConversationItem))).all()
    assert len(pending) == 1 and pending[0].status == "RESOLVED"


@pytest.mark.asyncio
async def test_sender_allowlist_wrong_go_instance_and_media_are_ignored(client, auth_headers, db_session, monkeypatch):
    instance = await _instance(
        auth_headers, db_session, mode="ALLOWLIST", selected_ids=["allowed@s.whatsapp.net"],
    )
    calls = []

    async def classify(*args, **kwargs):
        calls.append(args)
        return []

    monkeypatch.setattr("app.api.v1.whatsapp.classify_event", classify)
    assert (await _post(client, instance, _payload("another-go"))).status_code == 204
    media = _payload(instance._go_name, message_id="media")
    media["data"]["message"] = {"imageMessage": {"caption": "ignore me"}}
    assert (await _post(client, instance, media)).status_code == 204
    assert (await _post(client, instance, _payload(instance._go_name))).status_code == 204
    assert calls == []
    assert (await db_session.exec(select(NormalizedEvent))).all() == []


@pytest.mark.asyncio
async def test_webhook_does_not_log_payload(client, auth_headers, db_session, monkeypatch, caplog):
    instance = await _instance(auth_headers, db_session)
    private = "whatsapp-body-private-value"

    async def classify(*args, **kwargs):
        return []

    monkeypatch.setattr("app.api.v1.whatsapp.classify_event", classify)
    response = await _post(client, instance, _payload(instance._go_name, content=private))
    assert response.status_code == 204
    assert private not in caplog.text


@pytest.mark.asyncio
async def test_policy_revoked_during_classification_rolls_back_all_webhook_rows(
    client, auth_headers, db_session, monkeypatch,
):
    instance = await _instance(auth_headers, db_session)
    classify_calls = []

    async def classify(*args, **kwargs):
        classify_calls.append(args)
        return [IntelligenceResult(
            type=IntelligenceType.TASK, title="Sensitive task", summary="Derived suggestion",
            confidence=0.99,
        )]

    policy_checks = iter((True, False))

    async def policy_revoked_after_materialization(*args, **kwargs):
        return next(policy_checks)

    monkeypatch.setattr("app.api.v1.whatsapp.classify_event", classify)
    monkeypatch.setattr(
        "app.api.v1.whatsapp.whatsapp_policy_allows", policy_revoked_after_materialization,
    )
    # SQLite's SAVEPOINT-based flush otherwise becomes the outermost physical
    # transaction; explicitly begin it so rollback mirrors PostgreSQL behavior.
    connection = await db_session.connection()
    await connection.exec_driver_sql("BEGIN")
    response = await _post(client, instance, _payload(instance._go_name))

    assert response.status_code == 204
    assert len(classify_calls) == 1
    assert (await db_session.exec(select(NormalizedEvent))).all() == []
    assert (await db_session.exec(select(Task))).all() == []
    assert (await db_session.exec(select(Reminder))).all() == []
    assert (await db_session.exec(select(ActionSuggestion))).all() == []
    assert (await db_session.exec(select(PendingConversationItem))).all() == []
    assert (await db_session.exec(select(ConversationState))).all() == []
