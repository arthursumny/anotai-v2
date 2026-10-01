import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

from app.models.action_item import FollowUp
from app.models.conversation_state import ConversationState
from app.models.normalized_event import NormalizedEvent
from app.models.pending_conversation_item import PendingConversationItem
from app.models.source_policy import SourceMonitoringPolicy
from app.models.user import User
from app.models.whatsapp_instance import WhatsAppInstance
from app.models.workspace import Workspace, WorkspaceMembership
from app.models.workflow import Workflow, WorkflowExecution, WorkflowStep
from app.schemas.intelligence import IntelligenceResult, IntelligenceType
from app.services import conversation_intelligence as service
from app.services.reminder_scheduler import process_followup_due


def _result(kind=IntelligenceType.FOLLOW_UP, summary="Please send the signed proposal"):
    return IntelligenceResult(type=kind, title="Proposal", summary=summary, confidence=0.95)


async def _setup(session, direction="both"):
    workspace = Workspace(name="workspace")
    user = User(name="Owner", email=f"{uuid.uuid4()}@example.test", hashed_password="unused")
    session.add_all([workspace, user])
    await session.flush()
    user.workspace_id = workspace.id
    instance = WhatsAppInstance(
        workspace_id=workspace.id, user_id=user.id, go_instance_name=f"instance-{uuid.uuid4()}",
        token_ciphertext="cipher", webhook_secret_hash="hash",
    )
    session.add_all([
        instance,
        WorkspaceMembership(workspace_id=workspace.id, user_id=user.id),
        SourceMonitoringPolicy(workspace_id=workspace.id, user_id=user.id, source="whatsapp", direction=direction),
    ])
    await session.flush()
    return workspace, user, instance


async def _event(session, workspace, user, *, outgoing=False, at=None, content="Sure, I will send it"):
    event = NormalizedEvent(
        workspace_id=workspace.id, user_id=user.id, source="whatsapp", type="MESSAGE",
        external_id=f"msg-{uuid.uuid4()}", conversation_id="123@s.whatsapp.net",
        content=content, occurred_at=at or datetime.now(timezone.utc),
        metadata_json={"direction": "outgoing" if outgoing else "incoming"}, status="accepted",
    )
    session.add(event)
    await session.flush()
    return event


@pytest.mark.asyncio
async def test_incoming_question_creates_pending_only_when_both(db_session):
    workspace, user, instance = await _setup(db_session)
    event = await _event(db_session, workspace, user, content="Can you send the proposal?")
    await service.process_authorized_conversation_event(db_session, event, instance, [_result()])
    items = (await db_session.exec(select(PendingConversationItem))).all()
    assert len(items) == 1
    assert items[0].source_event == str(event.id) and items[0].message_id == event.external_id
    assert items[0].status == "WAITING_FOR_RESPONSE"
    assert items[0].remind_at.replace(tzinfo=timezone.utc) == items[0].asked_at.replace(tzinfo=timezone.utc) + timedelta(hours=24)


@pytest.mark.asyncio
async def test_incoming_unrelated_does_not_resolve_and_one_direction_is_minimal(db_session):
    workspace, user, instance = await _setup(db_session)
    question = await _event(db_session, workspace, user, content="Can you send the proposal?")
    await service.process_authorized_conversation_event(db_session, question, instance, [_result()])
    another = await _event(db_session, workspace, user, content="Thanks")
    await service.process_authorized_conversation_event(db_session, another, instance, [_result(IntelligenceType.INFORMATION)])
    assert len((await db_session.exec(select(PendingConversationItem))).all()) == 1

    workspace2, user2, instance2 = await _setup(db_session, direction="incoming")
    excluded = await _event(db_session, workspace2, user2)
    await service.process_authorized_conversation_event(db_session, excluded, instance2, [_result()])
    assert len((await db_session.exec(select(PendingConversationItem))).all()) == 1


