"""AnotAI — testes do ciclo human-in-the-loop: PATCH, reclassify e dispatch."""

import uuid

from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

import app.services.dispatcher as dispatcher
from app.models.note import Note, NoteStatus
from tests.test_dispatcher import FakeBot

VET_SCHEMA = {
    "type": "object",
    "required": ["animal_nome"],
    "properties": {"animal_nome": {"type": "string"}, "sintomas": {"type": "array", "items": {"type": "string"}}},
}


async def _make_template(client: AsyncClient, headers: dict, **over) -> dict:
    payload = {
        "title": "Vet",
        "purpose": "Transformar áudio de consulta em prontuário.",
        "semantic_trigger": "consulta veterinária",
        "output_schema": VET_SCHEMA,
        "actions": [],
        "is_public": False,
    }
    payload.update(over)
    r = await client.post("/api/v1/templates", headers=headers, json=payload)
    assert r.status_code == 201, r.text
    return r.json()


async def _make_note(db: AsyncSession, uid: str, **over) -> str:
    note = Note(
        user_id=uuid.UUID(uid),
        raw_transcription="Consulta do Thor, canino com tosse",
        structured_data=None,
        status=NoteStatus.NO_MATCH,
    )
    for k, v in over.items():
        setattr(note, k, v)
    # garante UUID válido p/ template_id quando passado como str
    if isinstance(note.template_id, str):
        note.template_id = uuid.UUID(note.template_id)
    db.add(note)
    await db.commit()
    await db.refresh(note)
    return str(note.id)


async def test_patch_structured_persists(client: AsyncClient, auth_headers, db_session):
    headers, uid = auth_headers
    tpl = await _make_template(client, headers)
    nid = await _make_note(db_session, uid, template_id=tpl["id"],
                           structured_data={"animal_nome": "Thor"}, status=NoteStatus.PROCESSED)

    r = await client.patch(f"/api/v1/notes/{nid}", headers=headers,
                           json={"structured_data": {"animal_nome": "Rex", "sintomas": ["tosse"]}})
    assert r.status_code == 200, r.text
    assert r.json()["structured_data"]["animal_nome"] == "Rex"

    r = await client.get(f"/api/v1/notes/{nid}", headers=headers)
    assert r.json()["structured_data"]["animal_nome"] == "Rex"  # persistiu

    r = await client.patch(f"/api/v1/notes/{nid}", headers=headers,
                           json={"raw_transcription": "Consulta da Rex"})
    assert r.status_code == 200 and r.json()["raw_transcription"] == "Consulta da Rex"


async def test_patch_validates_against_template_schema(client: AsyncClient, auth_headers, db_session):
    headers, uid = auth_headers
    tpl = await _make_template(client, headers)
    nid = await _make_note(db_session, uid)

    r = await client.patch(f"/api/v1/notes/{nid}", headers=headers,
                           json={"template_id": tpl["id"], "structured_data": {"sintomas": []}})
    assert r.status_code == 422, r.text  # falta animal_nome (required)

    r = await client.patch(f"/api/v1/notes/{nid}", headers=headers,
                           json={"template_id": tpl["id"], "structured_data": {"animal_nome": "Thor"}})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "processed" and body["template_id"] == tpl["id"]


async def test_patch_other_user_note_404(client: AsyncClient, auth_headers, db_session):
    headers, uid = auth_headers
    nid = await _make_note(db_session, uid)
    other_email = f"o+{uuid.uuid4().hex[:8]}@anotai.com"
    await client.post("/api/v1/auth/register",
                      json={"name": "O", "email": other_email, "password": "senha-forte-123"})
    token = (await client.post("/api/v1/auth/login", data={"username": other_email, "password": "senha-forte-123"})).json()["access_token"]
    r = await client.patch(f"/api/v1/notes/{nid}", headers={"Authorization": f"Bearer {token}"},
                           json={"raw_transcription": "hack"})
    assert r.status_code == 404


