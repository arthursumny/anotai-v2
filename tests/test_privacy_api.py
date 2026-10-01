"""Owner-scoped privacy API coverage."""

import json
import uuid
from datetime import datetime, timezone

import pytest
from sqlmodel import select

from app.models.action_item import ActionSuggestion, FollowUp, Reminder
from app.models.audit_event import AuditEvent
from app.models.generated_document import GeneratedDocument
from app.models.normalized_event import NormalizedEvent
from app.models.note import Note
from app.models.user import User
from app.models.workspace import WorkspaceMembership


def _event(workspace_id, user_id, source="audio"):
    now = datetime.now(timezone.utc)
    return NormalizedEvent(workspace_id=workspace_id, user_id=user_id, source=source,
                           type="capture", content="must not leak", occurred_at=now)


@pytest.mark.asyncio
async def test_delete_data_requires_exact_confirmation(client, auth_headers):
    headers, _ = auth_headers
    response = await client.post("/api/v1/privacy/delete-data", headers=headers,
                                 json={"source": "audio", "confirm": "apagar"})
    assert response.status_code == 422
    extra = await client.post("/api/v1/privacy/delete-data", headers=headers,
                              json={"source": "audio", "confirm": "APAGAR", "workspaceID": "fake"})
    assert extra.status_code == 422


@pytest.mark.asyncio
async def test_summary_is_content_free_and_delete_preserves_notes(client, auth_headers, db_session):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    workspace_id = user.workspace_id
    event = _event(workspace_id, user.id)
    db_session.add(event)
    await db_session.flush()
    doc = GeneratedDocument(workspace_id=workspace_id, user_id=user.id, source_event_id=event.id,
                            template_version="v1", rendered_content="private document")
    reminder = Reminder(workspace_id=workspace_id, user_id=user.id, source_event_id=event.id,
                        idempotency_key="rem", title="private", remind_at=datetime.now(timezone.utc), status="scheduled")
    followup = FollowUp(workspace_id=workspace_id, user_id=user.id, source_event_id=event.id,
                        idempotency_key="follow", title="private", status="waiting")
    suggestion = ActionSuggestion(workspace_id=workspace_id, user_id=user.id, source_event_id=event.id,
                                  idempotency_key="suggest", title="private", status="suggested")
    note = Note(user_id=user.id, title="keep me", raw_transcription="note retained")
    db_session.add_all([doc, reminder, followup, suggestion, note])
    await db_session.commit()

    summary = await client.get("/api/v1/privacy/summary?source=audio", headers=headers)
    assert summary.status_code == 200
    summary_body = summary.json()
    assert summary_body["events"] == 1 and summary_body["generated_documents"] == 1
    assert summary_body["reminders_to_cancel"] == summary_body["follow_ups_to_cancel"] == 1
    assert "must not leak" not in summary.text and "private" not in summary.text

    response = await client.post("/api/v1/privacy/delete-data", headers=headers,
                                 json={"source": "audio", "confirm": "APAGAR"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["complete"] is True and body["counts"]["events"] == 1
    assert body["counts"]["generated_documents"] == body["counts"]["action_suggestions"] == 1
    assert body["notes_retained"] == 1
    assert body["notice"] == "notas preservadas; exclua-as em Notas se desejar"
    assert await db_session.get(NormalizedEvent, event.id) is None
    assert await db_session.get(GeneratedDocument, doc.id) is None
    assert await db_session.get(ActionSuggestion, suggestion.id) is None
    assert await db_session.get(Reminder, reminder.id) is None
    assert await db_session.get(FollowUp, followup.id) is None
    assert (await db_session.get(Note, note.id)).raw_transcription == "note retained"
    audit = (await db_session.exec(select(AuditEvent))).one()
    assert audit.event == "USER_DELETED_SOURCE_DATA" and audit.status == "success"
    assert audit.metadata_json == {"count": 1, "reason_code": "user_delete"}
    assert "private" not in json.dumps(audit.metadata_json)

    again = await client.post("/api/v1/privacy/delete-data", headers=headers,
                              json={"source": "audio", "confirm": "APAGAR"})
    assert again.status_code == 200 and again.json()["counts"]["events"] == 0


@pytest.mark.asyncio
async def test_privacy_endpoints_reject_non_members(client, auth_headers, db_session):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    member = (await db_session.exec(select(WorkspaceMembership).where(
        WorkspaceMembership.workspace_id == user.workspace_id,
        WorkspaceMembership.user_id == user.id,
    ))).one()
    await db_session.delete(member)
    await db_session.commit()
    summary = await client.get("/api/v1/privacy/summary", headers=headers)
    delete = await client.post("/api/v1/privacy/delete-data", headers=headers,
                               json={"source": "all", "confirm": "APAGAR"})
    assert summary.status_code == delete.status_code == 403


@pytest.mark.asyncio
async def test_partial_purge_is_not_reported_as_complete(client, auth_headers, db_session, monkeypatch):
    from app.api.v1 import privacy

    headers, _ = auth_headers

    async def partial(*args, **kwargs):
        return {"events": 2, "generated_documents": 1, "tasks": 0, "reminders": 0,
                "action_suggestions": 0, "pending_conversation_items": 0, "follow_ups": 0, "failed": 1}

    monkeypatch.setattr(privacy, "purge_user_source_events", partial)
    response = await client.post("/api/v1/privacy/delete-data", headers=headers,
                                 json={"source": "all", "confirm": "APAGAR"})
    assert response.status_code == 200
    assert response.json()["complete"] is False
    assert response.json()["counts"]["failed"] == 1
    audits = (await db_session.exec(select(AuditEvent))).all()
    assert len(audits) == 2
    audit = next(event for event in audits if event.action == "delete")
    assert audit.status == "partial"


@pytest.mark.asyncio
async def test_delete_data_records_failed_rows_audit(client, auth_headers, db_session, monkeypatch):
    from app.api.v1 import privacy

    headers, _ = auth_headers

    async def partial(*args, **kwargs):
        return {"events": 3, "generated_documents": 0, "tasks": 0, "reminders": 0,
                "action_suggestions": 0, "pending_conversation_items": 0, "follow_ups": 0, "failed": 2}

    monkeypatch.setattr(privacy, "purge_user_source_events", partial)
    response = await client.post("/api/v1/privacy/delete-data", headers=headers,
                                 json={"source": "all", "confirm": "APAGAR"})
    assert response.status_code == 200
    audits = (await db_session.exec(select(AuditEvent))).all()
    assert len(audits) == 2
    by_action = {audit.action: audit for audit in audits}
    assert by_action["delete"].status == "partial"
    assert by_action["delete"].metadata_json == {"count": 3, "reason_code": "user_delete"}
    assert by_action["delete_failed_rows"].status == "partial"
    assert by_action["delete_failed_rows"].metadata_json == {"count": 2, "reason_code": "user_delete"}
