"""Secure Nango Connect session, connection, and webhook API."""

import hashlib
import hmac
import json
import re
import uuid
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from email_validator import EmailNotValidError, validate_email
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.core.database import get_session
from app.core.deps import get_current_user
from app.models.gmail_watch_state import GmailWatchState
from app.models.integration_connection import IntegrationConnection
from app.models.source_policy import SourceMonitoringPolicy
from app.models.user import User
from app.models.workspace import WorkspaceMembership
from app.schemas.integration import (
    IntegrationConnectionRead,
    IntegrationProvider,
    GmailResumeRequest,
    IntegrationSessionCreate,
    IntegrationSessionRead,
    IntegrationStatus,
)
from app.services.nango_client import NangoClient, NangoClientError
from app.services.gmail_ingress import GmailSourceProvider
from app.services.audit import write_audit

router = APIRouter()
_MAX_WEBHOOK_BYTES = 64 * 1024
_SIGNATURE_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_OPERATIONS = {"creation", "override", "refresh", "deletion"}


def _provider_configs() -> dict[str, tuple[IntegrationProvider, str]]:
    return {
        "google_gmail": ("google_gmail", settings.NANGO_GOOGLE_GMAIL_PROVIDER_CONFIG_KEY),
        "google_calendar": ("google_calendar", settings.NANGO_GOOGLE_CALENDAR_PROVIDER_CONFIG_KEY),
        "slack": ("slack", settings.NANGO_SLACK_PROVIDER_CONFIG_KEY),
        "notion": ("notion", settings.NANGO_NOTION_PROVIDER_CONFIG_KEY),
    }


def _nango_client(*, require_signing_key: bool = False) -> NangoClient:
    if (
        not settings.NANGO_ENV_KEY
        or not settings.NANGO_BASE_URL
        or (require_signing_key and not settings.NANGO_SIGNING_KEY)
    ):
        raise HTTPException(status_code=503, detail="Integrações temporariamente indisponíveis")
    try:
        return NangoClient(
            settings.NANGO_ENV_KEY,
            base_url=settings.NANGO_BASE_URL,
            allow_insecure_localhost=settings.ENV == "dev",
        )
    except ValueError:
        raise HTTPException(status_code=503, detail="Integrações temporariamente indisponíveis") from None


async def _current_workspace(session: AsyncSession, current: User) -> uuid.UUID:
    workspace_id = current.workspace_id
    if workspace_id is None:
        raise HTTPException(status_code=403, detail="Workspace indisponível")
    member = (await session.exec(select(WorkspaceMembership.id).where(
        WorkspaceMembership.workspace_id == workspace_id,
        WorkspaceMembership.user_id == current.id,
    ))).first()
    if member is None:
        raise HTTPException(status_code=403, detail="Acesso ao workspace negado")
    return workspace_id


def _masked_email(email: str | None) -> str | None:
    if not email:
        return None
    local, separator, domain = email.partition("@")
    if not separator:
        return "***"
    return f"{local[:1]}***@{domain}"


