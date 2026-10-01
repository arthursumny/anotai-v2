"""Secure WhatsApp Evolution Go connection bootstrap and webhook boundary."""

import ipaddress
import json
import logging
import secrets
import uuid
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.core.database import get_session
from app.core.deps import get_current_user
from app.core.whatsapp_crypto import (
    WhatsAppCryptoError,
    decrypt_instance_token,
    encrypt_instance_token,
    hash_webhook_secret,
    verify_webhook_secret,
)
from app.models.source_policy import SourceMonitoringPolicy
from app.models.user import User
from app.models.whatsapp_instance import WhatsAppInstance
from app.models.workspace import Workspace, WorkspaceMembership
from app.schemas.whatsapp import (
    WhatsAppConnectResponse,
    WhatsAppDisconnectResponse,
    WhatsAppQrResponse,
    WhatsAppStatusResponse,
    WhatsAppMetadataResponse,
)
from app.services.evolution_go_client import EvolutionGoAdapter, EvolutionGoError
from app.services.whatsapp_privacy import purge_whatsapp_data
from app.services.conversation_intelligence import process_authorized_conversation_event
from app.services.event_ingestion import EventCandidate, ingest, whatsapp_policy_allows
from app.services.intelligence import classify_event
from app.services.intelligence_materializer import materialize_results
from app.services.whatsapp_event_parser import parse_whatsapp_event
from app.schemas.intelligence import IntelligenceType
from app.services.audit import write_audit

logger = logging.getLogger(__name__)
router = APIRouter()
MAX_WEBHOOK_BYTES = 64 * 1024


def _is_private_http(url: str) -> bool:
    parsed = urlsplit(url)
    host = parsed.hostname
    if parsed.scheme != "http" or not host:
        return False
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(host)
        return address.is_private or address.is_loopback
    except ValueError:
        return False


def _webhook_url(instance_id: uuid.UUID, secret: str) -> str:
    base = settings.WHATSAPP_WEBHOOK_BASE_URL.strip().rstrip("/")
    parsed = urlsplit(base)
    dev_http = settings.ENV == "dev" and _is_private_http(base)
    if (
        not base or parsed.scheme not in ({"https", "http"} if dev_http else {"https"})
        or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
    ):
        raise HTTPException(status_code=503, detail="WhatsApp não está configurado.")
    return f"{base}/api/v1/whatsapp/webhook/{instance_id}/{secret}"


async def get_evolution_adapter():
    base_url = settings.WHATSAPP_EVOLUTION_BASE_URL.strip()
    api_key = settings.WHATSAPP_EVOLUTION_GLOBAL_API_KEY.strip()
    if not base_url or not api_key:
        raise HTTPException(status_code=503, detail="WhatsApp não está configurado.")
    client = httpx.AsyncClient()
    try:
        try:
            adapter = EvolutionGoAdapter(
                api_key,
                base_url=base_url,
                client=client,
                allow_insecure_private=settings.ENV == "dev" and _is_private_http(base_url),
            )
        except ValueError:
            raise HTTPException(status_code=503, detail="WhatsApp não está configurado.") from None
        yield adapter
    finally:
        await client.aclose()


async def get_optional_evolution_adapter():
    """Yield a provider when configured; local disconnect must not depend on it."""
    base_url = settings.WHATSAPP_EVOLUTION_BASE_URL.strip()
    api_key = settings.WHATSAPP_EVOLUTION_GLOBAL_API_KEY.strip()
    if not base_url or not api_key:
        yield None
        return
    client = httpx.AsyncClient()
    try:
        try:
            adapter = EvolutionGoAdapter(
                api_key,
                base_url=base_url,
                client=client,
                allow_insecure_private=settings.ENV == "dev" and _is_private_http(base_url),
            )
        except ValueError:
            yield None
            return
        yield adapter
    finally:
        await client.aclose()


