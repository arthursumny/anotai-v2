"""AnotAI — testes da nota geral (substituta do antigo `no_match`)."""

import json
import uuid
from types import SimpleNamespace

import app.services.general_note as general_note
import app.worker as worker
from app.models.note import Note, NoteStatus
from app.services.general_note import GeneralNoteError, generate_general_note


def _fake_chat(payload: dict):
    async def _chat(client, **kwargs):
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))]
        )

    return _chat


async def test_generate_general_note_ok(monkeypatch):
    monkeypatch.setattr(
        general_note, "chat_create",
        _fake_chat({
            "title": "Teste de áudio",
            "body": "## Resumo\n- falou de água",
            "tasks": [{"title": "Comprar água"}],
            "due_date": None,
            "reminder_at": None,
            "send_to_kanban": True,
        }),
    )
    monkeypatch.setattr(general_note.settings, "OPENAI_API_KEY", "test-key")
    result = await generate_general_note("vamos testar se o áudio está funcionando", {})
    assert result["title"] == "Teste de áudio"
    assert result["body"] == "## Resumo\n- falou de água"
    assert result["tasks"] == [{"title": "Comprar água", "due_date": None, "reminder_at": None}]
    assert result["send_to_kanban"] is True


async def test_generate_general_note_no_key_returns_fallback(monkeypatch):
    """Sem chave: fallback local (nunca deixa a nota vazia)."""
    monkeypatch.setattr(general_note.settings, "OPENAI_API_KEY", "")
    result = await generate_general_note("alguma transcrição longa o suficiente aqui", {})
    assert result["title"]
    assert "alguma transcrição" in result["body"]
    assert result["tasks"] == []


async def test_generate_general_note_short_text():
    try:
        await generate_general_note("oi", {})
    except GeneralNoteError:
        pass
    else:
        raise AssertionError("deveria levantar GeneralNoteError")


async def test_worker_no_match_generates_general_note(worker_db, tmp_path, monkeypatch):
    """Sem template casando: vira nota geral PROCESSED (nunca 'sem match')."""
    import math as _math
    import wave as _wave

    async def fake_general(transcription, user_context):
        assert "fornecedor" in transcription
        return {
            "title": "Teste de áudio",
            "body": "## Resumo\n- teste de som",
            "tasks": [{"title": "Ligar fornecedor", "due_date": None, "reminder_at": None}],
            "due_date": None,
            "reminder_at": None,
            "send_to_kanban": False,
        }

    monkeypatch.setattr(worker, "generate_general_note", fake_general)

    class _STT:
        async def transcribe(self, path):
            return "vamos lá pra mais um teste com o fornecedor aqui"

    monkeypatch.setattr(worker, "get_stt_provider", lambda: _STT())
    monkeypatch.setattr(worker, "check_audible", lambda p: (True, "ok"))

    uid = uuid.uuid4()
    note = Note(user_id=uid, raw_transcription="", status=NoteStatus.PENDING_REVIEW,
                capture_mode="intentional")
    worker_db.add(note)
    await worker_db.commit()
    await worker_db.refresh(note)
    nid = str(note.id)

    frames = bytearray()
    for i in range(8000 * 2):
        frames += int(0.5 * 32767 * _math.sin(2 * _math.pi * 440.0 * i / 8000)).to_bytes(2, "little", signed=True)
    path = tmp_path / "tom.wav"
    with _wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(bytes(frames))

    # usuário sem templates → nota geral processada (template_id None)
    res = await worker.process_audio_job({}, nid, str(path), str(uid), "intentional")
    assert res["ok"] is True and res["status"] == "general", res
    await worker_db.refresh(note)
    assert note.title == "Teste de áudio"
    assert note.structured_data["tipo"] == "nota_geral"
    assert note.structured_data["nota"] == "## Resumo\n- teste de som"
    assert note.structured_data["tarefas"] == [
        {"title": "Ligar fornecedor", "due_date": None, "reminder_at": None}
    ]
    task_ids = note.structured_data["kanban_task_ids"]
    assert len(task_ids) == 1
    task = await worker_db.get(Note, uuid.UUID(task_ids[0]))
    assert task is not None and task.title == "Ligar fornecedor"
    assert task.kanban_status == "todo"
    assert note.status == NoteStatus.PROCESSED
    assert note.template_id is None


