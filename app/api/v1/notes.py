"""AnotAI — notas: leitura, edição human-in-the-loop, reclassificação e re-disparo."""

import uuid
from datetime import datetime, timezone
from typing import Any

import jsonschema
from fastapi import APIRouter, Depends, HTTPException, Query, status
from jsonschema import Draft202012Validator
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.database import get_session
from app.core.deps import get_current_user
from app.models.execution_log import ExecutionLog, ExecutionStatus
from app.models.note import KanbanColumn, Note, NoteStatus
from app.models.template import Template
from app.models.user import User
from app.schemas.note import ExecutionLogRead, NoteRead, NoteUpdate, ReclassifyRequest
from app.services.dispatcher import dispatch_actions
from app.services.extractor import ExtractionError, extract_structured

router = APIRouter()


def _is_general_note(note: Note) -> bool:
    """Nota geral: sem template, processada, com corpo interpretado pela IA."""
    if note.template_id is not None:
        return False
    data = note.structured_data or {}
    return isinstance(data, dict) and ("nota" in data or data.get("tipo") == "nota_geral")


def _parse_dt(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text[-1] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


async def _get_owned_note(session: AsyncSession, note_id: uuid.UUID, user_id: uuid.UUID) -> Note:
    note = await session.get(Note, note_id)
    if note is None or note.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Nota não encontrada")
    return note


async def _get_accessible_template(
    session: AsyncSession, template_id: uuid.UUID, user_id: uuid.UUID
) -> Template:
    template = await session.get(Template, template_id)
    if template is None or template.user_id not in (None, user_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Template não encontrado")
    return template


def _validate_against_schema(structured: dict[str, Any], schema: dict[str, Any]) -> None:
    """422 se o JSON editado não obedecer ao schema do template."""
    try:
        Draft202012Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Schema do template inválido: {exc.message}",
        ) from exc
    try:
        Draft202012Validator(schema).validate(structured)
    except jsonschema.ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=f"Dado fora do schema do template: {exc.message}",
        ) from exc


async def _note_with_logs(session: AsyncSession, note_id: uuid.UUID, user_id: uuid.UUID) -> NoteRead:
    note = await session.get(Note, note_id)
    if note is None or note.user_id != user_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Nota não encontrada")
    result = await session.exec(
        select(ExecutionLog).where(ExecutionLog.note_id == note_id).order_by(ExecutionLog.created_at.asc())  # type: ignore[arg-type]
    )
    logs = [ExecutionLogRead.model_validate(r) for r in result.all()]
    data = NoteRead.model_validate(note)
    data.logs = logs
    return data


