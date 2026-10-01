"""Authenticated controls for explicit source-monitoring opt-in."""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.database import get_session
from app.core.config import settings
from app.core.deps import get_current_user
from app.models.gmail_watch_state import GmailWatchState
from app.models.integration_connection import IntegrationConnection
from app.models.source_policy import SourceMonitoringPolicy
from app.models.user import User
from app.models.workspace import WorkspaceMembership
from app.schemas.source_policy import SourceName, SourcePolicyPut, SourcePolicyRead, validate_gmail_addresses
from app.services.gmail_ingress import GmailSourceProvider
from app.services.nango_client import NangoClient
from app.services.whatsapp_privacy import purge_whatsapp_data

_purge_whatsapp_data = purge_whatsapp_data

router = APIRouter()


async def _pause_gmail_watch(session: AsyncSession, workspace_id, user_id) -> None:
    connection = (await session.exec(select(IntegrationConnection).where(
        IntegrationConnection.workspace_id == workspace_id,
        IntegrationConnection.user_id == user_id,
        IntegrationConnection.provider == "google_gmail",
    ))).first()
    if connection is None:
        return
    state = (await session.exec(select(GmailWatchState).where(
        GmailWatchState.integration_connection_id == connection.id,
    ))).first()
    if state is not None and state.status != "needs_resync":
        state.status = "paused"


async def _start_gmail_watch_best_effort(session: AsyncSession, workspace_id, user_id) -> None:
    if not (settings.NANGO_ENV_KEY.strip() and settings.NANGO_BASE_URL.strip()
            and settings.GMAIL_PUBSUB_TOPIC.strip()):
        return
    connection = (await session.exec(select(IntegrationConnection).where(
        IntegrationConnection.workspace_id == workspace_id,
        IntegrationConnection.user_id == user_id,
        IntegrationConnection.provider == "google_gmail",
        IntegrationConnection.status == "active",
        IntegrationConnection.account_email != None,  # noqa: E711
    ))).first()
    if connection is None or not (connection.account_email or "").strip():
        return
    try:
        provider = GmailSourceProvider(NangoClient(
            api_key=settings.NANGO_ENV_KEY, base_url=settings.NANGO_BASE_URL,
        ))
        await provider.renew_watch(session, connection)
        await session.commit()
    except Exception:
        # Policy opt-in is already committed; a later scheduled renewal can recover.
        await session.rollback()


async def _current_membership(session: AsyncSession, current: User):
    if current.workspace_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Workspace indisponível")
    membership = (await session.exec(select(WorkspaceMembership).where(
        WorkspaceMembership.workspace_id == current.workspace_id,
        WorkspaceMembership.user_id == current.id,
    ))).first()
    if membership is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Acesso ao workspace negado")
    return current.workspace_id


def _whatsapp_filter_changed(existing: SourceMonitoringPolicy | None, values: dict) -> bool:
    if existing is None:
        return False
    return any(
        getattr(existing, field) != values[field]
        for field in ("mode", "selected_ids", "direction", "allow_groups", "allow_individuals")
    )


def _response(source: SourceName, policy: SourceMonitoringPolicy | None) -> SourcePolicyRead:
    if policy is None:
        return SourcePolicyRead(source=source, monitor_enabled=False)
    return SourcePolicyRead(
        source=source,
        monitor_enabled=True,
        mode=policy.mode,
        selected_ids=policy.selected_ids,
        direction=policy.direction,
        allow_groups=policy.allow_groups,
        allow_individuals=policy.allow_individuals,
        gmail_label_ids=policy.gmail_label_ids or ["INBOX"],
        gmail_allowed_senders=policy.gmail_allowed_senders,
        retention_days=policy.retention_days,
    )


