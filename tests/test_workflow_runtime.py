"""Runtime integration of generated documents, workflow preparation, and recovery."""

import uuid
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

import app.services.document_pipeline as document_pipeline
import app.services.document_recovery as document_recovery
import app.worker as worker
from app.models.action_execution import ActionExecution
from app.models.automation_settings import AutomationMode, AutomationSettings
from app.models.generated_document import GeneratedDocument
from app.models.normalized_event import NormalizedEvent
from app.models.note import Note, NoteStatus
from app.models.user import User
from app.models.workflow import Workflow, WorkflowExecution, WorkflowStep
from app.models.workspace import Workspace, WorkspaceMembership


async def _runtime_identity(session, *, mode: AutomationMode | None = None):
    workspace = Workspace(name="Runtime workspace", timezone="UTC", locale="en-US")
    user = User(
        name="Runtime user",
        email=f"runtime-{uuid.uuid4().hex}@example.test",
        hashed_password="not-a-real-password",
        workspace_id=workspace.id,
    )
    session.add(workspace)
    session.add(user)
    await session.flush()
    session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="OWNER"))
    if mode is not None:
        session.add(AutomationSettings(
            workspace_id=workspace.id,
            user_id=user.id,
            automation_mode=mode,
            action_config={"CREATE_TASK": "automatic"},
        ))
    workflow = Workflow(
        workspace_id=workspace.id,
        user_id=user.id,
        title="Generated document workflow",
        trigger_type="DOCUMENT_GENERATED",
        conditions={"source": "audio", "event_type": "DOCUMENT_GENERATED"},
        enabled=True,
    )
    session.add(workflow)
    await session.flush()
    session.add(WorkflowStep(
        workflow_id=workflow.id,
        position=0,
        action_type="CREATE_TASK",
        input_template={"title": "Review generated note", "summary": ""},
        require_confirmation=False,
        enabled=True,
    ))
    note = Note(
        user_id=user.id,
        raw_transcription="",
        structured_data=None,
        status=NoteStatus.PENDING_REVIEW,
    )
    session.add(note)
    await session.commit()
    await session.refresh(note)
    return workspace, user, workflow, note


def _install_worker_stt(monkeypatch, transcription: str):
    class FakeSTT:
        async def transcribe(self, _path):
            return transcription

    monkeypatch.setattr(worker, "get_stt_provider", lambda: FakeSTT())
    monkeypatch.setattr(worker, "check_audible", lambda _path: (True, "ok"))


async def _general_result(_transcription, _context):
    return {
        "title": "Runtime note",
        "body": "A safe summary",
        "tasks": [],
        "due_date": None,
        "reminder_at": None,
        "send_to_kanban": False,
    }


@pytest.mark.parametrize(
    "mode,expected_status",
    [("suggestive", "awaiting_confirmation"), ("automatic", "pending")],
)
async def test_audio_document_trigger_prepares_idempotent_workflow_action(
    worker_db, tmp_path, monkeypatch, mode, expected_status
):
    workspace, user, workflow, note = await _runtime_identity(worker_db, mode=mode)
    _install_worker_stt(monkeypatch, "audio content")
    monkeypatch.setattr(worker, "generate_general_note", _general_result)
    classify = AsyncMock(return_value=[])
    monkeypatch.setattr(document_pipeline, "classify_event", classify)

    first = await worker.process_audio_job({}, str(note.id), str(tmp_path / "audio.wav"), str(user.id))
    second = await worker.process_audio_job({}, str(note.id), str(tmp_path / "audio.wav"), str(user.id))

    assert first["status"] == second["status"] == "general"
    events = list((await worker_db.exec(select(NormalizedEvent))).all())
    documents = list((await worker_db.exec(select(GeneratedDocument))).all())
    executions = list((await worker_db.exec(select(WorkflowExecution))).all())
    actions = list((await worker_db.exec(select(ActionExecution))).all())
    assert len(events) == len(documents) == len(executions) == len(actions) == 1
    assert events[0].metadata_json["capture_mode"] == "intentional"
    assert documents[0].template_version == "general-v1"
    assert executions[0].source_id == str(documents[0].id)
    assert executions[0].workflow_id == workflow.id
    assert actions[0].status == expected_status
    assert classify.await_count == 1
    assert workspace.id == documents[0].workspace_id


