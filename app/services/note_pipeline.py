"""The single recording/document flow shared by processing and manual retries."""

import logging
from datetime import UTC, datetime

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.note import Note, NoteStatus
from app.models.template import Template
from app.models.user import User
from app.models.workspace import WorkspaceMembership
from app.services.document_pipeline import process_generated_note
from app.services.event_ingestion import EventCandidate, ingest
from app.services.extractor import ExtractionError, generate_note
from app.services.general_note import GeneralNoteError, generate_general_note
from app.services.router import RouterError, route_transcription
from app.services.trigger_executor import execute_template_tools
from app.services.workflow_engine import emit_trigger, prepare_template_actions

logger = logging.getLogger(__name__)


async def process_transcription(
    session: AsyncSession, note: Note, *, template: Template | None = None, regenerate: bool = False,
) -> None:
    """Persist a complete note/document before idempotently preparing its actions."""
    user = await session.get(User, note.user_id)
    if user is None or user.workspace_id is None:
        raise ValueError("Usuário sem espaço pessoal")
    member = (await session.exec(select(WorkspaceMembership.id).where(
        WorkspaceMembership.workspace_id == user.workspace_id,
        WorkspaceMembership.user_id == user.id,
    ))).first()
    if member is None:
        raise PermissionError("Usuário não pertence ao espaço da nota")
    if template is not None and template.user_id != note.user_id:
        raise PermissionError("Modelo não pertence ao usuário da nota")
    if not note.raw_transcription.strip():
        raise ValueError("Nota sem transcrição")
    if regenerate or note.status != NoteStatus.PROCESSED or not note.body:
        await ingest(
            EventCandidate(
                workspace_id=user.workspace_id,
                user_id=user.id,
                source="audio",
                type="AUDIO_TRANSCRIBED",
                occurred_at=note.created_at.replace(tzinfo=UTC)
                if note.created_at.tzinfo is None else note.created_at,
                content=note.raw_transcription,
                external_id=str(note.id),
            ),
            session,
        )
        if template is None:
            templates = list((await session.exec(
                select(Template).where(Template.user_id == note.user_id)
            )).all())
        else:
            templates = []
        try:
            if template is None:
                routing = await route_transcription(note.raw_transcription, templates)
                template = next((item for item in templates if item.id == routing.template_id), None)
            generated = await generate_note(note.raw_transcription, template) if template else (
                await generate_general_note(note.raw_transcription)
            )
            note.title = generated.title
            note.body = generated.body
            note.summary = generated.summary
            note.template_id = template.id if template else None
            note.structured_data = generated.data if template else {
                "tipo": "nota_geral", "nota": generated.body,
            }
            note.status = NoteStatus.PROCESSED
        except (GeneralNoteError, ExtractionError, RouterError) as exc:
            reason = str(exc)[:200]
            logger.warning(
                "Falha na geração via IA para a nota %s; salvando transcrição bruta: %s",
                note.id,
                reason,
            )
            first_line = next(
                (line.strip() for line in note.raw_transcription.splitlines() if line.strip()),
                "",
            )
            note.title = (
                first_line[:80]
                if first_line
                else f"Transcrição de {datetime.now().astimezone().strftime('%d/%m/%Y %H:%M')}"
            )
            note.body = note.raw_transcription
            note.summary = "Resumo indisponível: falha na IA"
            note.template_id = None
            note.structured_data = {
                "tipo": "transcricao_bruta",
                "motivo": reason,
            }
            note.status = NoteStatus.PROCESSED
        session.add(note)
        await session.flush()
    await process_generated_note(session, note.id, note.user_id)
    await session.commit()
    await prepare_template_actions(session, note, note.user_id)
    await session.commit()
    # A execução das ferramentas de integração é best-effort: uma falha de
    # tool/LLM/credencial NUNCA pode quebrar o fluxo gravação→nota.
    if note.template_id is not None:
        try:
            await execute_template_tools(
                session,
                user_id=note.user_id,
                template_id=note.template_id,
                transcription=note.raw_transcription,
                note_body=note.body or "",
            )
        except Exception:
            logger.exception("Falha ao executar as ferramentas de integração da nota")
    # O trigger de fluxo "nota pronta" também é best-effort: a nota já foi
    # persistida e processada, então uma falha de fluxo NUNCA pode desfazê-la.
    if note.status == NoteStatus.PROCESSED:
        try:
            trigger_user = await session.get(User, note.user_id)
            if trigger_user is not None and trigger_user.workspace_id is not None:
                context = {"event_type": "DOCUMENT_GENERATED"}
                # Filtro "nota do modelo X": o id do modelo vai no context["source"],
                # que é o que _condition_matches compara. O source_id continua sendo
                # a nota (chave de idempotência da execução).
                if note.template_id is not None:
                    context["source"] = str(note.template_id)
                created = await emit_trigger(
                    session,
                    trigger_user.workspace_id,
                    note.user_id,
                    "DOCUMENT_GENERATED",
                    source_id=note.id,
                    context=context,
                )
                if created:
                    await session.commit()
        except Exception:
            logger.exception("Falha ao emitir o trigger de fluxo da nota %s", note.id)
