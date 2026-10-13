"""Runtime integration of generated documents, workflow preparation, and recovery."""

import uuid
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

import app.services.document_recovery as document_recovery
import app.services.note_pipeline as note_pipeline
import app.worker as worker
from app.models.action_execution import ActionExecution
from app.models.automation_settings import AutomationMode, AutomationSettings
from app.models.generated_document import GeneratedDocument
from app.models.normalized_event import NormalizedEvent
from app.models.note import Note, NoteStatus
from app.models.template import Template
from app.models.user import User
from app.models.workflow import Workflow, WorkflowExecution, WorkflowStep
from app.models.workspace import Workspace, WorkspaceMembership
from app.services.extractor import GeneratedNote
from app.services.router import RouterResult


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


async def _route_to(template_id):
    return RouterResult(template_id, 0.9, "forced for test")


async def _template_result(_transcription, _template=None):
    return GeneratedNote(
        title="Runtime note",
        body="A safe summary",
        summary="Runtime summary",
        data={"tipo": "nota_geral"},
    )


@pytest.mark.parametrize(
    "mode,require_confirmation,expected_status",
    [
        ("suggestive", True, "awaiting_confirmation"),
        ("automatic", False, "pending"),
    ],
)
async def test_audio_document_prepares_idempotent_template_action(
    worker_db, tmp_path, monkeypatch, mode, require_confirmation, expected_status
):
    """A processed recording prepares its template's actions exactly once, durably.

    The automations that fire are the **template's own actions** (email/task/
    telegram/webhook/mcp), prepared by ``prepare_template_actions``. The
    multi-step ``Workflow`` engine is CRUD plus confirm/dismiss only: nothing in
    production calls ``emit_trigger``, so a ``DOCUMENT_GENERATED`` workflow
    trigger produces **no** ``WorkflowExecution`` row here. That is asserted
    below so the gap stays visible instead of silently disappearing.
    """
    workspace, user, _workflow, note = await _runtime_identity(worker_db, mode=mode)
    template = Template(
        user_id=user.id,
        title="Meeting notes",
        purpose="Registrar decisões",
        semantic_trigger="reunião",
        example_output="## Decisões",
        output_schema={"type": "object"},
        actions=[{
            "type": "create_task",
            "enabled": True,
            "require_confirmation": require_confirmation,
            "config": {"title": "Review generated note", "summary": ""},
        }],
    )
    worker_db.add(template)
    await worker_db.commit()

    _install_worker_stt(monkeypatch, "audio content")
    monkeypatch.setattr(
        note_pipeline, "route_transcription",
        lambda _text, _templates: _route_to(template.id),
    )
    monkeypatch.setattr(note_pipeline, "generate_note", _template_result)

    first = await worker.process_audio_job({}, str(note.id), str(tmp_path / "audio.wav"), str(user.id))
    second = await worker.process_audio_job({}, str(note.id), str(tmp_path / "audio.wav"), str(user.id))

    assert first["status"] == second["status"] == "processed"
    events = list((await worker_db.exec(select(NormalizedEvent))).all())
    documents = list((await worker_db.exec(select(GeneratedDocument))).all())
    executions = list((await worker_db.exec(select(WorkflowExecution))).all())
    actions = list((await worker_db.exec(select(ActionExecution))).all())
    # One event, one document, exactly one prepared action — and no workflow
    # execution, because nothing emits DOCUMENT_GENERATED triggers.
    assert len(events) == 1
    assert len(documents) == 1
    assert len(executions) == 0
    assert len(actions) == 1
    assert "capture_mode" not in events[0].metadata_json
    assert documents[0].template_version == str(template.version)
    assert actions[0].action_type == "CREATE_TASK"
    assert actions[0].status == expected_status
    assert actions[0].action_input["title"] == "Review generated note"
    assert workspace.id == documents[0].workspace_id
    # The retry reuses the same durable row rather than preparing a second one.
    assert actions[0].idempotency_key.startswith("template-action-v1:")


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
        metadata_json={},
    )
    note.status = NoteStatus.PROCESSED
    note.body = "Recovered summary"
    note.structured_data = {"tipo": "nota_geral", "nota": "Recovered summary"}
    worker_db.add_all([note, event])
    await worker_db.commit()
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
    assert document.note_id == note.id
    # The note was general (no template), so recovery prepares no actions, and no
    # workflow execution exists because nothing emits DOCUMENT_GENERATED triggers.
    executions = list((await worker_db.exec(select(WorkflowExecution))).all())
    actions = list((await worker_db.exec(select(ActionExecution))).all())
    assert executions == [] and actions == []
    # Recovery never regenerates content: the note's own body is what is stored.
    assert document.rendered_content == note.body


async def test_recovery_ignores_legacy_and_missing_or_unprocessed_notes(worker_db):
    workspace, user, _, note = await _runtime_identity(worker_db)  # ainda não processada
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
            metadata_json={},
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
            metadata_json={},
        ),
    ]
    worker_db.add_all(events)
    await worker_db.commit()

    result = await document_recovery.reconcile_missing_documents(worker_db, limit=10)

    # nota não processada, external_id inválido e nota inexistente: nada a recuperar
    assert result["recovered"] == 0
    assert result["skipped"] == 3
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
