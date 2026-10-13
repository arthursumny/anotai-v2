"""Bounded recovery scan for audio documents lost on worker crashes."""

import uuid
from typing import Any

from sqlalchemy import not_
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.generated_document import GeneratedDocument
from app.models.normalized_event import NormalizedEvent
from app.models.note import Note, NoteStatus
from app.models.user import User
from app.models.workspace import WorkspaceMembership
from app.services.document_pipeline import process_generated_note
from app.services.workflow_engine import prepare_template_actions


async def reconcile_missing_documents(session: AsyncSession, limit: int = 100) -> dict[str, int]:
    """Recover persisted documents without regenerating content or inline sends."""
    if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
        raise ValueError("limit deve ser um inteiro positivo")
    has_document = select(GeneratedDocument.id).where(
        GeneratedDocument.source_event_id == NormalizedEvent.id,
        GeneratedDocument.workspace_id == NormalizedEvent.workspace_id,
    ).exists()
    cursor: uuid.UUID | None = None
    scanned = recovered = skipped = failed = 0

    while recovered < limit:
        statement = select(NormalizedEvent).where(
            NormalizedEvent.source == "audio",
            NormalizedEvent.type == "AUDIO_TRANSCRIBED",
            not_(has_document),
        ).order_by(NormalizedEvent.id.asc()).limit(limit)
        if cursor is not None:
            statement = statement.where(NormalizedEvent.id > cursor)
        events = list((await session.exec(statement)).all())
        if not events:
            break
        event_rows = [
            (event.id, event.workspace_id, event.user_id, event.external_id)
            for event in events
        ]
        cursor = event_rows[-1][0]

        for event_id, workspace_id, event_user_id, external_id in event_rows:
            scanned += 1
            try:
                try:
                    note_id = uuid.UUID(external_id or "")
                except (ValueError, TypeError, AttributeError):
                    skipped += 1
                    continue
                note = await session.get(Note, note_id)
                if (
                    note is None
                    or note.user_id != event_user_id
                    or note.status != NoteStatus.PROCESSED
                ):
                    skipped += 1
                    continue
                user = await session.get(User, event_user_id)
                if user is None or user.workspace_id != workspace_id:
                    skipped += 1
                    continue
                membership = (await session.exec(
                    select(WorkspaceMembership.id).where(
                        WorkspaceMembership.workspace_id == workspace_id,
                        WorkspaceMembership.user_id == event_user_id,
                    )
                )).first()
                if membership is None:
                    skipped += 1
                    continue

                document = await process_generated_note(session, note.id, user.id)
                if document is None:
                    await session.rollback()
                    skipped += 1
                    continue
                # The document is durable before workflow preparation. Trigger
                # failures can be retried independently without losing it.
                await session.commit()
                try:
                    await prepare_template_actions(session, note, user.id)
                    await session.commit()
                except Exception:
                    await session.rollback()
                    failed += 1
                recovered += 1
                if recovered >= limit:
                    break
            except Exception:
                # Keep the scan moving; rollback only the current event's work.
                await session.rollback()
                failed += 1

    return {"scanned": scanned, "recovered": recovered, "skipped": skipped, "failed": failed}
