"""Authenticated, workspace-scoped audit event listing."""

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlmodel import or_, select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.database import get_session
from app.core.deps import get_current_user
from app.models.audit_event import AuditEvent
from app.models.user import User
from app.models.workspace import WorkspaceMembership
from app.schemas.audit import AuditEventPage, AuditEventRead
from app.services.audit import _safe_metadata

router = APIRouter()


@router.get("", response_model=AuditEventPage)
async def list_audit_events(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> AuditEventPage:
    if current.workspace_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Workspace indisponível")
    membership = (await session.exec(select(WorkspaceMembership).where(
        WorkspaceMembership.workspace_id == current.workspace_id,
        WorkspaceMembership.user_id == current.id,
    ))).first()
    if membership is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acesso ao workspace negado")

    records = (await session.exec(
        select(AuditEvent)
        .where(
            AuditEvent.workspace_id == current.workspace_id,
            or_(AuditEvent.user_id == current.id, AuditEvent.actor == "system"),
        )
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .offset(offset)
        .limit(limit)
    )).all()
    items = []
    for item in records:
        try:
            safe_metadata = _safe_metadata(item.metadata_json)
        except (TypeError, ValueError):
            # Never leak untrusted legacy/directly-written metadata through the API.
            safe_metadata = {}
        items.append(AuditEventRead(
            actor=item.actor,
            event=item.event,
            action=item.action,
            status=item.status,
            source=item.source,
            target_kind=item.target_kind,
            target_ref=item.target_ref,
            metadata=safe_metadata,
            created_at=item.created_at,
        ))
    return AuditEventPage(items=items, limit=limit, offset=offset)
