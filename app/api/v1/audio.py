"""AnotAI — ingestão de áudio: upload -> Note pending -> job ARQ (202 Accepted)."""

import asyncio
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.core.database import get_session
from app.core.deps import get_current_user
from app.core.queue import enqueue_audio_job
from app.models.note import Note, NoteStatus
from app.models.user import User
from app.schemas.note import AudioProcessAccepted
from app.services.audio_check import check_audible

router = APIRouter()

_SUFFIX_BY_CONTENT_TYPE = {
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "audio/webm": ".webm",
    "audio/m4a": ".m4a",
    "audio/mp4": ".m4a",
    "video/mp4": ".mp4",
}


@router.post("/process", response_model=AudioProcessAccepted, status_code=status.HTTP_202_ACCEPTED)
async def process_audio(
    file: UploadFile = File(...),
    capture_mode: str = Form("intentional"),
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> AudioProcessAccepted:
    mode = (capture_mode or "").strip().lower()
    if mode not in ("intentional", "continuous"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="capture_mode deve ser 'intentional' ou 'continuous'",
        )
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type not in settings.allowed_audio_types_set:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Tipo de áudio não suportado: {content_type or 'desconhecido'}",
        )

    upload_dir = Path(settings.AUDIO_UPLOAD_DIR)
    upload_dir.mkdir(parents=True, exist_ok=True)

    suffix = _SUFFIX_BY_CONTENT_TYPE.get(content_type, Path(file.filename or "").suffix or ".bin")
    tmp_path = upload_dir / f"{uuid.uuid4().hex}{suffix}"

    try:
        size = 0
        with tmp_path.open("wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > settings.max_audio_bytes:
                    out.close()
                    tmp_path.unlink(missing_ok=True)
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail=f"Áudio excede {settings.MAX_AUDIO_MB}MB",
                    )
                out.write(chunk)
        if size == 0:
            tmp_path.unlink(missing_ok=True)
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Arquivo de áudio vazio")
    finally:
        await file.close()

    # Barreira de audibilidade: silêncio/mudo nem cria nota nem gasta Whisper.
    audible, reason = await asyncio.to_thread(check_audible, tmp_path)
    if not audible:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Sem som aproveitável: {reason}")

    note = Note(user_id=current.id, raw_transcription="", structured_data=None, status=NoteStatus.PENDING_REVIEW, capture_mode=mode)
    session.add(note)
    await session.commit()
    await session.refresh(note)

    try:
        job_id = await enqueue_audio_job(str(note.id), str(tmp_path), str(current.id), mode)
    except RuntimeError as exc:
        # Sem job não há nota: apaga o placeholder em vez de órfão pendente.
        await session.delete(note)
        await session.commit()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Falha ao enfileirar processamento (Redis): {exc}",
        ) from exc

    return AudioProcessAccepted(note_id=note.id, job_id=job_id)
