"""STT Soniox: contrato HTTP real sem rede, credenciais ou esperas."""

import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace

import anyio
import httpx
import pytest

from app.services import stt
from app.services.stt import FakeSTT, SonioxSTT, STTError

FILE_ID = "00000000-0000-0000-0000-000000000001"
TRANSCRIPTION_ID = "00000000-0000-0000-0000-000000000002"
FILE_PATH = f"/v1/files/{FILE_ID}"
JOB_PATH = f"/v1/transcriptions/{TRANSCRIPTION_ID}"


@pytest.fixture
def audio_path(tmp_path: Path) -> Path:
    path = tmp_path / "recording.ogg"
    path.write_bytes(b"test-audio")
    return path


def provider(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    poll_wait: Callable[[float], Awaitable[None]] = anyio.sleep,
) -> SonioxSTT:
    return SonioxSTT(
        api_key="test-key",
        model="stt-async-v5",
        base_url="https://soniox.test",
        timeout_seconds=120,
        poll_interval_seconds=1,
        transport=httpx.MockTransport(handler),
        poll_wait=poll_wait,
    )


def completed_response(request: httpx.Request) -> httpx.Response:
    match request.method, request.url.path:
        case "POST", "/v1/files":
            return httpx.Response(201, json={"id": FILE_ID})
        case "POST", "/v1/transcriptions":
            return httpx.Response(201, json={"id": TRANSCRIPTION_ID})
        case "GET", path if path == JOB_PATH:
            return httpx.Response(200, json={"status": "completed"})
        case "GET", path if path == f"{JOB_PATH}/transcript":
            return httpx.Response(200, json={"text": "  Consulta completa.  ", "tokens": []})
        case "DELETE", path if path in {JOB_PATH, FILE_PATH}:
            return httpx.Response(204)
        case _:
            pytest.fail(f"Unexpected HTTP request: {request.method} {request.url.path}")


@pytest.mark.asyncio
async def test_transcribe_uploads_full_audio_and_retrieves_final_text(audio_path: Path):
    # Given
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return completed_response(request)

    # When
    result = await provider(handler).transcribe(audio_path)

    # Then
    assert result == "Consulta completa."
    assert [(r.method, r.url.path) for r in requests] == [
        ("POST", "/v1/files"), ("POST", "/v1/transcriptions"),
        ("GET", JOB_PATH), ("GET", f"{JOB_PATH}/transcript"),
        ("DELETE", JOB_PATH), ("DELETE", FILE_PATH),
    ]
    assert all(r.headers["authorization"] == "Bearer test-key" for r in requests)
    assert all(r.url.host == "soniox.test" for r in requests)
    assert requests[0].headers["content-type"].startswith("multipart/form-data; boundary=")
    assert b'name="file"; filename="recording.ogg"' in requests[0].content
    assert b"test-audio" in requests[0].content
    assert json.loads(requests[1].content) == {
        "model": "stt-async-v5",
        "file_id": FILE_ID,
        "enable_speaker_diarization": True,
        "language_hints": ["pt", "en"],
    }


@pytest.mark.asyncio
async def test_polling_uses_status_sequence_without_test_sleep(audio_path: Path):
    # Given: exact scheduling seam advances the next server state immediately.
    statuses = iter(["queued", "processing", "completed"])
    waits: list[float] = []

    async def advance_status(interval: float) -> None:
        waits.append(interval)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == JOB_PATH:
            return httpx.Response(200, json={"status": next(statuses)})
        return completed_response(request)

    adapter = provider(handler, poll_wait=advance_status)

    # When
    result = await adapter.transcribe(audio_path)

    # Then
    assert result == "Consulta completa."
    assert waits == [1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "status_code", "body", "deleted"),
    [
        ("/v1/files", 401, {"error": "test-key"}, []),
        ("/v1/files", 200, {"id": ""}, []),
        ("/v1/transcriptions", 503, {"error": "test-key"}, [FILE_PATH]),
        ("/v1/transcriptions", 200, {"id": ""}, [FILE_PATH]),
        (JOB_PATH, 200, {"status": "error", "error_message": "test-key"}, [JOB_PATH, FILE_PATH]),
        (JOB_PATH, 200, {"status": "unexpected"}, [JOB_PATH, FILE_PATH]),
        (JOB_PATH, 429, {"error": "test-key"}, [JOB_PATH, FILE_PATH]),
        (f"{JOB_PATH}/transcript", 200, {"text": " \n "}, [JOB_PATH, FILE_PATH]),
        (f"{JOB_PATH}/transcript", 200, {"tokens": []}, [JOB_PATH, FILE_PATH]),
        (f"{JOB_PATH}/transcript", 500, {"error": "test-key"}, [JOB_PATH, FILE_PATH]),
    ],
)
async def test_error_never_returns_fake_and_cleans_acquired_resources(
    audio_path: Path, stage: str, status_code: int,
    body: dict[str, str | list[str]], deleted: list[str]
):
    # Given
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method != "DELETE" and request.url.path == stage:
            return httpx.Response(status_code, json=body)
        return completed_response(request)

    # When
    with pytest.raises(STTError) as error:
        await provider(handler).transcribe(audio_path)

    # Then
    assert "test-key" not in str(error.value)
    assert [r.url.path for r in requests if r.method == "DELETE"] == deleted