@router.post("/sessions", response_model=IntegrationSessionRead)
async def create_session(
    payload: IntegrationSessionCreate,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> IntegrationSessionRead:
    workspace_id = await _current_workspace(session, current)
    client = _nango_client(require_signing_key=True)
    provider_config = _provider_configs()[payload.provider][1]
    if not provider_config:
        raise HTTPException(status_code=503, detail="Integrações temporariamente indisponíveis")
    try:
        connect_session = await client.create_connect_session(
            end_user_id=str(current.id),
            organization_id=str(workspace_id),
            provider_config_key=provider_config,
        )
    except NangoClientError:
        raise HTTPException(status_code=502, detail="Não foi possível iniciar a conexão") from None
    except ValueError:
        raise HTTPException(status_code=503, detail="Integrações temporariamente indisponíveis") from None
    return IntegrationSessionRead(
        session_token=connect_session.token,
        expires_at=connect_session.expires_at,
    )


@router.get(
    "/connections", response_model=list[IntegrationConnectionRead], response_model_exclude_unset=True,
)
async def list_connections(
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> list[IntegrationConnectionRead]:
    workspace_id = await _current_workspace(session, current)
    rows = (await session.exec(select(IntegrationConnection).where(
        IntegrationConnection.workspace_id == workspace_id,
        IntegrationConnection.user_id == current.id,
    ))).all()
    gmail_policy = (await session.exec(select(SourceMonitoringPolicy).where(
        SourceMonitoringPolicy.workspace_id == workspace_id,
        SourceMonitoringPolicy.user_id == current.id,
        SourceMonitoringPolicy.source == "gmail",
    ))).first()
    gmail_ids = [row.id for row in rows if row.provider == "google_gmail"]
    watches = (await session.exec(select(GmailWatchState).where(
        GmailWatchState.integration_connection_id.in_(gmail_ids),
    ))).all() if gmail_ids else []
    watches_by_connection = {watch.integration_connection_id: watch for watch in watches}
    results = []
    for row in rows:
        watch = watches_by_connection.get(row.id)
        watch_fields = (
            {
                "gmail_watch_status": cast(Literal["paused", "active", "needs_resync"], watch.status),
                "expiration_at": watch.expiration_at,
            }
            if watch is not None else
            {"gmail_watch_status": None, "expiration_at": None}
            if row.provider == "google_calendar" else {}
        )
        results.append(IntegrationConnectionRead(
            id=row.id,
            provider=cast(IntegrationProvider, row.provider),
            account_email=_masked_email(row.account_email),
            display_name=row.display_name,
            status=cast(IntegrationStatus, row.status),
            monitor_enabled=bool(gmail_policy) if row.provider == "google_gmail" else False,
            **watch_fields,
        ))
    return results


@router.post("/connections/{local_id}/resume-gmail", response_model=IntegrationConnectionRead)
async def resume_gmail(
    local_id: uuid.UUID,
    payload: GmailResumeRequest,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> IntegrationConnectionRead:
    workspace_id = await _current_workspace(session, current)
    row = (await session.exec(select(IntegrationConnection).where(
        IntegrationConnection.id == local_id,
        IntegrationConnection.workspace_id == workspace_id,
        IntegrationConnection.user_id == current.id,
    ))).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Conexão não encontrada")
    if row.provider != "google_gmail":
        raise HTTPException(status_code=404, detail="Conexão não encontrada")
    if not payload.accept_gap:
        raise HTTPException(status_code=422, detail="Confirmação da lacuna necessária")
    if row.status != "active":
        raise HTTPException(status_code=409, detail="Conexão Gmail não está ativa")

    policy = (await session.exec(select(SourceMonitoringPolicy).where(
        SourceMonitoringPolicy.workspace_id == workspace_id,
        SourceMonitoringPolicy.user_id == current.id,
        SourceMonitoringPolicy.source == "gmail",
    ))).first()
    if policy is None:
        raise HTTPException(status_code=403, detail="Opt-in de monitoramento Gmail necessário")
    state = (await session.exec(select(GmailWatchState).where(
        GmailWatchState.integration_connection_id == row.id,
    ))).first()
    if state is None or state.status != "needs_resync":
        raise HTTPException(status_code=409, detail="Gmail não requer rebaseline")

    expected_config = _provider_configs().get("google_gmail")
    if (
        not settings.NANGO_ENV_KEY or not settings.NANGO_BASE_URL
        or not settings.GMAIL_PUBSUB_TOPIC.strip()
        or expected_config is None or not expected_config[1]
        or row.provider_config_key != expected_config[1]
    ):
        raise HTTPException(status_code=503, detail="Integrações temporariamente indisponíveis")
    client = _nango_client()
    provider = GmailSourceProvider(client)
    try:
        # The previous cursor is held only in the transaction until Nango confirms
        # a fresh baseline. Any failure rolls the temporary changes back.
        state.status = "paused"
        state.history_id = None
        state.page_token = None
        state.expiration_at = None
        await session.flush()
        await provider.renew_watch(session, row)
        baseline = state.history_id
        if (
            not isinstance(baseline, str) or not baseline.isdigit()
            or baseline == "0" or int(baseline) > 9_223_372_036_854_775_807
            or state.expiration_at is None
        ):
            raise ValueError("Invalid Gmail rebaseline response")
        state.status = "active"
        state.last_error_kind = "history_gap_acknowledged"
        state.updated_at = datetime.now(timezone.utc)
        await session.commit()
    except PermissionError:
        await session.rollback()
        raise HTTPException(status_code=503, detail="Integrações temporariamente indisponíveis") from None
    except (NangoClientError, ValueError):
        await session.rollback()
        raise HTTPException(status_code=502, detail="Não foi possível reativar o monitoramento Gmail") from None

    return IntegrationConnectionRead(
        id=row.id,
        provider=cast(IntegrationProvider, row.provider),
        account_email=_masked_email(row.account_email),
        display_name=row.display_name,
        status=cast(IntegrationStatus, row.status),
        monitor_enabled=True,
        gmail_watch_status="active",
        expiration_at=state.expiration_at,
    )


@router.delete("/connections/{local_id}", status_code=204)
async def delete_connection(
    local_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> Response:
    workspace_id = await _current_workspace(session, current)
    row = (await session.exec(select(IntegrationConnection).where(
        IntegrationConnection.id == local_id,
        IntegrationConnection.workspace_id == workspace_id,
        IntegrationConnection.user_id == current.id,
    ))).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Conexão não encontrada")
    was_disconnected = row.status == "disconnected"
    client = _nango_client()
    expected_config = _provider_configs().get(row.provider)
    if expected_config is None or not expected_config[1] or row.provider_config_key != expected_config[1]:
        raise HTTPException(status_code=503, detail="Integrações temporariamente indisponíveis")
    try:
        await client.delete_connection(row.nango_connection_id)
    except NangoClientError:
        raise HTTPException(status_code=502, detail="Não foi possível remover a conexão") from None
    except ValueError:
        raise HTTPException(status_code=503, detail="Integrações temporariamente indisponíveis") from None

    row.status = "disconnected"
    row.updated_at = datetime.now(timezone.utc)
    watch = (await session.exec(select(GmailWatchState).where(
        GmailWatchState.integration_connection_id == row.id,
    ))).first()
    if watch is not None:
        watch.status = "paused"
        watch.updated_at = row.updated_at
    if row.provider == "google_gmail":
        policy = (await session.exec(select(SourceMonitoringPolicy).where(
            SourceMonitoringPolicy.workspace_id == workspace_id,
            SourceMonitoringPolicy.user_id == current.id,
            SourceMonitoringPolicy.source == "gmail",
        ))).first()
        if policy is not None:
            await session.delete(policy)
    if not was_disconnected:
        await write_audit(
            session,
            workspace_id=workspace_id,
            user_id=current.id,
            event="GOOGLE_CONNECTION_DISCONNECTED",
            action="disconnect",
            status="success",
            source="gmail" if row.provider == "google_gmail" else "internal",
            target_kind="integration_connection",
            target_ref=str(row.id),
            metadata={"reason_code": "disconnect", "count": 1},
        )
    await session.commit()
    return Response(status_code=204)


def _field(data: dict, *keys: str):
    for key in keys:
        if key in data:
            return data[key]
    return None


def _tag_uuids(tags: object) -> tuple[uuid.UUID, uuid.UUID] | None:
    if not isinstance(tags, dict):
        return None
    user_tag = tags.get("end_user_id")
    workspace_tag = tags.get("organization_id")
    if not isinstance(user_tag, str) or not isinstance(workspace_tag, str):
        return None
    try:
        return uuid.UUID(user_tag), uuid.UUID(workspace_tag)
    except (ValueError, AttributeError):
        return None


async def _event_owner(
    session: AsyncSession, user_id: uuid.UUID, workspace_id: uuid.UUID,
) -> User | None:
    user = await session.get(User, user_id)
    if user is None or user.workspace_id != workspace_id:
        return None
    member_id = (await session.exec(select(WorkspaceMembership.id).where(
        WorkspaceMembership.workspace_id == workspace_id,
        WorkspaceMembership.user_id == user_id,
    ))).first()
    return user if member_id is not None else None


async def _pause_watch(session: AsyncSession, connection_id: uuid.UUID) -> None:
    watch = (await session.exec(select(GmailWatchState).where(
        GmailWatchState.integration_connection_id == connection_id,
    ))).first()
    if watch is not None:
        watch.status = "paused"


async def _remove_gmail_policy(
    session: AsyncSession, workspace_id: uuid.UUID, user_id: uuid.UUID,
) -> None:
    policy = (await session.exec(select(SourceMonitoringPolicy).where(
        SourceMonitoringPolicy.workspace_id == workspace_id,
        SourceMonitoringPolicy.user_id == user_id,
        SourceMonitoringPolicy.source == "gmail",
    ))).first()
    if policy is not None:
        await session.delete(policy)


def _validated_gmail_email(value: object) -> str | None:
    if not isinstance(value, str) or len(value) > 320:
        return None
    parsed = parseaddr(value.strip())[1]
    if not parsed or parsed != value.strip() or parsed.count("@") != 1:
        return None
    try:
        normalized = validate_email(parsed, check_deliverability=False).normalized
    except (EmailNotValidError, ValueError):
        return None
    if len(normalized) > 320:
        return None
    return normalized.lower()


@router.post("/webhooks/nango", status_code=204)
async def nango_webhook(request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    """Authenticate exact raw bytes before parsing untrusted Nango webhook JSON."""
    if not settings.NANGO_SIGNING_KEY:
        raise HTTPException(status_code=503, detail="Webhook temporariamente indisponível")
    if not settings.NANGO_ENVIRONMENT:
        raise HTTPException(status_code=503, detail="Webhook temporariamente indisponível")

    signature = request.headers.get("X-Nango-Hmac-Sha256", "")
    if not _SIGNATURE_RE.fullmatch(signature):
        raise HTTPException(status_code=401, detail="Assinatura inválida")
    chunks: list[bytes] = []
    body_size = 0
    async for chunk in request.stream():
        body_size += len(chunk)
        if body_size > _MAX_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="Payload muito grande")
        chunks.append(chunk)
    raw_body = b"".join(chunks)
    expected = hmac.new(
        settings.NANGO_SIGNING_KEY.encode("utf-8"), raw_body, hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, signature.lower()):
        raise HTTPException(status_code=401, detail="Assinatura inválida")
    try:
        event = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return Response(status_code=204)
    if not isinstance(event, dict):
        return Response(status_code=204)

    event_type = _field(event, "type")
    operation = _field(event, "operation")
    success = _field(event, "success")
    environment = _field(event, "environment", "environmentName")
    connection_id = _field(event, "connectionId", "connection_id")
    provider_key = _field(event, "providerConfigKey", "provider_config_key")
    owner_tags = _tag_uuids(event.get("tags"))
    if event_type != "auth" or operation not in _OPERATIONS or not isinstance(success, bool):
        return Response(status_code=204)
    if environment != settings.NANGO_ENVIRONMENT:
        return Response(status_code=204)
    config_to_provider = {
        config_key: provider for provider, config_key in _provider_configs().values() if config_key
    }
    provider = config_to_provider.get(provider_key) if isinstance(provider_key, str) else None
    if provider is None:
        return Response(status_code=204)
    if (
        not isinstance(connection_id, str)
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", connection_id)
        or owner_tags is None
    ):
        return Response(status_code=204)
    if not isinstance(provider_key, str):
        return Response(status_code=204)
    user_id, workspace_id = owner_tags
    owner = await _event_owner(session, user_id, workspace_id)
    if owner is None:
        return Response(status_code=204)

    row = (await session.exec(select(IntegrationConnection).where(
        IntegrationConnection.provider_config_key == provider_key,
        IntegrationConnection.nango_connection_id == connection_id,
    ))).first()
    now = datetime.now(timezone.utc)
    if row is not None and (
        row.user_id != user_id or row.workspace_id != workspace_id or row.provider != provider
    ):
        # A Nango binding is permanent to its first tenant; webhook tags cannot rebind it.
        return Response(status_code=204)

    if operation in {"creation", "override"}:
        if not success:
            return Response(status_code=204)
        client = _nango_client()
        try:
            metadata = await client.get_connection_metadata(connection_id, provider_key)
        except NangoClientError:
            raise HTTPException(status_code=502, detail="Não foi possível validar conexão") from None
        except ValueError:
            return Response(status_code=204)
        metadata_tags = _tag_uuids(metadata.tags)
        if (
            metadata.connection_id != connection_id
            or metadata.provider_config_key != provider_key
            or metadata_tags != owner_tags
        ):
            return Response(status_code=204)

        account_email: str | None = None
        if provider == "google_gmail":
            try:
                profile = await client.proxy_request(
                    "GET", "gmail/v1/users/me/profile",
                    connection_id=connection_id,
                    provider_config_key=provider_key,
                )
            except (NangoClientError, ValueError):
                raise HTTPException(status_code=502, detail="Não foi possível validar a conta Gmail") from None
            profile_body = profile.body
            account_email = _validated_gmail_email(
                profile_body.get("emailAddress") if isinstance(profile_body, dict) else None
            )
            if account_email is None:
                raise HTTPException(status_code=502, detail="Não foi possível validar a conta Gmail")

        owner_provider_row = (await session.exec(select(IntegrationConnection).where(
            IntegrationConnection.workspace_id == workspace_id,
            IntegrationConnection.user_id == user_id,
            IntegrationConnection.provider == provider,
        ))).first()
        # A provider connection ID cannot be adopted from another local binding,
        # regardless of tenant, provider config, or webhook tags.
        id_collisions = (await session.exec(select(IntegrationConnection).where(
            IntegrationConnection.nango_connection_id == connection_id,
        ))).all()
        if any(collision is not owner_provider_row for collision in id_collisions):
            return Response(status_code=204)
        if row is None and owner_provider_row is not None:
            if owner_provider_row.workspace_id != workspace_id or owner_provider_row.user_id != user_id or owner_provider_row.provider != provider:
                return Response(status_code=204)
            row = owner_provider_row
        was_disconnected = row is not None and row.status == "disconnected"
        mailbox_changed = row is not None and row.account_email != account_email
        reset_gmail_watch = mailbox_changed or was_disconnected
        if row is None:
            row = IntegrationConnection(
                workspace_id=workspace_id,
                user_id=user_id,
                provider=cast(IntegrationProvider, provider),
                provider_config_key=provider_key,
                nango_connection_id=connection_id,
                account_email=account_email,
                status="active",
                metadata_json={},
                created_at=now,
                updated_at=now,
            )
            session.add(row)
            try:
                await session.flush()
            except IntegrityError:
                await session.rollback()
                return Response(status_code=204)
        else:
            row.status = "active"
            row.updated_at = now
            row.nango_connection_id = connection_id
            row.provider_config_key = provider_key
            if provider == "google_gmail":
                row.account_email = account_email
        if provider == "google_gmail":
            watch = (await session.exec(select(GmailWatchState).where(
                GmailWatchState.integration_connection_id == row.id,
            ))).first()
            if watch is None:
                session.add(GmailWatchState(
                    integration_connection_id=row.id,
                    status="paused",
                    updated_at=now,
                ))
            else:
                if reset_gmail_watch:
                    watch.status = "paused"
                    watch.history_id = None
                    watch.expiration_at = None
                    watch.last_renewed_at = None
                    watch.page_token = None
                    watch.updated_at = now
            if mailbox_changed:
                await _remove_gmail_policy(session, workspace_id, user_id)
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return Response(status_code=204)
        return Response(status_code=204)

    if row is None or row.user_id != user_id or row.workspace_id != workspace_id:
        return Response(status_code=204)
    if operation == "refresh":
        row.status = "active" if success else "error"
        row.updated_at = now
    elif operation == "deletion":
        row.status = "disconnected"
        row.updated_at = now
        await _pause_watch(session, row.id)
        if provider == "google_gmail":
            await _remove_gmail_policy(session, workspace_id, user_id)
    await session.commit()
    return Response(status_code=204)
