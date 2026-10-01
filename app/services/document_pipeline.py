"""Persist generated-document snapshots and suggestions from processed notes."""

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.generated_document import GeneratedDocument
from app.models.normalized_event import NormalizedEvent
from app.models.note import Note, NoteStatus
from app.models.template import Template
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMembership
from app.services.intelligence import classify_event
from app.services.intelligence_materializer import materialize_results
from app.schemas.intelligence import IntelligenceType

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


def _render_note(structured_data: dict[str, Any], template_id: uuid.UUID | None) -> str:
    safe_data = _safe_value(structured_data)
    if template_id is None:
        general_text = safe_data.get("nota")
        if isinstance(general_text, str) and general_text.strip():
            return general_text
    return json.dumps(safe_data, ensure_ascii=False, sort_keys=True, indent=2, default=str)


async def process_generated_note(
    session: AsyncSession,
    note_id: uuid.UUID,
    user_id: uuid.UUID,
) -> GeneratedDocument | None:
    """Create an idempotent document snapshot and safe suggestions, without committing."""
    if not isinstance(note_id, uuid.UUID) or not isinstance(user_id, uuid.UUID):
        return None
    note = await session.get(Note, note_id)
    if (
        note is None
        or note.user_id != user_id
        or note.status != NoteStatus.PROCESSED
        or not isinstance(note.structured_data, dict)
    ):
        return None

    user = await session.get(User, user_id)
    workspace_id = user.workspace_id if user is not None else None
    if not isinstance(workspace_id, uuid.UUID):
        return None
    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        return None
    membership_id = (await session.exec(
        select(WorkspaceMembership.id).where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == user_id,
        )
    )).first()
    if membership_id is None:
        return None

    event = (await session.exec(
        select(NormalizedEvent).where(
            NormalizedEvent.workspace_id == workspace_id,
            NormalizedEvent.source == "audio",
            NormalizedEvent.external_id == str(note.id),
            NormalizedEvent.user_id == user_id,
        )
    )).first()
    if event is None:
        return None

    existing = (await session.exec(
        select(GeneratedDocument).where(
            GeneratedDocument.note_id == note.id,
            GeneratedDocument.workspace_id == workspace_id,
        )
    )).first()
    if existing is not None:
        return existing

    owned_template: Template | None = None
    if note.template_id is not None:
        template = await session.get(Template, note.template_id)
        if template is not None and template.user_id == user_id:
            owned_template = template
    template_version = str(owned_template.version) if owned_template is not None else "general-v1"
    template_id = owned_template.id if owned_template is not None else None

    structured_data = _safe_value(note.structured_data)
    rendered_content = _render_note(note.structured_data, template_id)
    document = GeneratedDocument(
        workspace_id=workspace_id,
        user_id=user_id,
        source_event_id=event.id,
        note_id=note.id,
        template_id=template_id,
        template_version=template_version,
        structured_data=structured_data,
        rendered_content=rendered_content,
    )
    session.add(document)
    await session.flush()

    occurred_at = event.occurred_at
    if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
        # NormalizedEvent stores timestamps in UTC; SQLite may discard tzinfo.
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    results = await classify_event(
        event.content,
        occurred_at=occurred_at,
        timezone=workspace.timezone,
        locale=workspace.locale,
        context={
            "source": event.source,
            "template_version": template_version,
        },
    )
    # Audio worker may already have materialized these legacy suggestion types
    # from the same event. Keep this event-local dedup independent of titles.
    has_kanban_tasks = isinstance(note.structured_data.get("kanban_task_ids"), list) and bool(
        note.structured_data["kanban_task_ids"]
    )
    has_legacy_reminder = note.reminder_at is not None
    if has_kanban_tasks or has_legacy_reminder:
        results = [
            result for result in results
            if not (
                (has_kanban_tasks and result.type == IntelligenceType.TASK)
                or (has_legacy_reminder and result.type == IntelligenceType.REMINDER)
            )
        ]
    await materialize_results(
        session,
        event,
        results,
        timezone=workspace.timezone,
        automation_mode="SUGGESTIVE",
    )
    return document
