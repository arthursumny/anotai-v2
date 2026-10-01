import uuid
from datetime import datetime, timedelta, timezone

import pytest
from app import worker
from app.models.action_execution import ActionExecution
from app.models.audit_event import AuditEvent
from app.models.normalized_event import NormalizedEvent
from app.models.note import Note
from app.models.user import User
from app.models.workspace import Workspace


async def _owner(session):
    workspace = Workspace(name="privacy job")
    user = User(
        name="privacy job", email=f"{uuid.uuid4()}@example.test", hashed_password="unused",
        workspace_id=None,
    )
    session.add_all([workspace, user])
    await session.flush()
    user.workspace_id = workspace.id
    return workspace, user


def _event(workspace, user, created_at):
    return NormalizedEvent(
        workspace_id=workspace.id, user_id=user.id, source="audio", type="capture",
        content="private event", occurred_at=created_at, created_at=created_at,
    )


@pytest.mark.asyncio
async def test_privacy_job_purges_expired_events_preserves_active_and_is_idempotent(worker_db):
    now = datetime.now(timezone.utc)
    workspace, user = await _owner(worker_db)
    expired = _event(workspace, user, now - timedelta(days=31))
    active = _event(workspace, user, now - timedelta(days=2))
    note = Note(user_id=user.id, raw_transcription="must stay")
    worker_db.add_all([expired, active, note])
    await worker_db.commit()

    result = await worker.privacy_retention_job({})
    assert result["ok"] is True
    assert await worker_db.get(NormalizedEvent, expired.id) is None
    assert await worker_db.get(NormalizedEvent, active.id) is not None
    assert (await worker_db.get(Note, note.id)).raw_transcription == "must stay"
    assert (await worker.privacy_retention_job({}))["events"] == 0


@pytest.mark.asyncio
async def test_privacy_job_bounds_event_batches_and_does_not_claim_completion(worker_db, monkeypatch):
    calls = []

    async def full_batch(session, *, now, batch_size):
        calls.append(batch_size)
        return {"events": batch_size, "failed": 0}

    monkeypatch.setattr(worker, "purge_expired_events", full_batch)
    result = await worker.privacy_retention_job({})
    assert calls == [100] * 10
    assert result["events"] == 1000
    assert result["more_events_may_remain"] is True
    assert result["ok"] is False


@pytest.mark.asyncio
async def test_privacy_job_audit_cutoff(worker_db):
    now = datetime.now(timezone.utc)
    workspace, user = await _owner(worker_db)
    old = AuditEvent(
        workspace_id=workspace.id, user_id=user.id, actor="system", event="retention",
        action="cleanup", status="success", source="system", target_kind="audit",
        metadata_json={"count": 1}, created_at=now - timedelta(days=91),
    )
    current = AuditEvent(
        workspace_id=workspace.id, user_id=user.id, actor="system", event="retention",
        action="cleanup", status="success", source="system", target_kind="audit",
        metadata_json={}, created_at=now - timedelta(days=89),
    )
    worker_db.add_all([old, current])
    await worker_db.commit()

    result = await worker.privacy_retention_job({})
    assert result["audit_events"] == 1
    assert await worker_db.get(AuditEvent, old.id) is None
    assert await worker_db.get(AuditEvent, current.id) is not None


@pytest.mark.asyncio
async def test_action_job_commits_helper_and_failure_logs_no_payload(worker_db, monkeypatch, caplog):
    async def scrub(session, *, now, batch_size):
        assert batch_size == 100
        return 3

    monkeypatch.setattr(worker, "cleanup_action_executions", scrub)
    assert await worker.action_hygiene_job({}) == {"ok": True, "changed": 3}

    async def fail(*args, **kwargs):
        raise RuntimeError("secret action_input value")

    monkeypatch.setattr(worker, "cleanup_action_executions", fail)
    result = await worker.action_hygiene_job({})
    assert result == {"ok": False, "error": "processing_failed"}
    assert "secret action_input value" not in caplog.text


@pytest.mark.asyncio
async def test_action_job_scrubs_old_success_input_and_fails_stale_awaiting(worker_db):
    now = datetime.now(timezone.utc)
    workspace, user = await _owner(worker_db)
    common = dict(
        workspace_id=workspace.id, user_id=user.id, trigger_type="event", action_type="notify",
        idempotency_key=str(uuid.uuid4()), policy_snapshot={}, updated_at=now - timedelta(days=8),
    )
    succeeded = ActionExecution(
        **common, status="succeeded", action_input={"private": "payload"},
    )
    awaiting = ActionExecution(
        **{**common, "idempotency_key": str(uuid.uuid4())},
        status="awaiting_confirmation", action_input={"private": "pending"},
    )
    worker_db.add_all([succeeded, awaiting])
    await worker_db.commit()

    result = await worker.action_hygiene_job({})
    assert result == {"ok": True, "changed": 2}
    assert succeeded.action_input is None
    assert awaiting.status == "failed"
    assert awaiting.last_error == "stale_workflow"


@pytest.mark.asyncio
async def test_privacy_job_service_failure_is_non_throwing_and_redacted(worker_db, monkeypatch, caplog):
    async def fail(*args, **kwargs):
        raise RuntimeError("private event content")

    monkeypatch.setattr(worker, "purge_expired_events", fail)
    result = await worker.privacy_retention_job({})
    assert result["ok"] is False
    assert "private event content" not in caplog.text


def test_privacy_cron_registry_keeps_existing_jobs_and_registers_unique_daily_jobs():
    jobs = worker.WorkerSettings.cron_jobs
    by_name = {job.coroutine.__name__: job for job in jobs}
    assert {"privacy_retention_job", "action_hygiene_job"} <= by_name.keys()
    assert by_name["privacy_retention_job"].unique is True
    assert by_name["action_hygiene_job"].unique is True
    assert by_name["privacy_retention_job"].hour == 4
    assert by_name["privacy_retention_job"].minute == 0
    assert by_name["action_hygiene_job"].hour == 4
    assert by_name["action_hygiene_job"].minute == 15
    assert {
        "check_scheduled_reminders", "reconcile_due_items", "renew_gmail_watches_job",
        "reconcile_gmail_histories_job",
    } <= by_name.keys()