async def _workspace_id(session: AsyncSession, current: User) -> uuid.UUID:
    workspace_id = current.workspace_id
    if workspace_id is None:
        raise HTTPException(status_code=404, detail="Workspace não encontrado.")
    membership = await session.exec(select(WorkspaceMembership.id).where(
        WorkspaceMembership.workspace_id == workspace_id,
        WorkspaceMembership.user_id == current.id,
    ))
    if membership.first() is None:
        raise HTTPException(status_code=404, detail="Workspace não encontrado.")
    return workspace_id


async def _owned_instance(session: AsyncSession, current: User) -> WhatsAppInstance:
    workspace_id = await _workspace_id(session, current)
    result = await session.exec(select(WhatsAppInstance).where(
        WhatsAppInstance.workspace_id == workspace_id,
        WhatsAppInstance.user_id == current.id,
    ))
    instance = result.first()
    if instance is None:
        raise HTTPException(status_code=404, detail="Conexão WhatsApp não encontrada.")
    return instance


def _token(instance: WhatsAppInstance) -> str:
    try:
        return decrypt_instance_token(instance.id, instance.token_ciphertext)
    except WhatsAppCryptoError:
        raise HTTPException(status_code=503, detail="Credenciais WhatsApp indisponíveis.") from None


@router.post("/connect", response_model=WhatsAppConnectResponse, status_code=201)
async def connect(
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
    adapter: EvolutionGoAdapter = Depends(get_evolution_adapter),
) -> WhatsAppConnectResponse:
    workspace_id = await _workspace_id(session, current)
    if not settings.WHATSAPP_TOKEN_ENCRYPTION_KEY.strip():
        raise HTTPException(status_code=503, detail="WhatsApp não está configurado.")
    result = await session.exec(select(WhatsAppInstance).where(
        WhatsAppInstance.workspace_id == workspace_id,
        WhatsAppInstance.user_id == current.id,
    ))
    instance = result.first()
    if instance is not None and instance.status not in {"disconnected", "error"}:
        raise HTTPException(status_code=409, detail="Já existe uma conexão WhatsApp.")
    if instance is None:
        instance_id = uuid.uuid4()
        instance = WhatsAppInstance(
            id=instance_id,
            workspace_id=workspace_id,
            user_id=current.id,
            go_instance_name=f"anotai-{instance_id.hex}",
            token_ciphertext="pending",
            webhook_secret_hash="0" * 64,
        )
    else:
        instance_id = instance.id
        instance.go_instance_name = f"anotai-{uuid.uuid4().hex}"
    token = secrets.token_urlsafe(32)
    webhook_secret = secrets.token_urlsafe(32)
    webhook = _webhook_url(instance_id, webhook_secret)
    try:
        instance.token_ciphertext = encrypt_instance_token(instance_id, token)
    except WhatsAppCryptoError:
        raise HTTPException(status_code=503, detail="WhatsApp não está configurado.") from None
    instance.webhook_secret_hash = hash_webhook_secret(webhook_secret)
    instance.status = "pending_qr"
    instance.phone_jid = None
    instance.updated_at = datetime.now(timezone.utc)
    session.add(instance)
    await session.commit()
    try:
        await adapter.create_instance(instance.go_instance_name, token, webhook=webhook)
    except (EvolutionGoError, ValueError):
        instance.status = "error"
        instance.updated_at = datetime.now(timezone.utc)
        session.add(instance)
        await session.commit()
        logger.warning("WhatsApp Evolution instance creation failed")
        raise HTTPException(status_code=502, detail="Não foi possível iniciar a conexão WhatsApp.") from None
    return WhatsAppConnectResponse(status="pending_qr", instance_id=str(instance.id))


@router.get("/qr", response_model=WhatsAppQrResponse)
async def qr(
    response: Response,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
    adapter: EvolutionGoAdapter = Depends(get_evolution_adapter),
) -> WhatsAppQrResponse:
    instance = await _owned_instance(session, current)
    was_disconnected = instance.status == "disconnected"
    if instance.status not in {"pending_qr", "connected"}:
        raise HTTPException(status_code=409, detail="Conexão WhatsApp indisponível.")
    try:
        result = await adapter.get_qr(_token(instance))
    except (EvolutionGoError, ValueError):
        raise HTTPException(status_code=502, detail="Não foi possível obter o QR WhatsApp.") from None
    response.headers["Cache-Control"] = "no-store"
    return WhatsAppQrResponse(qr=result.code)