async def test_pending_actions_worker_job_calls_executor_without_exposing_payload(monkeypatch):
    process = AsyncMock(return_value={"succeeded": 2, "retry_scheduled": 0, "failed": 0, "skipped": 1})
    monkeypatch.setattr(worker, "process_pending_actions", process)

    result = await worker.run_pending_actions_job({})

    assert result["succeeded"] == 2
    process.assert_awaited_once_with(worker.async_session_factory, limit=100)


async def test_recovery_retries_missing_document_after_first_failure(worker_db, monkeypatch):
    workspace, user, _, note = await _runtime_identity(worker_db)
    event = NormalizedEvent(
        workspace_id=workspace.id,
        user_id=user.id,
        source="audio",
        type="AUDIO_TRANSCRIBED",
        external_id=str(note.id),
        content="private transcript must not appear in logs",
        occurred_at=note.created_at,
        metadata_json={"capture_mode": "intentional"},
    )
    note.status = NoteStatus.PROCESSED
    note.structured_data = {"tipo": "nota_geral", "nota": "Recovered summary"}
    worker_db.add_all([note, event])
    await worker_db.commit()
    monkeypatch.setattr(document_pipeline, "classify_event", AsyncMock(return_value=[]))
    real_process = document_recovery.process_generated_note
    attempts = 0

    async def fail_once(session, note_id, user_id):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise RuntimeError("private transcript / provider detail")
        return await real_process(session, note_id, user_id)

    monkeypatch.setattr(document_recovery, "process_generated_note", fail_once)

    first = await document_recovery.reconcile_missing_documents(worker_db, limit=10)
    assert first["failed"] == 1
    assert list((await worker_db.exec(select(GeneratedDocument))).all()) == []

    second = await document_recovery.reconcile_missing_documents(worker_db, limit=10)
    assert second["recovered"] == 1
    document = (await worker_db.exec(select(GeneratedDocument))).one()
    execution = (await worker_db.exec(select(WorkflowExecution))).one()
    assert document.note_id == note.id
    assert execution.source_id == str(document.id)


async def test_recovery_ignores_continuous_legacy_and_missing_or_unprocessed_notes(worker_db):
    workspace, user, _, note = await _runtime_identity(worker_db)
    note.status = NoteStatus.PROCESSED
    await worker_db.flush()
    events = [
        NormalizedEvent(
            workspace_id=workspace.id,
            user_id=user.id,
            source="audio",
            type="AUDIO_TRANSCRIBED",
            external_id=str(note.id),
            content="not logged",
            occurred_at=note.created_at,
            metadata_json={"capture_mode": "continuous"},
        ),
        NormalizedEvent(
            workspace_id=workspace.id,
            user_id=user.id,
            source="audio",
            type="AUDIO_TRANSCRIBED",
            external_id=str(note.id) + "-legacy",
            content="not logged",
            occurred_at=note.created_at,
            metadata_json={},
        ),
        NormalizedEvent(
            workspace_id=workspace.id,
            user_id=user.id,
            source="audio",
            type="AUDIO_TRANSCRIBED",
            external_id=str(uuid.uuid4()),
            content="not logged",
            occurred_at=note.created_at,
            metadata_json={"capture_mode": "intentional"},
        ),
    ]
    worker_db.add_all(events)
    await worker_db.commit()

    result = await document_recovery.reconcile_missing_documents(worker_db, limit=10)

    assert result["recovered"] == 0
    assert result["skipped"] == 1
    assert list((await worker_db.exec(select(GeneratedDocument))).all()) == []


async def test_worker_registers_runtime_and_existing_crons():
    function_names = {function.__name__ for function in worker.WorkerSettings.functions}
    cron_names = {getattr(cron_job.coroutine, "__name__", "") for cron_job in worker.WorkerSettings.cron_jobs}

    assert "run_pending_actions_job" in function_names
    assert "reconcile_missing_documents_job" in function_names
    assert {
        "check_scheduled_reminders",
        "reconcile_due_items",
        "run_pending_actions_job",
        "reconcile_missing_documents_job",
    }.issubset(cron_names)
    assert {"renew_gmail_watches_job", "reconcile_gmail_histories_job"}.issubset(cron_names)
