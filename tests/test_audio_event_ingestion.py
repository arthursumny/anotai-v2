"""Worker integration for idempotent audio transcription events."""

import uuid

import pytest
from sqlmodel import select

import app.worker as worker
from app.models.normalized_event import NormalizedEvent
from app.models.note import Note, NoteStatus
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMembership


async def _workspace_note(session, *, workspace_enabled: bool = True):
    workspace = Workspace(name="Audio test")
    user = User(
        name="Audio Tester",
        email=f"audio-{uuid.uuid4().hex}@example.test",
        hashed_password="not-a-real-password",
        workspace_id=workspace.id if workspace_enabled else None,
    )
    session.add(workspace)
    session.add(user)
    await session.flush()
    if workspace_enabled:
        session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="OWNER"))
    note = Note(user_id=user.id, raw_transcription="", status=NoteStatus.PENDING_REVIEW)
    session.add(note)
    await session.commit()
    await session.refresh(note)
    return workspace, user, note


def _install_audio_mocks(monkeypatch, mode: str):
    transcription = "PII de teste: transcrição reconhecida"

    class FakeSTT:
        async def transcribe(self, _path):
            return transcription

    monkeypatch.setattr(worker, "get_stt_provider", lambda: FakeSTT())
    monkeypatch.setattr(worker, "check_audible", lambda _path: (True, "ok"))
    if mode == "intentional":
        async def fake_general(_text, _context):
            return {
                "title": "Áudio",
                "body": "Resumo de teste",
                "tasks": [],
                "due_date": None,
                "reminder_at": None,
                "send_to_kanban": False,
            }

        monkeypatch.setattr(worker, "generate_general_note", fake_general)
    else:
        async def fake_continuous(_text, _context):
            return []

        monkeypatch.setattr(worker, "analyze_continuous_chunk", fake_continuous)
    return transcription


@pytest.mark.parametrize("mode", ["intentional", "continuous"])
async def test_audio_transcription_is_ingested_for_both_capture_modes(worker_db, tmp_path, monkeypatch, mode):
    workspace, user, note = await _workspace_note(worker_db)
    transcription = _install_audio_mocks(monkeypatch, mode)

    result = await worker.process_audio_job({}, str(note.id), str(tmp_path / "audio.wav"), str(user.id), mode)

    assert result["ok"] is True, result
    events = list((await worker_db.exec(select(NormalizedEvent))).all())
    assert len(events) == 1
    event = events[0]
    assert event.workspace_id == workspace.id
    assert event.user_id == user.id
    assert event.source == "audio"
    assert event.type == "AUDIO_TRANSCRIBED"
    assert event.external_id == str(note.id)
    assert event.content == transcription
    assert event.metadata_json["capture_mode"] == mode
    # SQLite may return a naive datetime even for a timezone-aware database column.
    assert event.occurred_at is not None


async def test_audio_event_is_idempotent_across_worker_retries(worker_db, tmp_path, monkeypatch):
    workspace, user, note = await _workspace_note(worker_db)
    transcription = _install_audio_mocks(monkeypatch, "intentional")
    audio_path = str(tmp_path / "audio.wav")

    first = await worker.process_audio_job({}, str(note.id), audio_path, str(user.id), "intentional")
    second = await worker.process_audio_job({}, str(note.id), audio_path, str(user.id), "intentional")

    assert first["ok"] is True and second["ok"] is True
    events = list((await worker_db.exec(select(NormalizedEvent))).all())
    assert len(events) == 1
    assert events[0].workspace_id == workspace.id
    assert events[0].external_id == str(note.id)
    assert events[0].content == transcription


async def test_user_without_workspace_keeps_legacy_audio_pipeline(worker_db, tmp_path, monkeypatch):
    _, user, note = await _workspace_note(worker_db, workspace_enabled=False)
    _install_audio_mocks(monkeypatch, "intentional")

    result = await worker.process_audio_job({}, str(note.id), str(tmp_path / "audio.wav"), str(user.id))

    assert result["ok"] is True, result
    assert list((await worker_db.exec(select(NormalizedEvent))).all()) == []
    await worker_db.refresh(note)
    assert note.status == NoteStatus.PROCESSED