@router.get("/status", response_model=WhatsAppStatusResponse)
async def get_status(
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
    adapter: EvolutionGoAdapter = Depends(get_evolution_adapter),
) -> WhatsAppStatusResponse:
    instance = await _owned_instance(session, current)
    try:
        result = await adapter.get_status(_token(instance))
    except (EvolutionGoError, ValueError):
        raise HTTPException(status_code=502, detail="Não foi possível consultar a conexão WhatsApp.") from None
    instance.status = "connected" if result.connected and result.logged_in else "pending_qr"
    instance.phone_jid = result.my_jid
    instance.updated_at = datetime.now(timezone.utc)
    session.add(instance)
    await session.commit()
    phone = result.my_jid.split("@", 1)[0] if result.my_jid else None
    if phone and len(phone) > 4:
        phone = f"{phone[:2]}***{phone[-2:]}"
    elif phone:
        phone = "***"
    return WhatsAppStatusResponse(status=instance.status, phone=phone)


@router.delete("/disconnect", response_model=WhatsAppDisconnectResponse)
async def disconnect(
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
    adapter: EvolutionGoAdapter | None = Depends(get_optional_evolution_adapter),
) -> WhatsAppDisconnectResponse:
    instance = await _owned_instance(session, current)
    was_disconnected = instance.status == "disconnected"
    token = None
    try:
        token = decrypt_instance_token(instance.id, instance.token_ciphertext)
    except WhatsAppCryptoError:
        pass
    instance.status = "disconnected"
    instance.phone_jid = None
    instance.updated_at = datetime.now(timezone.utc)
    policies = await session.exec(select(SourceMonitoringPolicy).where(
        SourceMonitoringPolicy.workspace_id == instance.workspace_id,
        SourceMonitoringPolicy.user_id == instance.user_id,
        SourceMonitoringPolicy.source == "whatsapp",
    ))
    for row in policies.all():
        await session.delete(row)
    await purge_whatsapp_data(session, instance.workspace_id, instance.user_id)
    session.add(instance)
    if not was_disconnected:
        await write_audit(
            session,
            workspace_id=instance.workspace_id,
            user_id=current.id,
            event="WHATSAPP_DISCONNECTED",
            action="disconnect",
            status="success",
            source="whatsapp",
            target_kind="whatsapp_instance",
            target_ref=str(instance.id),
            metadata={"reason_code": "disconnect", "count": 1},
        )
    await session.commit()
    remote_logout_ok = False
    if adapter is not None and token is not None:
        try:
            await adapter.logout(token)
            remote_logout_ok = True
        except Exception:
            # Remote error text may contain credentials or provider data.
            logger.warning("WhatsApp remote logout could not be confirmed")
    return WhatsAppDisconnectResponse(
        status="disconnected",
        message=(
            "Sessão desconectada; a instância GO pode precisar de limpeza."
            if remote_logout_ok else
            "Não foi possível confirmar a saída no WhatsApp; remova o aparelho vinculado nas configurações do WhatsApp."
        ),
    )


async def _list_metadata(
    *,
    is_group: bool,
    response: Response,
    session: AsyncSession,
    current: User,
    adapter: EvolutionGoAdapter,
) -> list[WhatsAppMetadataResponse]:
    instance = await _owned_instance(session, current)
    if instance.status != "connected":
        raise HTTPException(status_code=409, detail="Conexão WhatsApp indisponível.")
    try:
        token = _token(instance)
        entries = await (adapter.list_groups(token) if is_group else adapter.list_contacts(token))
    except (EvolutionGoError, ValueError):
        logger.warning("WhatsApp metadata request failed")
        raise HTTPException(status_code=502, detail="Não foi possível consultar metadados WhatsApp.") from None
    response.headers["Cache-Control"] = "no-store"
    return [
        WhatsAppMetadataResponse(jid=item.jid, display_name=item.display_name, is_group=item.is_group)
        for item in entries
    ]