@pytest.mark.asyncio
async def test_invalid_json_cleans_file_after_create_failure(audio_path: Path):
    # Given
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/transcriptions":
            return httpx.Response(200, content=b"not-json")
        if request.method == "DELETE":
            deleted.append(request.url.path)
        return completed_response(request)

    # When
    with pytest.raises(STTError, match="ValidationError"):
        await provider(handler).transcribe(audio_path)

    # Then
    assert deleted == [FILE_PATH]


@pytest.mark.asyncio
async def test_transport_timeout_cleans_both_resources(audio_path: Path):
    # Given
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            raise httpx.ReadTimeout("timeout", request=request)
        if request.method == "DELETE":
            deleted.append(request.url.path)
        return completed_response(request)

    # When
    with pytest.raises(STTError, match="ReadTimeout"):
        await provider(handler).transcribe(audio_path)

    # Then
    assert deleted == [JOB_PATH, FILE_PATH]


@pytest.mark.asyncio
async def test_operation_deadline_is_translated_and_cleaned_without_waiting(audio_path: Path):
    # Given: emulate the bounded poll scheduler's deadline, not wall-clock timing.
    deleted: list[str] = []

    async def deadline_reached(interval: float) -> None:
        raise TimeoutError

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"status": "processing"})
        if request.method == "DELETE":
            deleted.append(request.url.path)
        return completed_response(request)

    adapter = provider(handler, poll_wait=deadline_reached)

    # When
    with pytest.raises(STTError, match="TimeoutError"):
        await adapter.transcribe(audio_path)

    # Then
    assert deleted == [JOB_PATH, FILE_PATH]


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_path", [JOB_PATH, FILE_PATH])
async def test_cleanup_failure_attempts_other_resource_and_never_succeeds(
    audio_path: Path, fail_path: str
):
    # Given
    deleted: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            deleted.append(request.url.path)
            if request.url.path == fail_path:
                return httpx.Response(400, json={"error": "still processing"})
        return completed_response(request)

    # When
    with pytest.raises(STTError, match="limpeza") as error:
        await provider(handler).transcribe(audio_path)

    # Then
    assert deleted == [JOB_PATH, FILE_PATH]
    assert error.value.transcription_id == TRANSCRIPTION_ID
    assert error.value.file_id == FILE_ID