@router.get("/{source}", response_model=SourcePolicyRead)
async def get_source_policy(
    source: SourceName,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> SourcePolicyRead:
    workspace_id = await _current_membership(session, current)
    policy = (await session.exec(select(SourceMonitoringPolicy).where(
        SourceMonitoringPolicy.workspace_id == workspace_id,
        SourceMonitoringPolicy.user_id == current.id,
        SourceMonitoringPolicy.source == source,
    ))).first()
    return _response(source, policy)


@router.put("/{source}", response_model=SourcePolicyRead)
async def put_source_policy(
    source: SourceName,
    payload: SourcePolicyPut,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> SourcePolicyRead:
    workspace_id = await _current_membership(session, current)
    existing = (await session.exec(select(SourceMonitoringPolicy).where(
        SourceMonitoringPolicy.workspace_id == workspace_id,
        SourceMonitoringPolicy.user_id == current.id,
        SourceMonitoringPolicy.source == source,
    ))).first()

    if not payload.monitor_enabled:
        if source == "whatsapp":
            await _purge_whatsapp_data(session, workspace_id, current.id)
        if source == "gmail":
            await _pause_gmail_watch(session, workspace_id, current.id)
        if existing is not None:
            await session.delete(existing)
        if source == "gmail" or existing is not None:
            await session.commit()
        return _response(source, None)

    selected_ids = payload.selected_ids
    if source == "gmail":
        try:
            selected_ids = validate_gmail_addresses(selected_ids)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    values = {
        "mode": payload.mode,
        "selected_ids": selected_ids,
        "direction": payload.direction,
        "allow_groups": payload.allow_groups,
        "allow_individuals": payload.allow_individuals,
        "gmail_label_ids": payload.gmail_label_ids if source == "gmail" else [],
        "gmail_allowed_senders": payload.gmail_allowed_senders if source == "gmail" else [],
    }
    if existing is None or "retention_days" in payload.model_fields_set:
        values["retention_days"] = payload.retention_days
    whatsapp_filters_changed = source == "whatsapp" and _whatsapp_filter_changed(existing, values)
    retention_only_update = (
        existing is not None
        and "retention_days" in payload.model_fields_set
        and not payload.model_fields_set.intersection({
            "mode", "selected_ids", "direction", "allow_groups", "allow_individuals",
            "gmail_label_ids", "gmail_allowed_senders",
        })
    )
    if existing is None:
        existing = SourceMonitoringPolicy(
            workspace_id=workspace_id, user_id=current.id, source=source,
            mode=payload.mode, selected_ids=selected_ids, direction=payload.direction,
            allow_groups=payload.allow_groups, allow_individuals=payload.allow_individuals,
            gmail_label_ids=payload.gmail_label_ids if source == "gmail" else [],
            gmail_allowed_senders=payload.gmail_allowed_senders if source == "gmail" else [],
            retention_days=payload.retention_days,
        )
        session.add(existing)
    else:
        for key, value in values.items():
            setattr(existing, key, value)
    if whatsapp_filters_changed:
        await _purge_whatsapp_data(session, workspace_id, current.id)
    await session.commit()
    await session.refresh(existing)
    response = _response(source, existing)
    if source == "gmail" and not retention_only_update:
        await _start_gmail_watch_best_effort(session, workspace_id, current.id)
    return response


@router.delete("/{source}", response_model=SourcePolicyRead)
async def delete_source_policy(
    source: SourceName,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> SourcePolicyRead:
    workspace_id = await _current_membership(session, current)
    existing = (await session.exec(select(SourceMonitoringPolicy).where(
        SourceMonitoringPolicy.workspace_id == workspace_id,
        SourceMonitoringPolicy.user_id == current.id,
        SourceMonitoringPolicy.source == source,
    ))).first()
    if existing is not None:
        if source == "whatsapp":
            await _purge_whatsapp_data(session, workspace_id, current.id)
        if source == "gmail":
            await _pause_gmail_watch(session, workspace_id, current.id)
        await session.delete(existing)
        await session.commit()
    elif source == "whatsapp":
        await _purge_whatsapp_data(session, workspace_id, current.id)
        await session.commit()
    elif source == "gmail":
        await _pause_gmail_watch(session, workspace_id, current.id)
        await session.commit()
    return _response(source, None)
