"""Authenticated workspace settings endpoints."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.database import get_session
from app.core.deps import get_current_user
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMembership
from app.schemas.settings import LocalizationPatch, LocalizationSettings

router = APIRouter()


async def _workspace_and_membership(session: AsyncSession, current: User):
    if current.workspace_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Workspace indisponível")

    membership = (await session.exec(select(WorkspaceMembership).where(
        WorkspaceMembership.workspace_id == current.workspace_id,
        WorkspaceMembership.user_id == current.id,
    ))).first()
    if membership is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acesso ao workspace negado")

    workspace = await session.get(Workspace, current.workspace_id)
    if workspace is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Workspace indisponível")
    return workspace, membership


@router.get("/localization", response_model=LocalizationSettings)
async def get_localization(
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> LocalizationSettings:
    workspace, _membership = await _workspace_and_membership(session, current)
    return LocalizationSettings(timezone=workspace.timezone, locale=workspace.locale)


@router.patch("/localization", response_model=LocalizationSettings)
async def patch_localization(
    payload: LocalizationPatch,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> LocalizationSettings:
    workspace, membership = await _workspace_and_membership(session, current)
    if membership.role != "OWNER":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Somente OWNER pode alterar configurações")

    if "timezone" in payload.model_fields_set and payload.timezone is not None:
        workspace.timezone = payload.timezone
    if "locale" in payload.model_fields_set and payload.locale is not None:
        workspace.locale = payload.locale
    await session.commit()
    await session.refresh(workspace)
    return LocalizationSettings(timezone=workspace.timezone, locale=workspace.locale)
