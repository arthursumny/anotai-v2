"""AnotAI — testes do quadro Kanban, tarefas nativas e lembretes."""

import uuid
import wave
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlmodel import select

from app.core.config import settings
from app.core.user_secret_crypto import encrypt_user_secret
import app.worker as worker
from app.models.action_item import Task
from app.models.execution_log import ExecutionLog, ExecutionStatus
from app.models.note import Note, NoteStatus
from app.models.user import User
from app.models.user_secret import UserSecret


class FakeBot:
    last = None

    def __init__(self, token: str) -> None:
        self.token = token

    async def send_message(self, chat_id, text):
        FakeBot.last = {"token": self.token, "chat_id": chat_id, "text": text}
        return SimpleNamespace(message_id=3)


@pytest.fixture(autouse=True)
def _reset_fake_bot():
    FakeBot.last = None


async def _task(db_session, uid: str, title="Tarefa X", **over) -> str:
    user = await db_session.get(User, uuid.UUID(uid))
    task = Task(
        workspace_id=user.workspace_id, user_id=user.id, title=title,
        idempotency_key=f"kanban-test:{uuid.uuid4()}", **over,
    )
    db_session.add(task)
    await db_session.commit()
    return str(task.id)


async def test_accept_moves_to_kanban_todo(client, auth_headers, db_session):
    headers, uid = auth_headers
    nid = await _task(db_session, uid, title="Ligar fornecedor", status="suggested")
    r = await client.post(f"/api/v1/pendencies/task/{nid}/accept", headers=headers,
                          json={"due_at": "2026-10-02T09:00:00+00:00"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "open"

    r = await client.get("/api/v1/kanban", headers=headers)
    assert r.status_code == 200, r.text
    board = r.json()
    assert [c["id"] for c in board["todo"]] == [nid]
    assert board["todo"][0]["due_at"].startswith("2026-10-02T09:00:00")
    assert board["todo"][0]["kanban_status"] == "todo"
    assert "raw_transcription" not in board["todo"][0]
    assert board["in_progress"] == [] and board["done"] == []

    r = await client.post(f"/api/v1/pendencies/task/{nid}/accept", headers=headers, json={})
    assert r.status_code == 200

    r = await client.patch(f"/api/v1/kanban/{nid}/move", headers=headers,
                          json={"kanban_status": "urgente"})
    assert r.status_code in (400, 422)


async def test_accept_defaults_to_todo(client, auth_headers, db_session):
    headers, uid = auth_headers
    nid = await _task(db_session, uid, status="suggested")
    r = await client.post(f"/api/v1/pendencies/task/{nid}/accept", headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "open", r.text
    board = await client.get("/api/v1/kanban", headers=headers)
    assert board.json()["todo"][0]["id"] == nid


# ------------------------------------------------------------ kanban ---
async def test_move_card_between_columns(client, auth_headers, db_session):
    headers, uid = auth_headers
    target = datetime(2030, 1, 2, 9, tzinfo=timezone.utc)
    nid = await _task(db_session, uid, due_at=target)

    r = await client.patch(f"/api/v1/kanban/{nid}/move", headers=headers,
                           json={"kanban_status": "in_progress"})
    assert r.status_code == 200 and r.json()["kanban_status"] == "in_progress", r.text
    assert r.json()["status"] == "in_progress"
    assert r.json()["due_at"].startswith("2030-01-02T09:00:00")

    r = await client.get("/api/v1/kanban", headers=headers)
    board = r.json()
    assert board["todo"] == [] and [c["id"] for c in board["in_progress"]] == [nid]

    r = await client.patch(f"/api/v1/kanban/{nid}/move", headers=headers,
                           json={"kanban_status": "done", "due_at": None})
    assert r.status_code == 200 and r.json()["kanban_status"] == "done", r.text
    assert r.json()["status"] == "done" and r.json()["due_at"] is None

    r = await client.patch(f"/api/v1/kanban/{nid}/move", headers=headers,
                           json={"kanban_status": "todo", "due_date": target.isoformat()})
    assert r.status_code == 200 and r.json()["status"] == "open", r.text
    assert r.json()["due_at"].startswith("2030-01-02T09:00:00")
    assert "due_date" not in r.json()

    r = await client.patch(f"/api/v1/kanban/{nid}/move", headers=headers,
                           json={"kanban_status": "arquivada"})
    assert r.status_code == 422


async def test_move_requires_kanban_card(client, auth_headers, db_session):
    headers, uid = auth_headers
    # Notes are sources, never native task cards.
    note = Note(user_id=uuid.UUID(uid), raw_transcription="x", status=NoteStatus.PROCESSED)
    db_session.add(note)
    await db_session.commit()
    await db_session.refresh(note)
    r = await client.patch(f"/api/v1/kanban/{note.id}/move", headers=headers,
                           json={"kanban_status": "todo"})
    assert r.status_code == 404, r.text

    pending = await _task(db_session, uid, status="suggested")
    r = await client.patch(f"/api/v1/kanban/{pending}/move", headers=headers,
                           json={"kanban_status": "todo"})
    assert r.status_code == 409


# ---------------------------------------------------------- lembretes ---
async def _reminder_note(db_session, uid: str, **over):
    note = Note(
        user_id=uuid.UUID(uid), title="Ligar fornecedor", raw_transcription="ctx do áudio",
        status=NoteStatus.PROCESSED,
        reminder_at=datetime.now(timezone.utc) - timedelta(minutes=5),
        reminder_sent=False,
    )
    for k, v in over.items():
        setattr(note, k, v)
    db_session.add(note)
    await db_session.commit()
    await db_session.refresh(note)
    return note


async def test_reminder_fires_once_with_telegram(client, auth_headers, worker_db, monkeypatch):
    headers, uid = auth_headers
    monkeypatch.setattr(worker, "Bot", FakeBot)
    user_id = uuid.UUID(uid)
    token_secret = UserSecret(user_id=user_id, key="TELEGRAM_BOT_TOKEN", value="")
    chat_secret = UserSecret(user_id=user_id, key="TELEGRAM_CHAT_ID", value="")
    token_secret.value = encrypt_user_secret(
        user_id, token_secret.id, "bot-x", key=settings.USER_SECRET_ENCRYPTION_KEY,
    )
    chat_secret.value = encrypt_user_secret(
        user_id, chat_secret.id, "42", key=settings.USER_SECRET_ENCRYPTION_KEY,
    )
    worker_db.add(token_secret)
    worker_db.add(chat_secret)
    await worker_db.commit()
    note = await _reminder_note(worker_db, uid)

    res = await worker.check_scheduled_reminders({})
    assert res == {"ok": True, "sent": 1, "failed": 0, "skipped": 0}, res
    assert FakeBot.last is not None
    assert "Ligar fornecedor" in FakeBot.last["text"]
    assert "ctx do áudio" in FakeBot.last["text"]

    await worker_db.refresh(note)
    assert note.reminder_sent is True

    res = await worker.check_scheduled_reminders({})
    assert res["sent"] == 0  # sem reenvio


async def test_reminder_skips_future_and_unprocessed_and_fails_without_bot(worker_db, monkeypatch):
    import uuid as _uuid

    monkeypatch.setattr(worker, "Bot", FakeBot)
    uid = _uuid.uuid4()
    await _reminder_note(worker_db, str(uid),  # futura
                         reminder_at=datetime.now(timezone.utc) + timedelta(hours=1))
    await _reminder_note(worker_db, str(uid),  # não processada: fora da seleção
                         status=NoteStatus.PENDING_REVIEW)
    no_bot = await _reminder_note(worker_db, str(uid))  # sem segredos → failed
    res = await worker.check_scheduled_reminders({})
    assert res == {"ok": True, "sent": 0, "failed": 1, "skipped": 1}, res
    await worker_db.refresh(no_bot)
    assert no_bot.reminder_sent is False  # tenta de novo no próximo ciclo


async def test_worker_keeps_note_pending_review_without_transcription(worker_db, tmp_path, monkeypatch):
    import math as _math

    from app.services.stt import STTError

    class _BadSTT:
        async def transcribe(self, path):
            raise STTError("Whisper retornou transcrição vazia")

    monkeypatch.setattr(worker, "get_stt_provider", lambda: _BadSTT())

    uid = uuid.uuid4()
    carrier = Note(user_id=uid, raw_transcription="", status=NoteStatus.PENDING_REVIEW)
    worker_db.add(carrier)
    await worker_db.commit()
    await worker_db.refresh(carrier)
    nid = str(carrier.id)

    frames = bytearray()
    for i in range(8000 * 2):
        frames += int(0.5 * 32767 * _math.sin(2 * _math.pi * 440.0 * i / 8000)).to_bytes(2, "little", signed=True)
    path = tmp_path / "tom.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(bytes(frames))

    res = await worker.process_audio_job({}, nid, str(path), str(uid))
    assert res["ok"] is False  # sem transcrição o job falha, mas nunca apaga a nota

    note = await worker_db.get(Note, uuid.UUID(nid))
    assert note is not None and note.status == NoteStatus.PENDING_REVIEW

    logs = (await worker_db.exec(
        select(ExecutionLog).where(ExecutionLog.note_id == uuid.UUID(nid))
    )).all()
    assert len(logs) == 1
    assert logs[0].action_type == "pipeline" and logs[0].status == ExecutionStatus.FAILED


# ------------------------------------------------- nota geral → kanban ---
async def _general_note(db_session, uid: str, **over) -> str:
    note = Note(
        user_id=uuid.UUID(uid),
        title="Nota geral",
        raw_transcription="comprar água e pagar fornecedor amanhã",
        structured_data={
            "tipo": "nota_geral",
            "nota": "## Pendências\n- comprar água\n- pagar fornecedor",
            "tarefas": [
                {"title": "Comprar água", "due_date": None, "reminder_at": None},
                {"title": "Pagar fornecedor", "due_date": "2026-10-01T09:00:00+00:00", "reminder_at": None},
            ],
        },
        status=NoteStatus.PROCESSED,
    )
    for k, v in over.items():
        setattr(note, k, v)
    db_session.add(note)
    await db_session.commit()
    await db_session.refresh(note)
    return str(note.id)


async def test_general_note_to_kanban(client, auth_headers, db_session):
    headers, uid = auth_headers
    nid = await _general_note(db_session, uid)
    r = await client.post(f"/api/v1/notes/{nid}/to-kanban", headers=headers)
    assert r.status_code == 200, r.text
    assert r.json()["kanban_status"] == "todo"
    r = await client.get("/api/v1/kanban", headers=headers)
    assert len(r.json()["todo"]) == 1
    assert r.json()["todo"][0]["source_note_id"] == nid
    assert r.json()["todo"][0]["id"] != nid


async def test_general_tasks_to_kanban(client, auth_headers, db_session):
    headers, uid = auth_headers
    nid = await _general_note(db_session, uid)
    r = await client.post(f"/api/v1/notes/{nid}/tasks-to-kanban", headers=headers)
    assert r.status_code == 200, r.text
    cards = r.json()
    assert len(cards) == 2
    assert {c["title"] for c in cards} == {"Comprar água", "Pagar fornecedor"}
    assert all(c["kanban_status"] == "todo" for c in cards)
    r = await client.get("/api/v1/kanban", headers=headers)
    assert len(r.json()["todo"]) == 2


async def test_tasks_to_kanban_without_tasks_400(client, auth_headers, db_session):
    headers, uid = auth_headers
    note = Note(user_id=uuid.UUID(uid), raw_transcription="x",
                structured_data={"tipo": "nota_geral", "nota": "só texto", "tarefas": []},
                status=NoteStatus.PROCESSED)
    db_session.add(note)
    await db_session.commit()
    await db_session.refresh(note)
    r = await client.post(f"/api/v1/notes/{note.id}/tasks-to-kanban", headers=headers)
    assert r.status_code == 400, r.text