@router.get("", response_model=list[NoteRead])
async def list_notes(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> list[NoteRead]:
    result = await session.exec(
        select(Note).where(Note.user_id == current.id).order_by(Note.created_at.desc()).offset(offset).limit(limit)  # type: ignore[arg-type]
    )
    out: list[NoteRead] = []
    for note in result.all():
        logs_res = await session.exec(select(ExecutionLog).where(ExecutionLog.note_id == note.id))  # type: ignore[arg-type]
        item = NoteRead.model_validate(note)
        item.logs = [ExecutionLogRead.model_validate(r) for r in logs_res.all()]
        out.append(item)
    return out


@router.get("/{note_id}", response_model=NoteRead)
async def get_note(
    note_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> NoteRead:
    return await _note_with_logs(session, note_id, current.id)


@router.post("/{note_id}/reclassify", response_model=NoteRead)
async def reclassify_note(
    note_id: uuid.UUID,
    data: ReclassifyRequest,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> NoteRead:
    """Associa um template a uma nota existente (inclusive nota geral).

    Reaproveita a `raw_transcription` já salva, roda o extrator com o schema do
    novo template e dispara as ações via dispatcher. Retorna a nota atualizada
    (sempre 200 quando nota+template existem; o `status` diz o resultado).
    """
    note = await _get_owned_note(session, note_id, current.id)
    template = await _get_accessible_template(session, data.template_id, current.id)

    if not (note.raw_transcription or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nota sem transcrição para reclassificar",
        )

    try:
        structured = await extract_structured(template, note.raw_transcription)
    except ExtractionError as exc:
        note.template_id = template.id
        note.structured_data = None
        note.status = NoteStatus.PENDING_REVIEW
        session.add(note)
        session.add(
            ExecutionLog(
                note_id=note.id,
                action_type="extraction",
                status=ExecutionStatus.FAILED,
                payload={"template_id": str(template.id), "via": "reclassify"},
                error_message=str(exc)[:4000],
            )
        )
        await session.commit()
        return await _note_with_logs(session, note.id, current.id)

    note.template_id = template.id
    note.structured_data = structured
    note.status = NoteStatus.PROCESSED
    session.add(note)
    await session.commit()
    await session.refresh(note)

    await dispatch_actions(note, template, session)
    return await _note_with_logs(session, note.id, current.id)


@router.patch("/{note_id}", response_model=NoteRead)
async def update_note(
    note_id: uuid.UUID,
    data: NoteUpdate,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> NoteRead:
    """Edição human-in-the-loop: corrige transcrição e/ou JSON manualmente.

    Se `template_id` for informado, o JSON resultante é validado contra o
    schema do template (422 se inválido). Vincular template + JSON válido
    move a nota para `processed`. Desvincular (template_id=null) mantém a
    nota como genérica `processed` quando há conteúdo, ou `pending_review`
    quando vazia — nunca volta para o legado `no_match`.
    Sem `template_id`, o status é mantido.
    """
    note = await _get_owned_note(session, note_id, current.id)
    patch = data.model_dump(exclude_unset=True)

    template: Template | None = None
    if "template_id" in patch:
        if patch["template_id"] is not None:
            template = await _get_accessible_template(session, patch["template_id"], current.id)
            note.template_id = template.id
        else:
            note.template_id = None
    elif note.template_id is not None:
        template = await session.get(Template, note.template_id)

    if "raw_transcription" in patch and patch["raw_transcription"] is not None:
        note.raw_transcription = patch["raw_transcription"]

    if "structured_data" in patch:
        note.structured_data = patch["structured_data"]

    for field in ("title", "due_date", "reminder_at"):
        if field in patch:
            setattr(note, field, patch[field])
    if "reminder_at" in patch:
        note.reminder_sent = False
    if "kanban_status" in patch:
        if patch["kanban_status"] is not None and patch["kanban_status"] not in ("todo", "in_progress", "done"):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail="kanban_status deve ser 'todo', 'in_progress' ou 'done'",
            )
        note.kanban_status = patch["kanban_status"]

    if template is not None and note.structured_data is not None:
        _validate_against_schema(note.structured_data, template.output_schema)

    if "template_id" in patch:
        if note.template_id is not None and note.structured_data is not None:
            note.status = NoteStatus.PROCESSED
        elif note.template_id is not None:
            note.status = NoteStatus.PENDING_REVIEW
        else:
            # Nota geral/desvinculada: com conteúdo é processada, vazia é revisão.
            # (NO_MATCH é legado de leitura; não criamos mais.)
            note.status = (
                NoteStatus.PROCESSED if note.structured_data else NoteStatus.PENDING_REVIEW
            )

    session.add(note)
    await session.commit()
    await session.refresh(note)
    return await _note_with_logs(session, note.id, current.id)


@router.post("/{note_id}/dispatch", response_model=NoteRead)
async def redispatch_note(
    note_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> NoteRead:
    """Re-dispara as ações do template vinculado (útil após correção manual do JSON)."""
    note = await _get_owned_note(session, note_id, current.id)
    if note.template_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nota sem template vinculado (use reclassify ou PATCH com template_id)",
        )
    template = await _get_accessible_template(session, note.template_id, current.id)
    await dispatch_actions(note, template, session)
    return await _note_with_logs(session, note.id, current.id)


@router.post("/{note_id}/to-kanban", response_model=NoteRead)
async def send_note_to_kanban(
    note_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> NoteRead:
    """Coloca qualquer nota processada (inclusive nota geral) no Kanban (`todo`).

    Idempotente: se já estiver no Kanban, só retorna a nota.
    """
    note = await _get_owned_note(session, note_id, current.id)
    if note.status not in (NoteStatus.PROCESSED, NoteStatus.NO_MATCH):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Somente notas processadas podem ir ao Kanban",
        )
    if note.kanban_status is None:
        note.kanban_status = KanbanColumn.TODO.value
        session.add(note)
        await session.commit()
        await session.refresh(note)
    return await _note_with_logs(session, note.id, current.id)


@router.post("/{note_id}/tasks-to-kanban", response_model=list[NoteRead])
async def split_tasks_to_kanban(
    note_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> list[NoteRead]:
    """Cria um card `todo` no Kanban para cada tarefa da nota geral.

    Lê `structured_data.tarefas` (lista de {title, due_date, reminder_at} ou
    strings). Retorna os cards criados. 400 se a nota não tiver tarefas.
    """
    note = await _get_owned_note(session, note_id, current.id)
    data = note.structured_data or {}
    raw_tasks = data.get("tarefas", []) if isinstance(data, dict) else []
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nota sem tarefas para enviar ao Kanban",
        )
    existing_ids = data.get("kanban_task_ids") if isinstance(data, dict) else None
    if isinstance(existing_ids, list) and existing_ids:
        existing_cards: list[NoteRead] = []
        for card_id in existing_ids:
            try:
                card = await session.get(Note, uuid.UUID(str(card_id)))
            except ValueError:
                card = None
            if card is not None and card.user_id == current.id:
                existing_cards.append(await _note_with_logs(session, card.id, current.id))
        if existing_cards:
            return existing_cards
    created: list[Note] = []
    for item in raw_tasks[:10]:
        if isinstance(item, str):
            title, due_s, rem_s = item.strip(), None, None
        elif isinstance(item, dict):
            title = str(item.get("title", "")).strip()
            due_s = item.get("due_date")
            rem_s = item.get("reminder_at")
        else:
            continue
        if not title:
            continue
        card = Note(
            user_id=current.id,
            title=title[:300],
            raw_transcription=note.raw_transcription,
            structured_data={"description": title, "origem_nota_geral": str(note.id)},
            status=NoteStatus.PROCESSED,
            capture_mode=note.capture_mode,
            kanban_status=KanbanColumn.TODO.value,
            due_date=_parse_dt(due_s),
            reminder_at=_parse_dt(rem_s),
            reminder_sent=False,
        )
        session.add(card)
        created.append(card)
    if not created:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Nota sem tarefas válidas para enviar ao Kanban",
        )
    data["kanban_task_ids"] = [str(card.id) for card in created]
    note.structured_data = data
    note.kanban_status = None
    session.add(note)
    await session.commit()
    out: list[NoteRead] = []
    for card in created:
        await session.refresh(card)
        out.append(await _note_with_logs(session, card.id, current.id))
    return out


@router.delete("/{note_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_note(
    note_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> None:
    """Apaga a nota do usuário junto com seus logs."""
    note = await _get_owned_note(session, note_id, current.id)
    result = await session.exec(select(ExecutionLog).where(ExecutionLog.note_id == note.id))
    for log in result.all():
        await session.delete(log)
    await session.delete(note)
    await session.commit()
