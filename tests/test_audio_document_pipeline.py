"""End-to-end audio worker integration for routing, documents, and suggestions."""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

from sqlmodel import select

import app.services.document_pipeline as document_pipeline
import app.services.router as router
import app.services.general_note as general_note
import app.worker as worker
from app.models.action_item import ActionSuggestion, Reminder, Task
from app.models.generated_document import GeneratedDocument
from app.models.normalized_event import NormalizedEvent
from app.models.note import Note, NoteStatus
from app.models.template import Template
from app.models.template_routing_rule import TemplateRoutingRule
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMembership
from app.schemas.intelligence import IntelligenceResult, IntelligenceType


async def _audio_scope(session, *, workspace_enabled: bool = True, with_template: bool = False):
    workspace = Workspace(name="Audio document workspace", timezone="America/Sao_Paulo", locale="pt-BR")
    user = User(
        name="Audio document user",
        email=f"audio-doc-{uuid.uuid4().hex}@example.test",
        hashed_password="not-a-real-password",
        workspace_id=workspace.id if workspace_enabled else None,
    )
    session.add(workspace)
    session.add(user)
    await session.flush()
    if workspace_enabled:
        session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="OWNER"))
    template = None
    if with_template:
        template = Template(
            user_id=user.id,
            title="Owned format",
            purpose="Use for deterministic test routing",
            semantic_trigger="no trigger overlap expected",
            version=9,
            output_schema={"type": "object"},
            actions=[],
        )
        session.add(template)
        await session.flush()
    note = Note(user_id=user.id, raw_transcription="", status=NoteStatus.PENDING_REVIEW)
    session.add(note)
    await session.commit()
    await session.refresh(note)
    return workspace, user, note, template


def _install_stt(monkeypatch, transcription: str):
    class FakeSTT:
        async def transcribe(self, _path):
            return transcription

    monkeypatch.setattr(worker, "get_stt_provider", lambda: FakeSTT())
    monkeypatch.setattr(worker, "check_audible", lambda _path: (True, "ok"))


def _intelligence_results():
    return [
        IntelligenceResult(
            type=IntelligenceType.TASK,
            title="Call client",
            summary="Call the client back.",
            confidence=0.96,
        ),
        IntelligenceResult(
            type=IntelligenceType.REMINDER,
            title="Review tomorrow",
            summary="Review the proposal tomorrow.",
            confidence=0.93,
            due_at=datetime(2026, 10, 2, 14, tzinfo=timezone.utc),
        ),
    ]


async def test_general_audio_creates_event_document_and_idempotent_suggestions(worker_db, tmp_path, monkeypatch):
    workspace, user, note, _ = await _audio_scope(worker_db)
    _install_stt(monkeypatch, "I need to call a client and review the proposal tomorrow.")

    async def general(_text, _context):
        return {
            "title": "Client follow-up",
            "body": "Call client; review proposal tomorrow.",
            "tasks": [],
            "due_date": None,
            "reminder_at": None,
            "send_to_kanban": False,
        }

    classify = AsyncMock(return_value=_intelligence_results())
    monkeypatch.setattr(worker, "generate_general_note", general)
    monkeypatch.setattr(document_pipeline, "classify_event", classify)

    first = await worker.process_audio_job({}, str(note.id), str(tmp_path / "audio.wav"), str(user.id))
    second = await worker.process_audio_job({}, str(note.id), str(tmp_path / "audio.wav"), str(user.id))

    assert first["status"] == second["status"] == "general"
    events = list((await worker_db.exec(select(NormalizedEvent))).all())
    documents = list((await worker_db.exec(select(GeneratedDocument))).all())
    tasks = list((await worker_db.exec(select(Task))).all())
    reminders = list((await worker_db.exec(select(Reminder))).all())
    assert len(events) == len(documents) == 1
    assert events[0].workspace_id == workspace.id and events[0].external_id == str(note.id)
    assert documents[0].template_version == "general-v1"
    assert documents[0].source_event_id == events[0].id
    assert len(tasks) == 1 and tasks[0].status == "suggested"
    assert len(reminders) == 1 and reminders[0].status == "suggested"
    assert reminders[0].remind_at == reminders[0].due_at
    assert classify.await_count == 1
    assert list((await worker_db.exec(select(ActionSuggestion))).all()) == []


