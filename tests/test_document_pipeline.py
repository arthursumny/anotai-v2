"""Owned readable snapshots, atomic failures, and idempotent edits."""

import uuid

import pytest
from sqlmodel import select

from app.models.action_item import Task
from app.models.generated_document import GeneratedDocument
from app.models.note import NoteStatus
from app.services.document_pipeline import process_generated_note
from tests.test_audio_document_pipeline import _audio_scope


async def _processed(session, **options):
    workspace, user, note, template = await _audio_scope(session, **options)
    note.body = "## Documento\nTodas as informações."
    note.summary = "Resumo separado."
    note.structured_data = {"value": 7}
    note.template_id = template.id if template else None
    note.status = NoteStatus.PROCESSED
    session.add(note)
    await session.commit()
    return workspace, user, note, template


async def test_snapshot_uses_body_instead_of_rendering_json(db_session):
    _, user, note, template = await _processed(db_session, with_template=True)
    document = await process_generated_note(db_session, note.id, user.id)
    assert document.rendered_content == note.body
    assert document.template_id == template.id and document.template_version == "9"
    assert document.structured_data == {"value": 7}
    assert list((await db_session.exec(select(Task))).all()) == []


async def test_general_snapshot_is_a_saved_document_even_without_retained_event(db_session):
    _, user, note, _ = await _processed(db_session)
    document = await process_generated_note(db_session, note.id, user.id)
    assert document.rendered_content == note.body and document.template_id is None
    assert document.source_event_id is None


async def test_edit_updates_same_document_without_duplicate(db_session):
    _, user, note, _ = await _processed(db_session)
    first = await process_generated_note(db_session, note.id, user.id)
    note.body = "## Documento\nInformação corrigida."
    second = await process_generated_note(db_session, note.id, user.id)
    assert first.id == second.id
    assert second.rendered_content == note.body
    assert len(list((await db_session.exec(select(GeneratedDocument))).all())) == 1


async def test_unscoped_account_fails_explicitly(db_session):
    _, user, note, _ = await _processed(db_session, workspace_enabled=False)
    with pytest.raises(ValueError):
        await process_generated_note(db_session, note.id, user.id)
    assert list((await db_session.exec(select(GeneratedDocument))).all()) == []


async def test_other_owner_cannot_materialize_document(db_session):
    _, _, note, _ = await _processed(db_session)
    with pytest.raises(ValueError):
        await process_generated_note(db_session, note.id, uuid.uuid4())


async def test_empty_body_cannot_be_marked_as_complete_document(db_session):
    _, user, note, _ = await _processed(db_session)
    note.body = ""
    with pytest.raises(ValueError):
        await process_generated_note(db_session, note.id, user.id)