@router.get("/contacts", response_model=list[WhatsAppMetadataResponse])
async def list_contacts(
    response: Response,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
    adapter: EvolutionGoAdapter = Depends(get_evolution_adapter),
) -> list[WhatsAppMetadataResponse]:
    return await _list_metadata(
        is_group=False, response=response, session=session, current=current, adapter=adapter
    )


@router.get("/groups", response_model=list[WhatsAppMetadataResponse])
async def list_groups(
    response: Response,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
    adapter: EvolutionGoAdapter = Depends(get_evolution_adapter),
) -> list[WhatsAppMetadataResponse]:
    return await _list_metadata(
        is_group=True, response=response, session=session, current=current, adapter=adapter
    )


@router.post("/webhook/{instance_id}/{secret}", status_code=204)
async def webhook(instance_id: uuid.UUID, secret: str, request: Request, session: AsyncSession = Depends(get_session)) -> Response:
    # Go-live gate: configure reverse-proxy and Evolution logs to scrub this
    # path parameter. Uvicorn access logging is disabled in docker-compose.
    try:
        instance = await session.get(WhatsAppInstance, instance_id)
    except Exception:
        await session.rollback()
        logger.warning("WhatsApp webhook lookup failed")
        raise HTTPException(status_code=503, detail="Webhook temporariamente indisponível.") from None
    if (
        instance is None
        or instance.status not in {"pending_qr", "connected"}
        or not verify_webhook_secret(secret, instance.webhook_secret_hash)
    ):
        raise HTTPException(status_code=404, detail="Not found")
    size = 0
    body = bytearray()
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="Payload too large")
        body.extend(chunk)
    try:
        payload = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        return Response(status_code=204)
    message = parse_whatsapp_event(payload)
    if message is None or message.instance != instance.go_instance_name:
        return Response(status_code=204)

    candidate = EventCandidate(
        workspace_id=instance.workspace_id,
        user_id=instance.user_id,
        source="whatsapp",
        external_id=f"{instance.id}:{message.message_id}",
        type="MESSAGE_SENT" if message.from_me else "MESSAGE_RECEIVED",
        conversation_id=message.conversation_id,
        sender=message.sender,
        occurred_at=message.timestamp,
        content=message.content,
        from_me=message.from_me,
        is_group=message.is_group,
    )
    try:
        membership = await session.exec(select(WorkspaceMembership.id).where(
            WorkspaceMembership.workspace_id == instance.workspace_id,
            WorkspaceMembership.user_id == instance.user_id,
        ))
        if membership.first() is None:
            return Response(status_code=204)
        event = await ingest(candidate, session)
        if event is None:
            await session.rollback()
            return Response(status_code=204)
        if event.status == "processed":
            await session.rollback()
            return Response(status_code=204)

        workspace = await session.get(Workspace, instance.workspace_id)
        if workspace is None:
            await session.rollback()
            return Response(status_code=204)
        results = await classify_event(
            message.content,
            occurred_at=message.timestamp,
            timezone=workspace.timezone,
            locale=workspace.locale,
            context={"source": "whatsapp", "direction": "outgoing" if message.from_me else "incoming"},
        )
        if not await whatsapp_policy_allows(candidate, session):
            await session.rollback()
            return Response(status_code=204)
        materializable = [result for result in results if result.type is not IntelligenceType.FOLLOW_UP]
        await materialize_results(session, event, materializable, timezone=workspace.timezone)
        if not await whatsapp_policy_allows(candidate, session):
            await session.rollback()
            return Response(status_code=204)
        await process_authorized_conversation_event(session, event, instance, results)
        await session.commit()
    except Exception:
        await session.rollback()
        # Do not log exception text: provider/DB errors may contain message data.
        logger.warning("WhatsApp webhook processing failed")
        raise HTTPException(status_code=503, detail="Webhook temporariamente indisponível.") from None
    return Response(status_code=204)
