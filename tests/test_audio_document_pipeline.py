"""Recording to readable document with atomic persistence and action isolation."""

import uuid
from unittest.mock import AsyncMock

from sqlmodel import select

import app.services.note_pipeline as pipeline
import app.worker as worker
from app.models.action_execution import ActionExecution
from app.models.action_item import Task
from app.models.execution_log import ExecutionLog
from app.models.generated_document import GeneratedDocument
from app.models.normalized_event import NormalizedEvent
from app.models.note import Note, NoteStatus
from app.models.template import Template
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMembership


async def _audio_scope(session, *, workspace_enabled=True, with_template=False):
    workspace = Workspace(name="Audio", timezone="America/Sao_Paulo")
    user = User(
        name="Audio user", email=f"{uuid.uuid4().hex}@example.test",
        hashed_password="test", workspace_id=workspace.id if workspace_enabled else None,
    )
    session.add_all([workspace, user])
    await session.flush()
    if workspace_enabled:
        session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id))
    template = None
    if with_template:
        template = Template(
            user_id=user.id, title="Ata", purpose="Registrar decisões", semantic_trigger="reunião",
            example_output="## Decisões\n- Exemplo", version=9,
            output_schema={"type": "object"}, actions=[],
        )
        session.add(template)
    note = Note(user_id=user.id)
    session.add(note)
    await session.commit()
    return workspace, user, note, template


def _install_stt(monkeypatch, transcription):
    stt = AsyncMock()
    stt.transcribe.return_value = transcription
    monkeypatch.setattr(worker, "get_stt_provider", lambda: stt)
    monkeypatch.setattr(worker, "check_audible", lambda _path: (True, "ok"))
    return stt


def _document():
    return {"title": "Decisões da equipe", "body": "## Decisões\n- Entregar proposta sexta.",
            "summary": "Entregar proposta sexta.", "data": {"prazo": "sexta"}}


async def test_general_audio_saves_document_without_unconfigured_tasks(worker_db, tmp_path, monkeypatch, openai_stub):
    workspace, user, note, _ = await _audio_scope(worker_db)
    stt = _install_stt(monkeypatch, "Entregar proposta sexta.")
    requests = openai_stub(_document())
    result = await worker.process_audio_job({}, str(note.id), str(tmp_path / "audio.wav"), str(user.id))
    assert result["status"] == "general"
    saved = await worker_db.get(Note, note.id)
    document = (await worker_db.exec(select(GeneratedDocument))).one()
    event = (await worker_db.exec(select(NormalizedEvent))).one()
    assert saved.body == document.rendered_content == _document()["body"]
    assert document.workspace_id == workspace.id and document.template_id is None
    assert event.content == saved.raw_transcription == "Entregar proposta sexta."
    assert list((await worker_db.exec(select(Task))).all()) == []
    assert list((await worker_db.exec(select(ActionExecution))).all()) == []
    assert len(requests) == stt.transcribe.await_count == 1


async def test_openai_selected_template_is_snapshotted_with_readable_body(worker_db, tmp_path, monkeypatch, openai_stub):
    _, user, note, template = await _audio_scope(worker_db, with_template=True)
    _install_stt(monkeypatch, "Entregar proposta sexta.")
    openai_stub(
        {"template_id": str(template.id), "confidence": 0.95, "reason": "matched"}, _document(),
    )
    result = await worker.process_audio_job({}, str(note.id), str(tmp_path / "audio.wav"), str(user.id))
    document = (await worker_db.exec(select(GeneratedDocument))).one()
    assert result["status"] == "processed"
    assert document.template_id == note.template_id == template.id
    assert document.template_version == "9"
    assert document.rendered_content == _document()["body"]
    assert note.structured_data == {"prazo": "sexta"}


async def test_retry_does_not_retranscribe_regenerate_or_duplicate_actions(worker_db, tmp_path, monkeypatch, openai_stub):
    _, user, note, template = await _audio_scope(worker_db, with_template=True)
    template.actions = [{"type": "create_task", "enabled": True, "config": {"title": "{title}", "summary": "{summary}"}}]
    worker_db.add(template)
    await worker_db.commit()
    stt = _install_stt(monkeypatch, "Entregar proposta sexta.")
    requests = openai_stub(
        {"template_id": str(template.id), "confidence": 0.95, "reason": "matched"}, _document(),
    )
    first = await worker.process_audio_job({}, str(note.id), str(tmp_path / "audio.wav"), str(user.id))
    second = await worker.process_audio_job({}, str(note.id), str(tmp_path / "audio.wav"), str(user.id))
    assert first["ok"] and second["ok"]
    assert stt.transcribe.await_count == 1 and len(requests) == 2
    assert len(list((await worker_db.exec(select(GeneratedDocument))).all())) == 1
    assert len(list((await worker_db.exec(select(ActionExecution))).all())) == 1


async def test_document_write_failure_never_marks_note_processed_or_runs_actions(
    worker_db, tmp_path, monkeypatch, openai_stub,
):
    _, user, note, _ = await _audio_scope(worker_db)
    _install_stt(monkeypatch, "Transcrição preservada.")
    openai_stub(_document())
    monkeypatch.setattr(pipeline, "process_generated_note", AsyncMock(side_effect=RuntimeError("write failed")))
    result = await worker.process_audio_job({}, str(note.id), str(tmp_path / "audio.wav"), str(user.id))
    await worker_db.refresh(note)
    assert result["ok"] is False
    assert note.status == NoteStatus.PENDING_REVIEW and note.body == ""
    assert note.raw_transcription == "Transcrição preservada."
    assert list((await worker_db.exec(select(GeneratedDocument))).all()) == []
    assert list((await worker_db.exec(select(ActionExecution))).all()) == []
    assert len(list((await worker_db.exec(select(ExecutionLog))).all())) == 1
