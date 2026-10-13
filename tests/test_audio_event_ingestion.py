"""All recordings share one scoped event/document path."""

from sqlmodel import select

import app.worker as worker
from app.models.normalized_event import NormalizedEvent
from app.models.note import NoteStatus
from tests.test_audio_document_pipeline import _audio_scope, _document, _install_stt


async def test_audio_event_is_complete_and_idempotent(worker_db, tmp_path, monkeypatch, openai_stub):
    workspace, user, note, _ = await _audio_scope(worker_db)
    transcript = "Dados completos da gravação."
    _install_stt(monkeypatch, transcript)
    openai_stub(_document())
    first = await worker.process_audio_job({}, str(note.id), str(tmp_path / "audio.wav"), str(user.id))
    second = await worker.process_audio_job({}, str(note.id), str(tmp_path / "audio.wav"), str(user.id))
    events = list((await worker_db.exec(select(NormalizedEvent))).all())
    assert first["ok"] and second["ok"]
    assert len(events) == 1
    assert events[0].workspace_id == workspace.id and events[0].user_id == user.id
    assert events[0].external_id == str(note.id) and events[0].content == transcript
    assert "capture_mode" not in events[0].metadata_json


async def test_account_without_workspace_cannot_take_an_unscoped_legacy_path(worker_db, tmp_path, monkeypatch):
    _, user, note, _ = await _audio_scope(worker_db, workspace_enabled=False)
    _install_stt(monkeypatch, "Transcrição preservada.")
    result = await worker.process_audio_job({}, str(note.id), str(tmp_path / "audio.wav"), str(user.id))
    await worker_db.refresh(note)
    assert result["ok"] is False and note.status == NoteStatus.PENDING_REVIEW
    assert note.raw_transcription == "Transcrição preservada."
    assert list((await worker_db.exec(select(NormalizedEvent))).all()) == []
