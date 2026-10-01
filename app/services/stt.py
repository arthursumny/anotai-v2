"""AnotAI — STT: interface abstrata + Whisper real (OpenAI) + mock."""

from pathlib import Path
from typing import Protocol

from openai import AsyncOpenAI

from app.core.config import settings


class STTError(Exception):
    """Falha na transcrição."""


class STTProvider(Protocol):
    async def transcribe(self, audio_path: Path) -> str: ...


class OpenAIWhisperSTT:
    """STT real via OpenAI Whisper (`whisper-1`)."""

    def __init__(self, api_key: str | None = None, model: str | None = None) -> None:
        self._api_key = api_key or settings.OPENAI_API_KEY
        self._model = model or settings.OPENAI_STT_MODEL
        if not self._api_key:
            raise STTError("OPENAI_API_KEY não configurada para STT real")

    async def transcribe(self, audio_path: Path) -> str:
        if not audio_path.exists():
            raise STTError(f"Arquivo de áudio não encontrado: {audio_path}")
        try:
            client = AsyncOpenAI(api_key=self._api_key)
            with audio_path.open("rb") as fh:
                resp = await client.audio.transcriptions.create(
                    model=self._model,
                    file=(audio_path.name, fh),
                    response_format="text",
                )
            text = resp if isinstance(resp, str) else getattr(resp, "text", "")
            text = (text or "").strip()
            if not text:
                raise STTError("Whisper retornou transcrição vazia")
            return text
        except STTError:
            raise
        except Exception as exc:
            raise STTError(f"Falha no Whisper: {exc}") from exc


class FakeSTT:
    """Mock determinístico p/ dev/testes sem custo de API."""

    def __init__(self, text: str = "Consulta do Thor, tutor Carlos, canino com tosse há 3 dias.") -> None:
        self._text = text

    async def transcribe(self, audio_path: Path) -> str:
        if not audio_path.exists():
            raise STTError(f"Arquivo de áudio não encontrado: {audio_path}")
        return self._text


def get_stt_provider() -> STTProvider:
    if settings.STT_PROVIDER == "fake":
        return FakeSTT()
    return OpenAIWhisperSTT()