@pytest.mark.asyncio
async def test_processing_timeout_preserves_error_and_remote_id_for_later_cleanup(audio_path: Path):
    # Given
    deleted: list[str] = []

    async def deadline_reached(interval: float) -> None:
        raise TimeoutError

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"status": "processing"})
        if request.method == "DELETE":
            deleted.append(request.url.path)
            if request.url.path == JOB_PATH:
                return httpx.Response(400, json={"error": "cannot delete processing job"})
        return completed_response(request)

    adapter = provider(handler, poll_wait=deadline_reached)

    # When
    with pytest.raises(STTError, match="TimeoutError") as error:
        await adapter.transcribe(audio_path)

    # Then
    assert deleted == [JOB_PATH, FILE_PATH]
    assert error.value.transcription_id == TRANSCRIPTION_ID
    assert "limpeza" in error.value.__notes__[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("cleanup_status", [204, 400])
async def test_cancelled_operation_still_cleans_acquired_resources(
    audio_path: Path, cleanup_status: int
):
    # Given: cancellation is triggered by the exact GET request event.
    deleted: list[str] = []
    with anyio.CancelScope() as scope:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                scope.cancel()
                await anyio.lowlevel.checkpoint()
            if request.method == "DELETE":
                deleted.append(request.url.path)
                if request.url.path == JOB_PATH:
                    return httpx.Response(cleanup_status)
            return completed_response(request)

        adapter = SonioxSTT(
            api_key="test-key", model="stt-async-v5", base_url="https://soniox.test",
            timeout_seconds=120, poll_interval_seconds=1,
            transport=httpx.MockTransport(handler),
        )

        # When
        await adapter.transcribe(audio_path)

    # Then
    assert scope.cancelled_caught
    assert deleted == [JOB_PATH, FILE_PATH]


@pytest.mark.asyncio
async def test_missing_local_file_never_makes_request(tmp_path: Path):
    # Given
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return completed_response(request)

    # When
    with pytest.raises(STTError, match="não encontrado"):
        await provider(handler).transcribe(tmp_path / "missing.ogg")

    # Then
    assert requests == []


@pytest.mark.parametrize("environment", ["dev", "test", "prod", "production"])
def test_fake_factory_is_explicit_and_only_allowed_in_dev_test(monkeypatch, environment: str):
    # Given
    monkeypatch.setattr(stt, "settings", SimpleNamespace(STT_PROVIDER="fake", ENV=environment))

    # When / Then
    if environment in {"dev", "test"}:
        assert isinstance(stt.get_stt_provider(), FakeSTT)
    else:
        with pytest.raises(STTError, match="dev/testes"):
            stt.get_stt_provider()


def test_factory_defaults_to_soniox_configuration_and_requires_its_key(monkeypatch):
    # Given
    config = SimpleNamespace(
        STT_PROVIDER="soniox", ENV="prod", SONIOX_API_KEY="test-key",
        SONIOX_STT_MODEL="custom-model", SONIOX_API_BASE_URL="https://soniox.test",
        SONIOX_STT_TIMEOUT_SECONDS=25, SONIOX_STT_POLL_INTERVAL_SECONDS=2,
        SONIOX_ENABLE_DIARIZATION=True, SONIOX_LANGUAGE_HINTS="pt,en",
    )
    monkeypatch.setattr(stt, "settings", config)

    # When
    adapter = stt.get_stt_provider()

    # Then
    assert isinstance(adapter, SonioxSTT)
    assert adapter._model == "custom-model"
    assert adapter._timeout == 25
    assert adapter._poll_interval == 2
    assert adapter._enable_diarization is True
    assert adapter._language_hints == ["pt", "en"]
    config.SONIOX_API_KEY = ""
    with pytest.raises(STTError, match="SONIOX_API_KEY"):
        stt.get_stt_provider()


def test_unknown_provider_never_falls_back(monkeypatch):
    # Given
    monkeypatch.setattr(stt, "settings", SimpleNamespace(STT_PROVIDER="openai", ENV="prod"))

    # When / Then
    with pytest.raises(STTError, match="não suportado"):
        stt.get_stt_provider()


@pytest.mark.asyncio
async def test_transcribe_formats_tokens_with_alternating_speakers(audio_path: Path):
    # Given: tokens com speakers alternados
    transcript_payload = {
        "text": "Texto bruto ignorado quando tokens com speaker estão presentes",
        "tokens": [
            {"text": "  Olá ", "speaker": "1"},
            {"text": "doutor,", "speaker": "1"},
            {"text": "  tudo ", "speaker": "1"},
            {"text": "bem? ", "speaker": "1"},
            {"text": " Tudo", "speaker": "2"},
            {"text": " ótimo, ", "speaker": "2"},
            {"text": "como ", "speaker": "2"},
            {"text": "  vai? ", "speaker": "2"},
            {"text": " Vou", "speaker": "1"},
            {"text": " muito", "speaker": "1"},
            {"text": " bem.", "speaker": "1"},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"{JOB_PATH}/transcript":
            return httpx.Response(200, json=transcript_payload)
        return completed_response(request)

    # When
    result = await provider(handler).transcribe(audio_path)

    # Then
    expected = (
        "Falante 1: Olá doutor, tudo bem?\n\n"
        "Falante 2: Tudo ótimo, como vai?\n\n"
        "Falante 1: Vou muito bem."
    )
    assert result == expected


@pytest.mark.asyncio
async def test_transcribe_tokens_without_speaker_falls_back_to_text(audio_path: Path):
    # Given: tokens sem speaker caem no fallback usando o campo text
    transcript_payload = {
        "text": "  Transcrição original usando fallback do campo text.  ",
        "tokens": [
            {"text": "Transcrição", "speaker": None},
            {"text": " original", "speaker": None},
            {"text": " usando"},
            {"text": " fallback"},
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == f"{JOB_PATH}/transcript":
            return httpx.Response(200, json=transcript_payload)
        return completed_response(request)

    # When
    result = await provider(handler).transcribe(audio_path)

    # Then
    assert result == "Transcrição original usando fallback do campo text."