async def test_reclassify_runs_extraction_and_dispatch(client: AsyncClient, auth_headers, db_session, monkeypatch):
    headers, uid = auth_headers
    monkeypatch.setattr(dispatcher, "Bot", FakeBot)

    async def fake_extract(template, transcription):
        assert "Thor" in transcription
        return {"animal_nome": "Thor", "sintomas": ["tosse"]}

    import app.api.v1.notes as notes_api
    monkeypatch.setattr(notes_api, "extract_structured", fake_extract)

    tpl = await _make_template(client, headers, actions=[
        {"type": "telegram", "config": {
            "bot_token": "${TELEGRAM_BOT_TOKEN}", "chat_id": "${TELEGRAM_CHAT_ID}",
            "template": "Prontuário de {animal_nome}",
        }}
    ])
    await client.post("/api/v1/secrets", headers=headers, json={"key": "TELEGRAM_BOT_TOKEN", "value": "bot-real-123"})
    await client.post("/api/v1/secrets", headers=headers, json={"key": "TELEGRAM_CHAT_ID", "value": "999"})
    nid = await _make_note(db_session, uid)

    r = await client.post(f"/api/v1/notes/{nid}/reclassify", headers=headers, json={"template_id": tpl["id"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "processed"
    assert body["structured_data"] == {"animal_nome": "Thor", "sintomas": ["tosse"]}
    tg_logs = [lg for lg in body["logs"] if lg["action_type"] == "telegram"]
    assert len(tg_logs) == 1 and tg_logs[0]["status"] == "success"
    assert FakeBot.last_token == "bot-real-123"
    assert FakeBot.last_sent is not None and "Thor" in FakeBot.last_sent["text"]


async def test_redispatch_after_manual_fix(client: AsyncClient, auth_headers, db_session, monkeypatch):
    headers, uid = auth_headers
    monkeypatch.setattr(dispatcher, "Bot", FakeBot)

    tpl = await _make_template(client, headers, actions=[
        {"type": "telegram", "config": {
            "bot_token": "${TELEGRAM_BOT_TOKEN}", "chat_id": "${TELEGRAM_CHAT_ID}",
            "template": "Prontuário de {animal_nome}",
        }}
    ])
    await client.post("/api/v1/secrets", headers=headers, json={"key": "TELEGRAM_BOT_TOKEN", "value": "bot-real-123"})
    await client.post("/api/v1/secrets", headers=headers, json={"key": "TELEGRAM_CHAT_ID", "value": "999"})
    nid = await _make_note(db_session, uid, template_id=tpl["id"],
                           structured_data={"animal_nome": "Thor"}, status=NoteStatus.PROCESSED)

    # corrige o JSON à mão e re-dispara
    r = await client.patch(f"/api/v1/notes/{nid}", headers=headers,
                           json={"structured_data": {"animal_nome": "Rex"}})
    assert r.status_code == 200, r.text
    r = await client.post(f"/api/v1/notes/{nid}/dispatch", headers=headers)
    assert r.status_code == 200, r.text
    tg_logs = [lg for lg in r.json()["logs"] if lg["action_type"] == "telegram" and lg["status"] == "success"]
    assert tg_logs, r.text
    assert FakeBot.last_sent is not None and "Rex" in FakeBot.last_sent["text"]


async def test_dispatch_without_template_400(client: AsyncClient, auth_headers, db_session):
    headers, uid = auth_headers
    nid = await _make_note(db_session, uid)
    r = await client.post(f"/api/v1/notes/{nid}/dispatch", headers=headers)
    assert r.status_code == 400, r.text


async def test_delete_note_removes_note_and_logs(client: AsyncClient, auth_headers, db_session):
    from app.models.execution_log import ExecutionLog, ExecutionStatus

    headers, uid = auth_headers
    nid = await _make_note(db_session, uid)
    db_session.add(ExecutionLog(note_id=uuid.UUID(nid), action_type="pipeline",
                                status=ExecutionStatus.FAILED, payload={}))
    await db_session.commit()

    r = await client.delete(f"/api/v1/notes/{nid}", headers=headers)
    assert r.status_code == 204, r.text
    r = await client.get(f"/api/v1/notes/{nid}", headers=headers)
    assert r.status_code == 404
    remaining = await db_session.exec(
        select(ExecutionLog).where(ExecutionLog.note_id == uuid.UUID(nid))
    )
    assert remaining.all() == []


async def test_delete_note_other_user_404(client: AsyncClient, auth_headers, db_session):
    headers, uid = auth_headers
    nid = await _make_note(db_session, uid)
    other_email = f"del+{uuid.uuid4().hex[:8]}@anotai.com"
    await client.post("/api/v1/auth/register",
                      json={"name": "O", "email": other_email, "password": "senha-forte-123"})
    token = (await client.post("/api/v1/auth/login", data={"username": other_email, "password": "senha-forte-123"})).json()["access_token"]
    r = await client.delete(f"/api/v1/notes/{nid}", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 404