async def test_db_rule_selects_owned_template_without_llm_and_snapshots_version(
    worker_db, tmp_path, monkeypatch
):
    workspace, user, note, template = await _audio_scope(worker_db, with_template=True)
    assert template is not None
    second = Template(
        user_id=user.id,
        title="Context-only template",
        purpose="Must not match without context",
        semantic_trigger="no overlap",
        version=3,
        output_schema={"type": "object"},
        actions=[],
    )
    worker_db.add(second)
    await worker_db.flush()
    worker_db.add_all([
        # Insert the newer equal-priority global rule first: created_at, not
        # database return order, must choose the older rule deterministically.
        TemplateRoutingRule(
            workspace_id=workspace.id,
            user_id=user.id,
            template_id=second.id,
            priority=10,
            enabled=True,
            created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        ),
        TemplateRoutingRule(
            workspace_id=workspace.id,
            user_id=user.id,
            template_id=template.id,
            priority=10,
            enabled=True,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
        TemplateRoutingRule(
            workspace_id=workspace.id,
            user_id=user.id,
            template_id=second.id,
            context="finance",
            priority=1000,
            capture_mode="intentional",
            enabled=True,
        ),
        TemplateRoutingRule(
            workspace_id=workspace.id,
            user_id=user.id,
            template_id=second.id,
            priority=999,
            enabled=False,
        ),
    ])
    await worker_db.commit()
    _install_stt(monkeypatch, "transcript deliberately unrelated to either template")
    monkeypatch.setattr(router.settings, "OPENAI_API_KEY", "test-key")
    llm = AsyncMock()
    monkeypatch.setattr(router, "chat_create", llm)
    async def extract(_template, _transcription):
        return {"result": "extracted"}

    monkeypatch.setattr(worker, "extract_structured", extract)
    monkeypatch.setattr(worker, "dispatch_actions", AsyncMock(return_value=[]))
    monkeypatch.setattr(document_pipeline, "classify_event", AsyncMock(return_value=[]))

    result = await worker.process_audio_job({}, str(note.id), str(tmp_path / "audio.wav"), str(user.id))

    assert result["status"] == "processed", result
    llm.assert_not_awaited()
    document = (await worker_db.exec(select(GeneratedDocument))).first()
    assert document is not None
    assert document.template_id == template.id
    assert document.template_version == "9"


async def test_workspace_less_user_keeps_legacy_path_when_llm_unavailable(
    worker_db, tmp_path, monkeypatch
):
    _, user, note, _ = await _audio_scope(worker_db, workspace_enabled=False)
    transcription = "This is a sufficiently long transcript for local general note fallback."
    _install_stt(monkeypatch, transcription)
    monkeypatch.setattr(general_note.settings, "OPENAI_API_KEY", "")
    monkeypatch.setattr(router.settings, "OPENAI_API_KEY", "")

    result = await worker.process_audio_job({}, str(note.id), str(tmp_path / "audio.wav"), str(user.id))

    assert result["ok"] is True and result["status"] == "general", result
    await worker_db.refresh(note)
    assert note.status == NoteStatus.PROCESSED
    assert list((await worker_db.exec(select(NormalizedEvent))).all()) == []
    assert list((await worker_db.exec(select(GeneratedDocument))).all()) == []


async def test_document_failure_rolls_back_and_keeps_note_and_dispatcher(worker_db, tmp_path, monkeypatch):
    _, user, note, template = await _audio_scope(worker_db, with_template=True)
    assert template is not None
    _install_stt(monkeypatch, "template extraction transcript")

    async def extract(_template, _transcription):
        return {"result": "extracted"}

    dispatch = AsyncMock(return_value=[])
    monkeypatch.setattr(worker, "extract_structured", extract)
    monkeypatch.setattr(worker, "route_transcription", AsyncMock(return_value=type(
        "Route", (), {"matched": True, "template_id": template.id}
    )()))
    monkeypatch.setattr(worker, "dispatch_actions", dispatch)

    async def failed_document(*_args, **_kwargs):
        raise RuntimeError("sensitive DB detail must not be logged")

    monkeypatch.setattr(worker, "process_generated_note", failed_document)
    result = await worker.process_audio_job({}, str(note.id), str(tmp_path / "audio.wav"), str(user.id))

    assert result["status"] == "processed", result
    await worker_db.refresh(note)
    assert note.status == NoteStatus.PROCESSED
    dispatch.assert_awaited_once()