@pytest.mark.asyncio
async def test_outgoing_semantic_answer_resolves_and_cancels_due_followup(db_session, monkeypatch):
    workspace, user, instance = await _setup(db_session)
    question = await _event(db_session, workspace, user, outgoing=False, content="Send the proposal")
    await service.process_authorized_conversation_event(db_session, question, instance, [_result()])
    pending = (await db_session.exec(select(PendingConversationItem))).one()
    followup = FollowUp(
        workspace_id=workspace.id, user_id=user.id, idempotency_key=f"conversation_pending:{pending.id}",
        title=pending.summary, source_event_id=question.id, status="open", response_status="pending",
    )
    db_session.add(followup)
    await db_session.flush()
    monkeypatch.setattr(service, "settings", SimpleNamespace(
        OPENAI_API_KEY="key", OPENAI_ROUTER_MODEL="test", ROUTER_CONFIDENCE_THRESHOLD=0.6,
        FOLLOW_UP_DELAY_HOURS=24,
    ))
    monkeypatch.setattr(service, "AsyncOpenAI", lambda **kwargs: object())
    llm = AsyncMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=f'{{"resolved_ids":["{pending.id}"],"confidence":0.98}}'))]
    ))
    monkeypatch.setattr(service, "chat_create", llm)
    answer = await _event(db_session, workspace, user, outgoing=True, content="Here is the signed proposal")
    await service.process_authorized_conversation_event(db_session, answer, instance)
    assert pending.status == "RESOLVED"
    assert followup.status == "resolved" and followup.response_status == "answered"
    assert answer.status == "processed"
    await service.process_authorized_conversation_event(db_session, answer, instance)
    assert llm.await_count == 1


@pytest.mark.asyncio
async def test_outgoing_unrelated_no_key_and_duplicate_do_not_call_llm(db_session, monkeypatch):
    workspace, user, instance = await _setup(db_session)
    question = await _event(db_session, workspace, user)
    await service.process_authorized_conversation_event(db_session, question, instance, [_result()])
    outgoing = await _event(db_session, workspace, user, outgoing=True, content="Good morning")
    fake = AsyncMock()
    monkeypatch.setattr(service, "settings", SimpleNamespace(
        OPENAI_API_KEY="", OPENAI_ROUTER_MODEL="test", ROUTER_CONFIDENCE_THRESHOLD=0.6,
    ))
    monkeypatch.setattr(service, "chat_create", fake)
    await service.process_authorized_conversation_event(db_session, outgoing, instance)
    await service.process_authorized_conversation_event(db_session, outgoing, instance)
    assert fake.await_count == 0
    item = (await db_session.exec(select(PendingConversationItem))).one()
    assert item.status == "WAITING_FOR_RESPONSE"


@pytest.mark.asyncio
async def test_cross_tenant_and_instance_rejected(db_session):
    workspace, user, instance = await _setup(db_session)
    event = await _event(db_session, workspace, user)
    other_workspace, _, other_instance = await _setup(db_session)
    with pytest.raises(PermissionError):
        await service.process_authorized_conversation_event(db_session, event, other_instance, [_result()])
    # Same owner/workspace event cannot be processed through an unrelated instance.
    other_instance.workspace_id = other_workspace.id


@pytest.mark.asyncio
async def test_logs_do_not_include_message_content_on_llm_failure(db_session, monkeypatch, caplog):
    workspace, user, instance = await _setup(db_session)
    question = await _event(db_session, workspace, user, content="SECRET QUESTION BODY")
    await service.process_authorized_conversation_event(db_session, question, instance, [_result()])
    outgoing = await _event(db_session, workspace, user, outgoing=True, content="SECRET ANSWER BODY")
    monkeypatch.setattr(service, "settings", SimpleNamespace(
        OPENAI_API_KEY="key", OPENAI_ROUTER_MODEL="test", ROUTER_CONFIDENCE_THRESHOLD=0.6,
        FOLLOW_UP_DELAY_HOURS=24,
    ))
    monkeypatch.setattr(service, "AsyncOpenAI", lambda **kwargs: object())
    monkeypatch.setattr(service, "chat_create", AsyncMock(side_effect=RuntimeError("provider")))
    await service.process_authorized_conversation_event(db_session, outgoing, instance)
    assert "SECRET" not in caplog.text


async def _followup_due_workflow(session, workspace, user):
    workflow = Workflow(
        workspace_id=workspace.id, user_id=user.id, title="Followup workflow",
        trigger_type="FOLLOW_UP_DUE", conditions={"event_type": "FOLLOW_UP_DUE"}, enabled=True,
    )
    session.add(workflow)
    await session.flush()
    session.add(WorkflowStep(
        workflow_id=workflow.id, position=0, action_type="CREATE_TASK",
        input_template={"title": "Check in"}, enabled=True, require_confirmation=True,
    ))
    await session.flush()


