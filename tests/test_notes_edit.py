"""AnotAI — testes do ciclo human-in-the-loop: PATCH, reclassify e dispatch."""

import uuid

from httpx import AsyncClient
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

import app.services.integration_service as integration_service
import app.services.note_pipeline as note_pipeline
from app.models.action_execution import ActionExecution
from app.models.note import Note, NoteStatus
from app.services.extractor import GeneratedNote
from tests.automation_helpers import execute_actions
from tests.test_dispatcher import FakeBot

VET_SCHEMA = {
    "type": "object",
    "required": ["animal_nome"],
    "properties": {"animal_nome": {"type": "string"}, "sintomas": {"type": "array", "items": {"type": "string"}}},
}

# Contrato atual: o token do Telegram é sempre o da plataforma; o secret do
# usuário fornece apenas o chat de destino.
PLATFORM_BOT_TOKEN = "bot-plataforma-123"


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
        body="Thor apresenta tosse há 3 dias.",
        structured_data=None,
        status=NoteStatus.PENDING_REVIEW,
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


async def _ledger_rows(db: AsyncSession, note_id: str) -> list[ActionExecution]:
    """Linhas duráveis do ledger preparadas para uma nota (reclassify/dispatch)."""
    return list((await db.exec(
        select(ActionExecution).where(
            ActionExecution.source_id == note_id,
            ActionExecution.trigger_type == "DOCUMENT_GENERATED",
        ).order_by(ActionExecution.created_at.asc())
    )).all())


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


async def test_patch_kanban_status_is_rejected(client: AsyncClient, auth_headers, db_session):
    """`kanban_status` saiu do NoteUpdate (extra="forbid"): o quadro é Tarefas."""
    headers, uid = auth_headers
    nid = await _make_note(db_session, uid)

    r = await client.patch(f"/api/v1/notes/{nid}", headers=headers,
                           json={"kanban_status": "in_progress"})
    assert r.status_code == 422, r.text


async def test_reclassify_runs_extraction_and_dispatch(client: AsyncClient, auth_headers, db_session, monkeypatch):
    headers, uid = auth_headers
    FakeBot.last_token = None
    FakeBot.last_sent = None
    monkeypatch.setattr(integration_service, "Bot", FakeBot)
    monkeypatch.setattr(integration_service.settings, "TELEGRAM_BOT_TOKEN", PLATFORM_BOT_TOKEN)

    async def fake_generate(transcription, template=None):
        assert "Thor" in transcription
        assert template is not None
        return GeneratedNote(
            title="Prontuário do Thor",
            body="Thor apresenta tosse há 3 dias.",
            summary="Consulta veterinária",
            data={"animal_nome": "Thor", "sintomas": ["tosse"]},
        )

    monkeypatch.setattr(note_pipeline, "generate_note", fake_generate)

    tpl = await _make_template(client, headers, actions=[
        {"type": "telegram", "enabled": True, "config": {
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

    # Contrato atual: o request prepara uma linha durável e idempotente no
    # ledger (status pending); a execução acontece depois, no worker.
    note = await db_session.get(Note, uuid.UUID(nid))
    rows = await _ledger_rows(db_session, nid)
    assert len(rows) == 1
    prepared = rows[0]
    assert prepared.action_type == "SEND_TELEGRAM"
    assert prepared.status == "pending"
    assert prepared.action_input == {"confidence": None, "text": "Prontuário de Thor"}
    assert prepared.idempotency_key.startswith("template-action-v1:")

    # Reclassificar de novo não cria uma segunda linha para a mesma geração.
    r = await client.post(f"/api/v1/notes/{nid}/reclassify", headers=headers, json={"template_id": tpl["id"]})
    assert r.status_code == 200, r.text
    assert [row.id for row in await _ledger_rows(db_session, nid)] == [prepared.id]

    # Metade de execução: o worker despacha a linha pendente preparada.
    executed = await execute_actions(db_session, note)
    assert [row.id for row in executed] == [prepared.id]
    await db_session.refresh(prepared)
    assert prepared.status == "succeeded"
    assert FakeBot.last_token == PLATFORM_BOT_TOKEN  # credencial de bot é só da plataforma
    assert FakeBot.last_sent is not None and FakeBot.last_sent["chat_id"] == "999"
    assert "Thor" in FakeBot.last_sent["text"]


async def test_redispatch_after_manual_fix(client: AsyncClient, auth_headers, db_session, monkeypatch):
    headers, uid = auth_headers
    FakeBot.last_token = None
    FakeBot.last_sent = None
    monkeypatch.setattr(integration_service, "Bot", FakeBot)
    monkeypatch.setattr(integration_service.settings, "TELEGRAM_BOT_TOKEN", PLATFORM_BOT_TOKEN)

    tpl = await _make_template(client, headers, actions=[
        {"type": "telegram", "enabled": True, "config": {
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

    # Contrato atual: o dispatch prepara a linha durável no ledger com os dados
    # corrigidos, ainda pendente — o worker é quem executa depois.
    note = await db_session.get(Note, uuid.UUID(nid))
    rows = await _ledger_rows(db_session, nid)
    assert len(rows) == 1
    prepared = rows[0]
    assert prepared.action_type == "SEND_TELEGRAM"
    assert prepared.status == "pending"
    assert prepared.action_input == {"confidence": None, "text": "Prontuário de Rex"}
    assert prepared.idempotency_key.startswith("template-action-v1:")

    # Re-disparar não duplica a linha do ledger.
    r = await client.post(f"/api/v1/notes/{nid}/dispatch", headers=headers)
    assert r.status_code == 200, r.text
    assert [row.id for row in await _ledger_rows(db_session, nid)] == [prepared.id]

    # Metade de execução: o worker despacha a linha pendente com os dados corrigidos.
    executed = await execute_actions(db_session, note)
    assert [row.id for row in executed] == [prepared.id]
    await db_session.refresh(prepared)
    assert prepared.status == "succeeded"
    assert FakeBot.last_token == PLATFORM_BOT_TOKEN
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
