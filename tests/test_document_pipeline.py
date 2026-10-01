"""Generated note document and suggestion pipeline tests."""

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from sqlmodel import select

import app.services.document_pipeline as pipeline
import app.services.intelligence as intelligence
from app.models.action_item import ActionSuggestion, Reminder, Task
from app.models.generated_document import GeneratedDocument
from app.models.normalized_event import NormalizedEvent
from app.models.note import Note, NoteStatus
from app.models.template import Template
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMembership
from app.schemas.intelligence import IntelligenceResult, IntelligenceType


async def _scope(session, *, with_workspace: bool = True, with_event: bool = True, template: bool = False):
    workspace = Workspace(name="Document workspace", timezone="America/Sao_Paulo", locale="pt-BR")
    user = User(
        name="Document user",
        email=f"document-{uuid.uuid4().hex}@example.test",
        hashed_password="not-a-real-password",
        workspace_id=workspace.id if with_workspace else None,
    )
    session.add(workspace)
    session.add(user)
    await session.flush()
    if with_workspace:
        session.add(WorkspaceMembership(workspace_id=workspace.id, user_id=user.id, role="OWNER"))
    tpl = None
    if template:
        tpl = Template(
            user_id=user.id,
            title="Owned template",
            purpose="test",
            semantic_trigger="test",
            version=7,
            output_schema={"type": "object"},
            actions=[],
        )
        session.add(tpl)
        await session.flush()
    note = Note(
        user_id=user.id,
        template_id=tpl.id if tpl else None,
        title="General note" if not tpl else "Template note",
        raw_transcription="source transcript",
        structured_data={"tipo": "nota_geral", "nota": "Summary: remember the appointment", "value": 1},
        status=NoteStatus.PROCESSED,
    )
    session.add(note)
    await session.flush()
    event = None
    if with_workspace and with_event:
        event = NormalizedEvent(
            workspace_id=workspace.id,
            user_id=user.id,
            source="audio",
            type="AUDIO_TRANSCRIBED",
            external_id=str(note.id),
            content="transcribed audio event",
            occurred_at=datetime(2026, 9, 20, 12, tzinfo=timezone.utc),
            metadata_json={},
        )
        session.add(event)
    await session.commit()
    await session.refresh(note)
    return workspace, user, note, event, tpl


def _task_result() -> IntelligenceResult:
    return IntelligenceResult(
        type=IntelligenceType.TASK,
        title="Call back",
        summary="Call the customer back.",
        confidence=0.95,
    )


def _reminder_result() -> IntelligenceResult:
    return IntelligenceResult(
        type=IntelligenceType.REMINDER,
        title="Appointment reminder",
        summary="Remember the appointment.",
        confidence=0.95,
        due_at=datetime(2026, 9, 25, 9, tzinfo=timezone.utc),
    )


async def test_kanban_task_is_deduplicated_but_reminder_is_materialized(db_session, monkeypatch):
    _, user, note, _, _ = await _scope(db_session)
    note.structured_data = {**(note.structured_data or {}), "kanban_task_ids": ["existing-task"]}
    await db_session.commit()
    monkeypatch.setattr(pipeline, "classify_event", AsyncMock(return_value=[_task_result(), _reminder_result()]))

    await pipeline.process_generated_note(db_session, note.id, user.id)

    assert list((await db_session.exec(select(Task))).all()) == []
    assert len(list((await db_session.exec(select(Reminder))).all())) == 1


async def test_legacy_note_reminder_is_deduplicated(db_session, monkeypatch):
    _, user, note, _, _ = await _scope(db_session)
    note.reminder_at = datetime(2026, 9, 25, 9, tzinfo=timezone.utc)
    await db_session.commit()
    monkeypatch.setattr(pipeline, "classify_event", AsyncMock(return_value=[_task_result(), _reminder_result()]))

    await pipeline.process_generated_note(db_session, note.id, user.id)

    assert len(list((await db_session.exec(select(Task))).all())) == 1
    assert list((await db_session.exec(select(Reminder))).all()) == []