@pytest.mark.asyncio
async def test_due_pending_creates_scheduler_waiting_row_then_scheduler_emits_due(db_session):
    workspace, user, instance = await _setup(db_session)
    await _followup_due_workflow(db_session, workspace, user)
    question = await _event(
        db_session, workspace, user, at=datetime.now(timezone.utc) - timedelta(hours=25),
        content="Please send the signed proposal",
    )
    await service.process_authorized_conversation_event(db_session, question, instance, [_result()])
    created = await service.materialize_due_conversation_followups(
        db_session, now=datetime.now(timezone.utc), batch_size=100,
    )
    assert created == 1
    item = (await db_session.exec(select(PendingConversationItem))).one()
    followup = (await db_session.exec(select(FollowUp))).one()
    assert item.status == "DUE"
    assert followup.status == "waiting" and followup.response_status == "pending"
    result = await process_followup_due(db_session, followup.id, followup.remind_at)
    assert result["status"] == "due"
    assert followup.status == "due"
    run = (await db_session.exec(select(WorkflowExecution))).one()
    assert run.trigger_type == "FOLLOW_UP_DUE"


@pytest.mark.asyncio
async def test_answer_before_cron_creates_no_followup_and_revoked_policy_suppresses_due(db_session, monkeypatch):
    workspace, user, instance = await _setup(db_session)
    question = await _event(
        db_session, workspace, user, at=datetime.now(timezone.utc) - timedelta(hours=25),
        content="Please send the signed proposal",
    )
    await service.process_authorized_conversation_event(db_session, question, instance, [_result()])
    pending = (await db_session.exec(select(PendingConversationItem))).one()
    monkeypatch.setattr(service, "settings", SimpleNamespace(
        OPENAI_API_KEY="key", OPENAI_ROUTER_MODEL="test", ROUTER_CONFIDENCE_THRESHOLD=0.6,
        FOLLOW_UP_DELAY_HOURS=24,
    ))
    monkeypatch.setattr(service, "AsyncOpenAI", lambda **kwargs: object())
    monkeypatch.setattr(service, "chat_create", AsyncMock(return_value=SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=f'{{"resolved_ids":["{pending.id}"],"confidence":0.99}}'))]
    )))
    answer = await _event(db_session, workspace, user, outgoing=True)
    await service.process_authorized_conversation_event(db_session, answer, instance)
    assert await service.materialize_due_conversation_followups(db_session) == 0
    assert (await db_session.exec(select(FollowUp))).all() == []

    # Another due question loses opt-in before the due helper runs.
    later = await _event(
        db_session, workspace, user, at=datetime.now(timezone.utc) - timedelta(hours=26),
        content="Please send the contract",
    )
    await service.process_authorized_conversation_event(db_session, later, instance, [_result()])
    policy = (await db_session.exec(select(SourceMonitoringPolicy))).one()
    await db_session.delete(policy)
    await db_session.flush()
    assert await service.materialize_due_conversation_followups(db_session) == 0
    later_pending = (await db_session.exec(select(PendingConversationItem).where(
        PendingConversationItem.source_event == str(later.id),
    ))).one()
    assert later_pending.status == "RESOLVED"
    assert (await db_session.exec(select(FollowUp))).all() == []


@pytest.mark.asyncio
async def test_due_batch_caps_size_and_one_invalid_item_does_not_block_next(db_session, caplog):
    workspace, user, instance = await _setup(db_session)
    old = datetime.now(timezone.utc) - timedelta(hours=30)
    malformed = PendingConversationItem(
        workspace_id=workspace.id, user_id=user.id, instance_id=instance.id,
        conversation_id="123@s.whatsapp.net", source_event="not-a-uuid", message_id="bad",
        summary="bad row", asked_at=old - timedelta(hours=24), remind_at=old,
        status="WAITING_FOR_RESPONSE", confidence=0.9,
    )
    event = await _event(db_session, workspace, user, at=old - timedelta(hours=24))
    valid = PendingConversationItem(
        workspace_id=workspace.id, user_id=user.id, instance_id=instance.id,
        conversation_id=event.conversation_id, source_event=str(event.id), message_id=event.external_id,
        summary="valid row", asked_at=old - timedelta(hours=24), remind_at=old,
        status="WAITING_FOR_RESPONSE", confidence=0.9,
    )
    db_session.add_all([malformed, valid])
    await db_session.flush()
    with pytest.raises(ValueError):
        await service.materialize_due_conversation_followups(db_session, batch_size=101)
    assert await service.materialize_due_conversation_followups(db_session, batch_size=2) == 1
    assert valid.status == "DUE"
    assert malformed.status == "WAITING_FOR_RESPONSE"
    assert "bad row" not in caplog.text
