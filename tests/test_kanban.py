"""AnotAI — testes do Always-On, inbox de triagem, Kanban e lembretes."""

import io
import uuid
import wave
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.core.user_secret_crypto import encrypt_user_secret
import app.worker as worker
from app.models.note import KanbanColumn, Note, NoteStatus
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


class _StubSTT:
    """STT determinístico: testes nunca chamam a API real (vale em qualquer env)."""

    def __init__(self, text: str) -> None:
        self._text = text

    async def transcribe(self, path) -> str:
        return self._text


def _wav_file(tmp_path) -> str:
    """WAV com tom audível (a barreira anti-silêncio rejeitaria zeros)."""
    import math

    path = tmp_path / "chunk.wav"
    frames = bytearray()
    for i in range(8000 * 2):
        sample = int(0.5 * 32767 * math.sin(2 * math.pi * 440.0 * i / 8000))
        frames += sample.to_bytes(2, "little", signed=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(bytes(frames))
    return str(path)


async def _carrier(db_session, uid: str):
    note = Note(user_id=uuid.UUID(uid), raw_transcription="", status=NoteStatus.PENDING_REVIEW,
                capture_mode="continuous")
    db_session.add(note)
    await db_session.commit()
    await db_session.refresh(note)
    return note


# ------------------------------------------------- ingestão contínua ---
async def test_continuous_ingestion_creates_suggestions(client, auth_headers, worker_db, tmp_path, monkeypatch):
    headers, uid = auth_headers
    monkeypatch.setattr(
        worker, "get_stt_provider",
        lambda: _StubSTT("preciso ligar pro fornecedor amanhã cedo, almoço dia 31 com Marcos"),
    )

    class _STT:
        async def transcribe(self, path):
            return "preciso ligar pro fornecedor amanhã cedo, almoço dia 31 com Marcos"

    monkeypatch.setattr(worker, "get_stt_provider", lambda: _STT())

    async def fake_analyze(transcription, user_context):
        assert "fornecedor" in transcription
        return [
            {"title": "Ligar pro fornecedor", "description": "Pedir orçamento",
             "suggested_due_date": "2026-10-01T09:00:00+00:00",
             "suggested_reminder_at": "2026-10-01T08:00:00+00:00",
             "suggested_action": "Lembrete Rápido"},
            {"title": "Almoço com Marcos", "description": "Dia 31 às 12h",
             "suggested_due_date": None, "suggested_reminder_at": None,
             "suggested_action": "Agendar Reunião"},
        ]

    monkeypatch.setattr(worker, "analyze_continuous_chunk", fake_analyze)
    carrier = await _carrier(worker_db, uid)
    res = await worker.process_audio_job({}, str(carrier.id), _wav_file(tmp_path), uid, "continuous")
    assert res["ok"] is True and len(res["suggestions"]) == 2, res

    r = await client.get("/api/v1/inbox/suggested", headers=headers)
    assert r.status_code == 200, r.text
    items = r.json()
    assert len(items) == 2
    first = next(i for i in items if i["title"] == "Ligar pro fornecedor")
    assert first["status"] == "suggested"
    assert first["capture_mode"] == "continuous"
    assert first["suggested_action"] == "Lembrete Rápido"
    assert first["due_date"].startswith("2026-10-01T09:00:00")
    assert first["reminder_at"].startswith("2026-10-01T08:00:00")
    assert first["kanban_status"] is None


async def test_continuous_without_items_creates_nothing(client, auth_headers, worker_db, tmp_path, monkeypatch):
    headers, uid = auth_headers
    monkeypatch.setattr(worker, "get_stt_provider", lambda: _StubSTT("blá blá blá conversa fiada sem ação"))

    async def fake_analyze(transcription, user_context):
        return []  # ruído / conversa fiada

    monkeypatch.setattr(worker, "analyze_continuous_chunk", fake_analyze)
    carrier = await _carrier(worker_db, uid)
    res = await worker.process_audio_job({}, str(carrier.id), _wav_file(tmp_path), uid, "continuous")
    assert res == {"ok": True, "status": "continuous", "suggestions": []}
    r = await client.get("/api/v1/inbox/suggested", headers=headers)
    assert r.json() == []
    r = await client.get("/api/v1/notes", headers=headers)
    assert r.json() == []  # portadora removida: nada polui


# ------------------------------------------------------------- inbox ---
async def _suggested(db_session, uid: str, title="Tarefa X", **over) -> str:
    note = Note(user_id=uuid.UUID(uid), title=title, raw_transcription="transc",
                status=NoteStatus.SUGGESTED, capture_mode="continuous",
                suggested_action="Criar Tarefa no Kanban", **over)
    db_session.add(note)
    await db_session.commit()
    await db_session.refresh(note)
    return str(note.id)


async def test_discard_suggestion(client, auth_headers, db_session):
    headers, uid = auth_headers
    nid = await _suggested(db_session, uid)
    r = await client.post(f"/api/v1/inbox/{nid}/discard", headers=headers)
    assert r.status_code == 200 and r.json()["status"] == "discarded", r.text
    r = await client.get("/api/v1/inbox/suggested", headers=headers)
    assert r.json() == []
    r = await client.post(f"/api/v1/inbox/{nid}/discard", headers=headers)
    assert r.status_code == 400  # só suggested pode ser descartada


async def test_accept_moves_to_kanban_todo(client, auth_headers, db_session):
    headers, uid = auth_headers
    nid = await _suggested(db_session, uid, title="Ligar fornecedor")
    r = await client.post(f"/api/v1/inbox/{nid}/accept", headers=headers,
                          json={"kanban_status": "todo", "due_date": "2026-10-02T09:00:00+00:00"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "processed" and body["kanban_status"] == "todo"
    assert body["due_date"].startswith("2026-10-02T09:00:00")

    r = await client.get("/api/v1/kanban", headers=headers)
    assert r.status_code == 200, r.text
    board = r.json()
    assert [c["id"] for c in board["todo"]] == [nid]
    assert board["in_progress"] == [] and board["done"] == []

    r = await client.post(f"/api/v1/inbox/{nid}/accept", headers=headers, json={})
    assert r.status_code == 400  # já saiu da inbox

    r = await client.post(f"/api/v1/inbox/{nid}/accept", headers=headers,
                          json={"kanban_status": "urgente"})
    assert r.status_code in (400, 422)


async def test_accept_defaults_to_todo(client, auth_headers, db_session):
    headers, uid = auth_headers
    nid = await _suggested(db_session, uid)
    r = await client.post(f"/api/v1/inbox/{nid}/accept", headers=headers)
    assert r.status_code == 200 and r.json()["kanban_status"] == "todo", r.text


# ------------------------------------------------------------ kanban ---
async def test_move_card_between_columns(client, auth_headers, db_session):
    headers, uid = auth_headers
    nid = await _suggested(db_session, uid)
    await client.post(f"/api/v1/inbox/{nid}/accept", headers=headers)

    r = await client.patch(f"/api/v1/kanban/{nid}/move", headers=headers,
                           json={"kanban_status": "in_progress"})
    assert r.status_code == 200 and r.json()["kanban_status"] == "in_progress", r.text

    r = await client.get("/api/v1/kanban", headers=headers)
    board = r.json()
    assert board["todo"] == [] and [c["id"] for c in board["in_progress"]] == [nid]

    r = await client.patch(f"/api/v1/kanban/{nid}/move", headers=headers,
                           json={"kanban_status": "done", "due_date": None})
    assert r.status_code == 200 and r.json()["kanban_status"] == "done", r.text

    r = await client.patch(f"/api/v1/kanban/{nid}/move", headers=headers,
                           json={"kanban_status": "arquivada"})
    assert r.status_code == 422


async def test_move_requires_kanban_card(client, auth_headers, db_session):
    headers, uid = auth_headers
    # Nota geral/processada fora do quadro ENTRA no Kanban pela coluna pedida.
    note = Note(user_id=uuid.UUID(uid), raw_transcription="x", status=NoteStatus.PROCESSED)
    db_session.add(note)
    await db_session.commit()
    await db_session.refresh(note)
    r = await client.patch(f"/api/v1/kanban/{note.id}/move", headers=headers,
                           json={"kanban_status": "todo"})
    assert r.status_code == 200, r.text
    assert r.json()["kanban_status"] == "todo"

    # Pendente de revisão ainda não pode entrar.
    pending = Note(user_id=uuid.UUID(uid), raw_transcription="y", status=NoteStatus.PENDING_REVIEW)
    db_session.add(pending)
    await db_session.commit()
    await db_session.refresh(pending)
    r = await client.patch(f"/api/v1/kanban/{pending.id}/move", headers=headers,
                           json={"kanban_status": "todo"})
    assert r.status_code == 400


# ---------------------------------------------------------- lembretes ---
async def _reminder_note(db_session, uid: str, **over):
    note = Note(
        user_id=uuid.UUID(uid), title="Ligar fornecedor", raw_transcription="ctx do áudio",
        status=NoteStatus.PROCESSED, kanban_status=KanbanColumn.TODO.value,
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


async def test_reminder_skips_done_and_future_and_missing_bot(worker_db, monkeypatch):
    import uuid as _uuid

    monkeypatch.setattr(worker, "Bot", FakeBot)
    uid = _uuid.uuid4()
    await _reminder_note(worker_db, str(uid), kanban_status=KanbanColumn.DONE.value)  # concluída
    await _reminder_note(worker_db, str(uid),  # futura
                         reminder_at=datetime.now(timezone.utc) + timedelta(hours=1))
    no_bot = await _reminder_note(worker_db, str(uid))  # sem segredos → failed
    res = await worker.check_scheduled_reminders({})
    assert res == {"ok": True, "sent": 0, "failed": 1, "skipped": 2}, res
    await worker_db.refresh(no_bot)
    assert no_bot.reminder_sent is False  # tenta de novo no próximo ciclo


async def test_worker_discards_note_without_transcription(worker_db, tmp_path, monkeypatch):
    import math as _math

    from app.services.stt import STTError

    class _BadSTT:
        async def transcribe(self, path):
            raise STTError("Whisper retornou transcrição vazia")

    monkeypatch.setattr(worker, "get_stt_provider", lambda: _BadSTT())

    uid = uuid.uuid4()
    carrier = Note(user_id=uid, raw_transcription="", status=NoteStatus.PENDING_REVIEW,
                   capture_mode="intentional")
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

    res = await worker.process_audio_job({}, nid, str(path), str(uid), "intentional")
    assert res["ok"] is False and res.get("discarded") is True
    assert await worker_db.get(Note, uuid.UUID(nid)) is None  # sem transcrição, sem nota


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
        capture_mode="intentional",
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
    assert [c["id"] for c in r.json()["todo"]] == [nid]


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