async def test_worker_general_note_goes_to_kanban_when_suggested(worker_db, tmp_path, monkeypatch):
    """IA marcou send_to_kanban: nota geral já nasce no Kanban + datas."""
    import math as _math
    import wave as _wave

    async def fake_general(transcription, user_context):
        return {
            "title": "Pagar fornecedor",
            "body": "## Pendências\n- pagar amanhã",
            "tasks": [{"title": "Pagar fornecedor", "due_date": "2026-10-01T09:00:00+00:00", "reminder_at": None}],
            "due_date": "2026-10-01T09:00:00+00:00",
            "reminder_at": None,
            "send_to_kanban": True,
        }

    monkeypatch.setattr(worker, "generate_general_note", fake_general)

    class _STT:
        async def transcribe(self, path):
            return "preciso pagar o fornecedor amanhã cedo sem falta"

    monkeypatch.setattr(worker, "get_stt_provider", lambda: _STT())
    monkeypatch.setattr(worker, "check_audible", lambda p: (True, "ok"))

    uid = uuid.uuid4()
    note = Note(user_id=uid, raw_transcription="", status=NoteStatus.PENDING_REVIEW,
                capture_mode="intentional")
    worker_db.add(note)
    await worker_db.commit()
    await worker_db.refresh(note)

    frames = bytearray()
    for i in range(8000 * 2):
        frames += int(0.5 * 32767 * _math.sin(2 * _math.pi * 440.0 * i / 8000)).to_bytes(2, "little", signed=True)
    path = tmp_path / "tom.wav"
    with _wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(bytes(frames))

    res = await worker.process_audio_job({}, str(note.id), str(path), str(uid), "intentional")
    assert res["status"] == "general", res
    await worker_db.refresh(note)
    assert note.kanban_status is None  # a tarefa individual, e não a nota inteira, foi para o quadro
    assert note.due_date is not None and note.due_date.year == 2026
    task_id = note.structured_data["kanban_task_ids"][0]
    task = await worker_db.get(Note, uuid.UUID(task_id))
    assert task is not None and task.kanban_status == "todo"
    assert task.reminder_at is not None  # aviso automático 30 min antes do prazo


async def test_worker_no_match_keeps_raw_when_llm_down(worker_db, tmp_path, monkeypatch):
    """Texto curto/LLM fora: ainda gera nota geral mínima (nunca vazia)."""
    import math as _math
    import wave as _wave

    from app.services.general_note import GeneralNoteError

    async def fake_down(transcription, user_context):
        raise GeneralNoteError("texto curto")

    monkeypatch.setattr(worker, "generate_general_note", fake_down)

    class _STT:
        async def transcribe(self, path):
            return "transcrição qualquer longa o suficiente para o teste"

    monkeypatch.setattr(worker, "get_stt_provider", lambda: _STT())
    monkeypatch.setattr(worker, "check_audible", lambda p: (True, "ok"))

    uid = uuid.uuid4()
    note = Note(user_id=uid, raw_transcription="", status=NoteStatus.PENDING_REVIEW,
                capture_mode="intentional")
    worker_db.add(note)
    await worker_db.commit()
    await worker_db.refresh(note)

    frames = bytearray()
    for i in range(8000 * 2):
        frames += int(0.5 * 32767 * _math.sin(2 * _math.pi * 440.0 * i / 8000)).to_bytes(2, "little", signed=True)
    path = tmp_path / "tom.wav"
    with _wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(bytes(frames))

    res = await worker.process_audio_job({}, str(note.id), str(path), str(uid), "intentional")
    assert res["ok"] is True and res["status"] == "general", res
    await worker_db.refresh(note)
    assert note.status == NoteStatus.PROCESSED
    assert note.title != ""
    assert (note.structured_data or {}).get("nota") != ""
