"""AnotAI — STT via Soniox assíncrono; fake exclusivo para dev/testes."""

import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal, Protocol, assert_never
from uuid import UUID

import anyio
import httpx
from pydantic import BaseModel, ConfigDict, ValidationError

from app.core.config import settings


class STTError(Exception):
    """Falha na transcrição."""

    def __init__(
        self,
        message: str,
        *,
        transcription_id: str | None = None,
        file_id: str | None = None,
    ) -> None:
        super().__init__(message)
        # IDs permitem limpeza posterior se Soniox ainda estiver processando.
        self.transcription_id = transcription_id
        self.file_id = file_id


class STTProvider(Protocol):
    async def transcribe(self, audio_path: Path) -> str: ...


class _RemoteResource(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: UUID


class _TranscriptionStatus(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: Literal["queued", "processing", "completed", "error"]


class _Transcript(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str


class SonioxSTT:
    """Upload, transcrição gravada e polling limitado via REST, sem fallback."""

    def __init__(
        self,
        api_key: str | None = None,
        model: str | None = None,
        *,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        poll_wait: Callable[[float], Awaitable[None]] = anyio.sleep,
    ) -> None:
        self._api_key = settings.SONIOX_API_KEY if api_key is None else api_key
        self._model = settings.SONIOX_STT_MODEL if model is None else model
        self._base_url = settings.SONIOX_API_BASE_URL if base_url is None else base_url
        self._timeout = (
            settings.SONIOX_STT_TIMEOUT_SECONDS if timeout_seconds is None else timeout_seconds
        )
        self._poll_interval = (
            settings.SONIOX_STT_POLL_INTERVAL_SECONDS
            if poll_interval_seconds is None else poll_interval_seconds
        )
        self._transport = transport
        self._poll_wait = poll_wait
        if not self._api_key:
            raise STTError("SONIOX_API_KEY não configurada para STT real")
        if self._timeout <= 0 or self._poll_interval <= 0:
            raise STTError("Timeout e intervalo de polling Soniox devem ser positivos")

    async def transcribe(self, audio_path: Path) -> str:
        if not audio_path.is_file():
            raise STTError(f"Arquivo de áudio não encontrado: {audio_path}")
        file_id: str | None = None
        transcription_id: str | None = None
        error: STTError | None = None
        async with httpx.AsyncClient(
            base_url=self._base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {self._api_key}"},
            timeout=min(self._timeout, 30.0),
            transport=self._transport,
        ) as client:
            try:
                with anyio.fail_after(self._timeout):
                    with audio_path.open("rb") as fh:
                        response = await client.post(
                            "v1/files", files={"file": (audio_path.name, fh)}
                        )
                    response.raise_for_status()
                    file_id = str(_RemoteResource.model_validate_json(response.content).id)
                    response = await client.post(
                        "v1/transcriptions", json={"model": self._model, "file_id": file_id}
                    )
                    response.raise_for_status()
                    transcription_id = str(_RemoteResource.model_validate_json(response.content).id)
                    while True:
                        response = await client.get(f"v1/transcriptions/{transcription_id}")
                        response.raise_for_status()
                        status = _TranscriptionStatus.model_validate_json(response.content).status
                        match status:
                            case "completed":
                                break
                            case "error":
                                # Não refletir corpo remoto: pode conter dados sensíveis.
                                raise STTError("Soniox retornou erro na transcrição")
                            case "queued" | "processing":
                                await self._poll_wait(self._poll_interval)
                            case unreachable:
                                assert_never(unreachable)
                    response = await client.get(f"v1/transcriptions/{transcription_id}/transcript")
                    response.raise_for_status()
                    text = _Transcript.model_validate_json(response.content).text.strip()
                    if not text:
                        raise STTError("Soniox retornou transcrição vazia")
                    return text
            except STTError as exc:
                error = exc
                raise
            except (TimeoutError, httpx.HTTPError, OSError, ValidationError) as exc:
                error = STTError(
                    f"Falha no Soniox ({type(exc).__name__})",
                    transcription_id=transcription_id,
                    file_id=file_id,
                )
                raise error from exc
            finally:
                pending_error = sys.exception()
                cleanup_failed = False
                for resource, resource_id in (
                    ("transcriptions", transcription_id), ("files", file_id)
                ):
                    if resource_id is None:
                        continue
                    try:
                        # Limite por recurso; tentar o arquivo mesmo se o job não puder ser removido.
                        with anyio.fail_after(min(self._timeout, 10.0), shield=True):
                            response = await client.delete(f"v1/{resource}/{resource_id}")
                            if response.status_code != 404:
                                response.raise_for_status()
                    except (TimeoutError, httpx.HTTPError):
                        cleanup_failed = True
                if cleanup_failed:
                    cleanup_error = STTError(
                        "Falha na limpeza remota Soniox; recursos exigem limpeza posterior",
                        transcription_id=transcription_id,
                        file_id=file_id,
                    )
                    if error is not None:
                        error.transcription_id = transcription_id
                        error.file_id = file_id
                        error.add_note(str(cleanup_error))
                    elif pending_error is not None:
                        pending_error.add_note(
                            f"{cleanup_error}; transcription_id={transcription_id}; file_id={file_id}"
                        )
                    else:
                        raise cleanup_error


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
        if settings.ENV not in {"dev", "test"}:
            raise STTError("STT fake permitido apenas em dev/testes")
        return FakeSTT()
    if settings.STT_PROVIDER == "soniox":
        return SonioxSTT()
    raise STTError("Provedor STT não suportado")
