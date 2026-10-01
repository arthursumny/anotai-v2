import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlmodel import select

from app.models.action_item import FollowUp
from app.models.normalized_event import NormalizedEvent
from app.models.pending_conversation_item import PendingConversationItem
from app.models.source_policy import SourceMonitoringPolicy
from app.models.user import User
from app.models.whatsapp_instance import WhatsAppInstance
from app.models.workflow import WorkflowExecution
from app.models.workspace import Workspace, WorkspaceMembership
from app.services.reminder_scheduler import process_followup_due
from app import worker


class _SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *args):
        return False


async def _pending(session, workspace, user, instance, conversation, status="WAITING_FOR_RESPONSE", remind_at=None):
    event = NormalizedEvent(
        workspace_id=workspace.id, user_id=user.id, source="whatsapp", type="MESSAGE",
        external_id=f"msg-{uuid.uuid4()}", conversation_id=conversation,
        content="not used by this scheduler job", occurred_at=datetime.now(timezone.utc) - timedelta(hours=26),
        metadata_json={"direction": "incoming", "is_group": False}, status="processed",
    )
    session.add(event)
    await session.flush()
    item = PendingConversationItem(
        workspace_id=workspace.id, user_id=user.id, instance_id=instance.id,
        conversation_id=conversation, source_event=str(event.id), message_id=event.external_id,
        summary=f"private summary {conversation}",
        asked_at=datetime.now(timezone.utc) - timedelta(hours=26),
        remind_at=remind_at or datetime.now(timezone.utc) - timedelta(hours=2),
        status=status, confidence=0.9,
    )
    session.add(item)
    await session.flush()
    return item


async def _setup(session):
    workspace = Workspace(name="Conversation cron")
    session.add(workspace)
    await session.flush()
    user = User(
        name="Owner", email=f"{uuid.uuid4()}@example.test", hashed_password="unused",
        workspace_id=workspace.id,
    )
    instance = WhatsAppInstance(
        workspace_id=workspace.id, user_id=user.id, go_instance_name=f"wa-{uuid.uuid4()}",
        token_ciphertext="cipher", webhook_secret_hash="hash", status="connected",
    )
    policy = SourceMonitoringPolicy(
        workspace_id=workspace.id, user_id=user.id, source="whatsapp", direction="both",
        mode="ALLOWLIST", selected_ids=["due@s.whatsapp.net"],
    )
    session.add_all([user, instance, policy, WorkspaceMembership(workspace_id=workspace.id, user_id=user.id)])
    await session.flush()
    return workspace, user, instance


@pytest.mark.asyncio
async def test_job_only_materializes_due_allowed_pending_and_is_idempotent(db_session, monkeypatch):
    workspace, user, instance = await _setup(db_session)
    answered = await _pending(
        db_session, workspace, user, instance, "answered@s.whatsapp.net", status="RESOLVED",
    )
    blocked = await _pending(db_session, workspace, user, instance, "blocked@s.whatsapp.net")
    due = await _pending(db_session, workspace, user, instance, "due@s.whatsapp.net")
    monkeypatch.setattr(worker, "async_session_factory", lambda: _SessionContext(db_session))

    async def forbidden(*args, **kwargs):
        raise AssertionError("scheduler job must not invoke LLM or Telegram")

    monkeypatch.setattr(worker, "_send_reminder_telegram", forbidden)
    from app.services import conversation_intelligence
    monkeypatch.setattr(conversation_intelligence, "_confirmed_answers", forbidden)

    first = await worker.check_pending_conversation_items_job({})
    second = await worker.check_pending_conversation_items_job({})
    assert first == {"ok": True, "created": 1}
    assert second == {"ok": True, "created": 0}
    rows = (await db_session.exec(select(FollowUp))).all()
    assert len(rows) == 1
    followup = rows[0]
    assert followup.status == "waiting" and followup.response_status == "pending"
    assert answered.status == "RESOLVED"
    assert blocked.status == "RESOLVED"
    assert due.status == "DUE"

    # Existing authoritative scheduler consumes the waiting row, not a premature due row.
    result = await process_followup_due(db_session, followup.id, followup.remind_at)
    assert result["status"] == "due"
    assert followup.status == "due"
    assert (await db_session.exec(select(WorkflowExecution))).all() == []


@pytest.mark.asyncio
async def test_job_failure_rolls_back_and_never_raises_or_logs_content(db_session, monkeypatch, caplog):
    monkeypatch.setattr(worker, "async_session_factory", lambda: _SessionContext(db_session))

    async def broken(*args, **kwargs):
        raise RuntimeError("private summary and token")

    monkeypatch.setattr(worker, "materialize_due_conversation_followups", broken)
    result = await worker.check_pending_conversation_items_job({})
    assert result == {"ok": False, "error": "processing_failed"}
    assert "private summary" not in caplog.text and "token" not in caplog.text


def test_worker_registers_unique_five_minute_pending_conversation_cron():
    assert worker.check_pending_conversation_items_job in worker.WorkerSettings.functions
    jobs = [job for job in worker.WorkerSettings.cron_jobs
            if job.coroutine is worker.check_pending_conversation_items_job]
    assert len(jobs) == 1
    assert jobs[0].unique is True
    assert jobs[0].minute == {0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55}