async def test_task_remains_suggested_without_kanban_cards(db_session, monkeypatch):
    _, user, note, _, _ = await _scope(db_session)
    monkeypatch.setattr(pipeline, "classify_event", AsyncMock(return_value=[_task_result()]))

    await pipeline.process_generated_note(db_session, note.id, user.id)

    assert len(list((await db_session.exec(select(Task))).all())) == 1


async def test_general_fallback_document_and_suggestions_are_created(db_session, monkeypatch):
    workspace, user, note, event, _ = await _scope(db_session)
    assert event is not None
    classify = AsyncMock(return_value=[_task_result()])
    monkeypatch.setattr(pipeline, "classify_event", classify)

    document = await pipeline.process_generated_note(db_session, note.id, user.id)

    assert document is not None
    assert document.workspace_id == workspace.id
    assert document.user_id == user.id
    assert document.source_event_id == event.id
    assert document.note_id == note.id
    assert document.template_id is None
    assert document.template_version == "general-v1"
    assert document.rendered_content == "Summary: remember the appointment"
    classify.assert_awaited_once()
    classifier_args = classify.await_args
    assert classifier_args is not None
    assert classifier_args.kwargs["occurred_at"].tzinfo is not None
    assert classifier_args.kwargs["timezone"] == workspace.timezone
    tasks = list((await db_session.exec(select(Task))).all())
    assert len(tasks) == 1 and tasks[0].status == "suggested"


async def test_template_document_snapshots_owned_template_version(db_session, monkeypatch):
    _, user, note, event, template = await _scope(db_session, template=True)
    assert event is not None and template is not None
    monkeypatch.setattr(pipeline, "classify_event", AsyncMock(return_value=[]))

    document = await pipeline.process_generated_note(db_session, note.id, user.id)

    assert document is not None
    assert document.source_event_id == event.id
    assert document.template_id == template.id
    assert document.template_version == "7"
    assert '"tipo": "nota_geral"' in document.rendered_content


@pytest.mark.parametrize("with_workspace,with_event", [(False, True), (True, False)])
async def test_missing_workspace_or_audio_event_is_a_noop(db_session, monkeypatch, with_workspace, with_event):
    _, user, note, _, _ = await _scope(db_session, with_workspace=with_workspace, with_event=with_event)
    classify = AsyncMock(return_value=[])
    monkeypatch.setattr(pipeline, "classify_event", classify)

    result = await pipeline.process_generated_note(db_session, note.id, user.id)

    assert result is None
    assert list((await db_session.exec(select(GeneratedDocument))).all()) == []
    classify.assert_not_awaited()


async def test_reprocessing_does_not_duplicate_documents_or_suggestions(db_session, monkeypatch):
    _, user, note, _, _ = await _scope(db_session)
    classify = AsyncMock(return_value=[_task_result()])
    monkeypatch.setattr(pipeline, "classify_event", classify)

    first = await pipeline.process_generated_note(db_session, note.id, user.id)
    second = await pipeline.process_generated_note(db_session, note.id, user.id)

    assert first is not None and second is not None
    assert first.id == second.id
    assert len(list((await db_session.exec(select(GeneratedDocument))).all())) == 1
    assert len(list((await db_session.exec(select(Task))).all())) == 1
    classify.assert_awaited_once()


async def test_no_llm_key_still_saves_document_and_redacts_credentials(db_session, monkeypatch):
    _, user, note, _, _ = await _scope(db_session)
    note.structured_data = {
        "tipo": "nota_geral",
        "nota": "Keep this summary; password: supersecret",
        "api_key": "private-key",
    }
    await db_session.commit()
    monkeypatch.setattr(intelligence.settings, "OPENAI_API_KEY", "")

    document = await pipeline.process_generated_note(db_session, note.id, user.id)

    assert document is not None
    assert "supersecret" not in document.rendered_content
    assert "private-key" not in str(document.structured_data)
    assert list((await db_session.exec(select(ActionSuggestion))).all()) == []
