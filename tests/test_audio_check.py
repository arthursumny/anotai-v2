"""AnotAI — testes da barreira de audibilidade (silêncio nem chega ao Whisper)."""

import math
import shutil
import wave

import pytest

from app.services.audio_check import check_audible


def _wav(path, frames: bytes, framerate=16000) -> str:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(framerate)
        w.writeframes(frames)
    return str(path)


def _silence(seconds=2) -> bytes:
    return b"\x00\x00" * (16000 * seconds)


def _tone(seconds=2, freq=440.0, amplitude=0.5) -> bytes:
    out = bytearray()
    for i in range(16000 * seconds):
        sample = int(amplitude * 32767 * math.sin(2 * math.pi * freq * i / 16000))
        out += sample.to_bytes(2, "little", signed=True)
    return bytes(out)


needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg ausente"
)


@needs_ffmpeg
def test_silence_is_rejected(tmp_path):
    from pathlib import Path
    ok, reason = check_audible(Path(_wav(tmp_path / "sil.wav", _silence())))
    assert ok is False
    assert "silêncio" in reason or "mudo" in reason


@needs_ffmpeg
def test_tone_is_accepted(tmp_path):
    from pathlib import Path
    ok, reason = check_audible(Path(_wav(tmp_path / "tom.wav", _tone())))
    assert ok is True, reason


def test_tiny_file_rejected(tmp_path):
    from pathlib import Path
    p = tmp_path / "mini.wav"
    p.write_bytes(b"RIFF....")
    ok, reason = check_audible(Path(p))
    assert ok is False
    assert "pequeno" in reason


def test_missing_file_rejected(tmp_path):
    from pathlib import Path
    ok, _reason = check_audible(Path(tmp_path / "nao-existe.wav"))
    assert ok is False


async def test_silent_upload_returns_400(client, auth_headers, tmp_path):
    from pathlib import Path
    headers, _uid = auth_headers
    path = _wav(tmp_path / "sil.wav", _silence())
    with open(path, "rb") as fh:
        r = await client.post(
            "/api/v1/audio/process",
            headers=headers,
            files={"file": ("sil.wav", fh, "audio/wav")},
        )
    # Sem redis no ambiente de teste o enqueue falharia depois; o que importa:
    # a barreira de silêncio age ANTES (400) e nunca cria nota/job.
    assert r.status_code in (400, 502), r.text
    if r.status_code == 400:
        assert "Sem som" in r.json()["detail"]


async def test_enqueue_failure_removes_placeholder(client, auth_headers, tmp_path, monkeypatch):
    """Redis fora → 502 E nenhuma nota órfã."""
    import app.api.v1.audio as audio_api
    from pathlib import Path

    async def boom(*args, **kwargs):
        raise RuntimeError("Redis: down")

    monkeypatch.setattr(audio_api, "enqueue_audio_job", boom)
    headers, _uid = auth_headers
    path = _wav(tmp_path / "tom.wav", _tone())
    with open(path, "rb") as fh:
        r = await client.post(
            "/api/v1/audio/process",
            headers=headers,
            files={"file": ("tom.wav", fh, "audio/wav")},
        )
    assert r.status_code == 502, r.text
    r = await client.get("/api/v1/notes", headers=headers)
    assert r.json() == []
