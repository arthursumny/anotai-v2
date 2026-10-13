"""The single recording/document flow shared by processing and manual retries."""

from datetime import timezone

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.note import Note, NoteStatus
from app.models.template import Template
from app.models.user import User
from app.models.workspace import WorkspaceMembership
from app.services.document_pipeline import process_generated_note
from app.services.event_ingestion import EventCandidate, ingest
from app.services.extractor import generate_note
from app.services.general_note import generate_general_note
from app.services.router import route_transcription
from app.services.workflow_engine import prepare_template_actions


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
                occurred_at=note.created_at.replace(tzinfo=timezone.utc)
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
        session.add(note)
        await session.flush()
    await process_generated_note(session, note.id, note.user_id)
    await session.commit()
    await prepare_template_actions(session, note, note.user_id)
    await session.commit()
