"""Persist the readable document in the same transaction as its note."""

import re
import uuid
from typing import Any

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.generated_document import GeneratedDocument
from app.models.normalized_event import NormalizedEvent
from app.models.note import Note, NoteStatus
from app.models.template import Template
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMembership

_SENSITIVE_KEY = re.compile(r"password|secret|credential|token|authorization|api[_-]?key|nango.?connection", re.I)
_SENSITIVE_VALUE = re.compile(r"\bBearer\s+\S+|\b(token|password|secret|api[_-]?key)\s*[:=]\s*\S+", re.I)


def _safe_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _safe_value(item)
            for key, item in value.items()
            if not _SENSITIVE_KEY.search(str(key))
        }
    if isinstance(value, list):
        return [_safe_value(item) for item in value]
    if isinstance(value, str):
        return _SENSITIVE_VALUE.sub("[redacted]", value)
    return value


async def process_generated_note(
    session: AsyncSession,
    note_id: uuid.UUID,
    user_id: uuid.UUID,
) -> GeneratedDocument | None:
    """Create or update the note's document without committing or running actions."""
    if not isinstance(note_id, uuid.UUID) or not isinstance(user_id, uuid.UUID):
        return None
    note = await session.get(Note, note_id)
    if (
        note is None
        or note.user_id != user_id
        or note.status != NoteStatus.PROCESSED
        or not isinstance(note.structured_data, dict)
        or not note.body.strip()
    ):
        raise ValueError("Nota sem documento completo")

    user = await session.get(User, user_id)
    workspace_id = user.workspace_id if user is not None else None
    if not isinstance(workspace_id, uuid.UUID):
        raise ValueError("Usuário sem espaço pessoal")
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise ValueError("Espaço pessoal não encontrado")
    membership_id = (await session.exec(
        select(WorkspaceMembership.id).where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == user_id,
        )
    )).first()
    if membership_id is None:
        raise PermissionError("Usuário não pertence ao espaço da nota")

    event = (await session.exec(
        select(NormalizedEvent).where(
            NormalizedEvent.workspace_id == workspace_id,
            NormalizedEvent.source == "audio",
            NormalizedEvent.external_id == str(note.id),
            NormalizedEvent.user_id == user_id,
        )
    )).first()
    existing = (await session.exec(
        select(GeneratedDocument).where(
            GeneratedDocument.note_id == note.id,
            GeneratedDocument.workspace_id == workspace_id,
        )
    )).first()
    owned_template: Template | None = None
    if note.template_id is not None:
        template = await session.get(Template, note.template_id)
        if template is not None and template.user_id == user_id:
            owned_template = template
    template_version = str(owned_template.version) if owned_template is not None else "general-v1"
    template_id = owned_template.id if owned_template is not None else None

    structured_data = _safe_value(note.structured_data)
    if existing is not None:
        existing.template_id = template_id
        existing.template_version = template_version
        existing.structured_data = structured_data
        existing.rendered_content = note.body
        session.add(existing)
        await session.flush()
        return existing
    document = GeneratedDocument(
        workspace_id=workspace_id,
        user_id=user_id,
        source_event_id=event.id if event is not None else None,
        note_id=note.id,
        template_id=template_id,
        template_version=template_version,
        structured_data=structured_data,
        rendered_content=note.body,
    )
    session.add(document)
    await session.flush()

    return document
