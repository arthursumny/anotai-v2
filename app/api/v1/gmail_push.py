"""Public, Google-authenticated Gmail Pub/Sub push ingress."""

import asyncio
import base64
import binascii
import json
import re

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token
from sqlalchemy import func
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.config import settings
from app.core.database import get_session
from app.core.queue import enqueue_gmail_history_job
from app.models.gmail_watch_state import GmailWatchState
from app.models.integration_connection import IntegrationConnection
from app.models.source_policy import SourceMonitoringPolicy
from app.models.workspace import WorkspaceMembership

router = APIRouter()
_MAX_BODY_BYTES = 64 * 1024
_HISTORY_ID = re.compile(r"^[0-9]+$")


async def _verify_push_token(authorization: str | None) -> None:
    audience = settings.GMAIL_PUSH_AUDIENCE
    service_account = settings.GMAIL_PUSH_SA_EMAIL
    if not audience or not service_account:
        raise HTTPException(status_code=503, detail="Gmail push authentication is not configured")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Invalid push authorization")
    token = authorization[len("Bearer "):].strip()
    if not token:
        raise HTTPException(status_code=401, detail="Invalid push authorization")
    try:
        claims = await asyncio.to_thread(
            id_token.verify_oauth2_token,
            token,
            GoogleAuthRequest(),
            audience=audience,
        )
        issuer = claims.get("iss")
        if issuer not in {"accounts.google.com", "https://accounts.google.com"}:
            raise ValueError("invalid issuer")
        if claims.get("aud") != audience:
            raise ValueError("invalid audience")
        if claims.get("email") != service_account or claims.get("email_verified") is not True:
            raise ValueError("invalid service account")
    except Exception:
        # Do not include token claims or provider exception data in responses/logs.
        raise HTTPException(status_code=401, detail="Invalid push authorization") from None


@router.post("/push", status_code=204)
async def gmail_push(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    await _verify_push_token(request.headers.get("authorization"))

    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > _MAX_BODY_BYTES:
                raise HTTPException(status_code=413, detail="Request body too large")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Malformed Pub/Sub message") from exc
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Request body too large")

    try:
        envelope = json.loads(body)
        data = envelope["message"]["data"]
        if not isinstance(data, str):
            raise ValueError("invalid data")
        decoded = base64.b64decode(data, validate=True)
        payload = json.loads(decoded)
        email = payload["emailAddress"]
        history_id = payload["historyId"]
        if not isinstance(email, str) or not isinstance(history_id, str):
            raise ValueError("invalid payload fields")
        email = email.strip().casefold()
        if not email or not _HISTORY_ID.fullmatch(history_id):
            raise ValueError("invalid payload fields")
    except (KeyError, TypeError, ValueError, UnicodeDecodeError, binascii.Error, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="Malformed Pub/Sub message") from None

    query = (
        select(IntegrationConnection)
        .join(
            GmailWatchState,
            GmailWatchState.integration_connection_id == IntegrationConnection.id,
        )
        .join(
            SourceMonitoringPolicy,
            (SourceMonitoringPolicy.workspace_id == IntegrationConnection.workspace_id)
            & (SourceMonitoringPolicy.user_id == IntegrationConnection.user_id)
            & (SourceMonitoringPolicy.source == "gmail"),
        )
        .join(
            WorkspaceMembership,
            (WorkspaceMembership.workspace_id == IntegrationConnection.workspace_id)
            & (WorkspaceMembership.user_id == IntegrationConnection.user_id),
        )
        .where(
            IntegrationConnection.provider == "google_gmail",
            IntegrationConnection.status == "active",
            func.lower(func.trim(IntegrationConnection.account_email)) == email,
            GmailWatchState.status == "active",
        )
    )
    connections = (await session.exec(query)).all()
    for connection in connections:
        try:
            await enqueue_gmail_history_job(str(connection.id), history_id)
        except Exception:
            raise HTTPException(status_code=503, detail="Gmail push temporarily unavailable") from None

    return Response(status_code=204)
