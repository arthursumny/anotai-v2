"""Owned documents, explicit corrections, retries, and native task creation."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from jsonschema import Draft202012Validator, SchemaError, ValidationError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.database import get_session
from app.core.deps import get_current_user
from app.models.action_item import Task
from app.models.execution_log import ExecutionLog, ExecutionStatus
from app.models.note import Note, NoteStatus
from app.models.template import Template
from app.models.user import User
from app.models.workspace import WorkspaceMembership
from app.schemas.note import ExecutionLogRead, NoteRead, NoteUpdate, ReclassifyRequest
from app.schemas.task import TaskRead
from app.services.document_pipeline import process_generated_note
from app.services.note_pipeline import process_transcription
from app.services.workflow_engine import prepare_template_actions

router = APIRouter()


async def _get_owned_note(session: AsyncSession, note_id: uuid.UUID, user_id: uuid.UUID) -> Note:
    note = await session.get(Note, note_id)
    if note is None or note.user_id != user_id:
        raise HTTPException(status_code=404, detail="Nota não encontrada")
    return note


async def _get_accessible_template(
    session: AsyncSession, template_id: uuid.UUID, user_id: uuid.UUID,
) -> Template:
    template = await session.get(Template, template_id)
    if template is None or template.user_id != user_id:
        raise HTTPException(status_code=404, detail="Modelo não encontrado")
    return template


def _validate_against_schema(structured: dict, schema: dict) -> None:
    try:
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(structured)
    except (SchemaError, ValidationError) as exc:
        raise HTTPException(status_code=422, detail=f"Dados fora do schema: {exc.message}") from exc


async def _note_with_logs(session: AsyncSession, note_id: uuid.UUID, user_id: uuid.UUID) -> NoteRead:
    note = await _get_owned_note(session, note_id, user_id)
    logs = (await session.exec(
        select(ExecutionLog).where(ExecutionLog.note_id == note_id)
        .order_by(ExecutionLog.created_at.asc())
    )).all()
    data = NoteRead.model_validate(note)
    data.logs = [ExecutionLogRead.model_validate(item) for item in logs]
    return data


@router.get("", response_model=list[NoteRead])
async def list_notes(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> list[NoteRead]:
    notes = (await session.exec(
        select(Note).where(Note.user_id == current.id, Note.status != NoteStatus.DISCARDED)
        .order_by(Note.created_at.desc()).offset(offset).limit(limit)
    )).all()
    return [await _note_with_logs(session, note.id, current.id) for note in notes]


@router.get("/{note_id}", response_model=NoteRead)
async def get_note(
    note_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> NoteRead:
    return await _note_with_logs(session, note_id, current.id)


async def _generate(
    session: AsyncSession, note: Note, template: Template | None = None,
) -> NoteRead:
    try:
        await process_transcription(session, note, template=template, regenerate=True)
    except Exception as exc:
        # HTTP generation boundary; preserve the previous saved document on failure.
        await session.rollback()
        await session.refresh(note)
        session.add(ExecutionLog(
            note_id=note.id, action_type="generation", status=ExecutionStatus.FAILED,
            payload={}, error_message=str(exc)[:4000],
        ))
        await session.commit()
        raise HTTPException(status_code=502, detail="Não foi possível gerar a nota; transcrição preservada") from exc
    return await _note_with_logs(session, note.id, note.user_id)


@router.post("/{note_id}/retry", response_model=NoteRead)
async def retry_note(
    note_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> NoteRead:
    note = await _get_owned_note(session, note_id, current.id)
    if not note.raw_transcription.strip():
        raise HTTPException(status_code=400, detail="Envie o áudio novamente; não há transcrição salva")
    if note.status == NoteStatus.PROCESSED and note.body:
        await prepare_template_actions(session, note, current.id)
        await session.commit()
        return await _note_with_logs(session, note.id, current.id)
    return await _generate(session, note)


@router.post("/{note_id}/reclassify", response_model=NoteRead)
async def reclassify_note(
    note_id: uuid.UUID,
    data: ReclassifyRequest,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> NoteRead:
    note = await _get_owned_note(session, note_id, current.id)
    template = await _get_accessible_template(session, data.template_id, current.id)
    if not note.raw_transcription.strip():
        raise HTTPException(status_code=400, detail="Nota sem transcrição para reclassificar")
    return await _generate(session, note, template)


@router.patch("/{note_id}", response_model=NoteRead)
async def update_note(
    note_id: uuid.UUID,
    data: NoteUpdate,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> NoteRead:
    note = await _get_owned_note(session, note_id, current.id)
    patch = data.model_dump(exclude_unset=True)
    template_id = patch.get("template_id", note.template_id)
    template = await _get_accessible_template(session, template_id, current.id) if template_id else None
    structured = patch.get("structured_data", note.structured_data)
    if template and structured is not None:
        _validate_against_schema(structured, template.output_schema)
    for field, value in patch.items():
        setattr(note, field, value)
    if "reminder_at" in patch:
        note.reminder_sent = False
    if "template_id" in patch and note.body:
        note.status = NoteStatus.PROCESSED
    session.add(note)
    if note.body and note.status == NoteStatus.PROCESSED:
        await session.flush()
        await process_generated_note(session, note.id, current.id)
    await session.commit()
    return await _note_with_logs(session, note.id, current.id)


@router.post("/{note_id}/dispatch", response_model=NoteRead)
async def redispatch_note(
    note_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> NoteRead:
    note = await _get_owned_note(session, note_id, current.id)
    if note.template_id is None or not note.body or note.status != NoteStatus.PROCESSED:
        raise HTTPException(status_code=400, detail="Gere uma nota com modelo antes de executar suas ações")
    await prepare_template_actions(session, note, current.id)
    await session.commit()
    return await _note_with_logs(session, note.id, current.id)


async def _create_task(
    session: AsyncSession, current: User, note: Note, key: str, title: str,
    summary: str, due_at: datetime | None = None, remind_at: datetime | None = None,
) -> Task:
    if current.workspace_id is None:
        raise HTTPException(status_code=409, detail="Usuário sem espaço pessoal")
    membership = (await session.exec(select(WorkspaceMembership.id).where(
        WorkspaceMembership.workspace_id == current.workspace_id,
        WorkspaceMembership.user_id == current.id,
    ))).first()
    if membership is None:
        raise HTTPException(status_code=404, detail="Espaço pessoal não encontrado")
    task = (await session.exec(select(Task).where(
        Task.workspace_id == current.workspace_id, Task.user_id == current.id,
        Task.idempotency_key == key,
    ))).first()
    if task is None:
        task = Task(
            workspace_id=current.workspace_id, user_id=current.id, source_note_id=note.id,
            idempotency_key=key, title=title[:300], summary=summary,
            status="open", kanban_status="todo", due_at=due_at, remind_at=remind_at,
        )
        session.add(task)
        await session.flush()
    return task


@router.post("/{note_id}/to-kanban", response_model=TaskRead)
async def send_note_to_kanban(
    note_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> Task:
    note = await _get_owned_note(session, note_id, current.id)
    if note.status != NoteStatus.PROCESSED:
        raise HTTPException(status_code=400, detail="Somente notas processadas podem criar tarefas")
    task = await _create_task(
        session, current, note, f"note-task:{note.id}",
        note.title or "Tarefa da nota", note.summary or note.body, note.due_date, note.reminder_at,
    )
    await session.commit()
    return task


@router.post("/{note_id}/tasks-to-kanban", response_model=list[TaskRead])
async def split_tasks_to_kanban(
    note_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> list[Task]:
    note = await _get_owned_note(session, note_id, current.id)
    raw = (note.structured_data or {}).get("tarefas", [])
    if not isinstance(raw, list) or not raw:
        raise HTTPException(status_code=400, detail="Nota sem tarefas para criar")
    tasks: list[Task] = []
    for index, item in enumerate(raw):
        title = item if isinstance(item, str) else item.get("title", "") if isinstance(item, dict) else ""
        if not title.strip():
            continue
        tasks.append(await _create_task(
            session, current, note, f"note-task:{note.id}:{index}", title, note.summary,
        ))
    if not tasks:
        raise HTTPException(status_code=400, detail="Nota sem tarefas válidas")
    await session.commit()
    return tasks


@router.delete("/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(
    note_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> None:
    note = await _get_owned_note(session, note_id, current.id)
    for log in (await session.exec(select(ExecutionLog).where(ExecutionLog.note_id == note.id))).all():
        await session.delete(log)
    await session.delete(note)
    await session.commit()
