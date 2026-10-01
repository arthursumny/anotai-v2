"""Scheduled renewal of explicitly authorized Gmail push watches."""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_
from sqlmodel import select

from app.core.config import settings
from app.core.database import async_session_factory
from app.models.gmail_watch_state import GmailWatchState
from app.models.integration_connection import IntegrationConnection
from app.models.source_policy import SourceMonitoringPolicy
from app.models.workspace import WorkspaceMembership
from app.services.gmail_ingress import GmailSourceProvider

logger = logging.getLogger("anotai.gmail_watch_jobs")


async def renew_gmail_watches_job(ctx: dict[str, Any]) -> dict[str, int]:
    """Renew at most 25 opted-in Gmail watches without logging account data."""
    if not (
        settings.NANGO_ENV_KEY.strip()
        and settings.NANGO_BASE_URL.strip()
        and settings.GMAIL_PUBSUB_TOPIC.strip()
    ):
        logger.warning("Gmail watch renewal skipped: integration is not configured")
        return {"scanned": 0, "renewed": 0, "failed": 0, "skipped": 0}

    now = datetime.now(timezone.utc)
    try:
        async with async_session_factory() as session:
            rows = (await session.exec(
                select(IntegrationConnection)
                .join(
                    WorkspaceMembership,
                    and_(
                        WorkspaceMembership.workspace_id == IntegrationConnection.workspace_id,
                        WorkspaceMembership.user_id == IntegrationConnection.user_id,
                    ),
                )
                .join(
                    SourceMonitoringPolicy,
                    and_(
                        SourceMonitoringPolicy.workspace_id == IntegrationConnection.workspace_id,
                        SourceMonitoringPolicy.user_id == IntegrationConnection.user_id,
                        SourceMonitoringPolicy.source == "gmail",
                    ),
                )
                .outerjoin(
                    GmailWatchState,
                    GmailWatchState.integration_connection_id == IntegrationConnection.id,
                )
                .where(
                    IntegrationConnection.provider == "google_gmail",
                    IntegrationConnection.status == "active",
                    IntegrationConnection.account_email != None,  # noqa: E711
                    or_(
                        GmailWatchState.id == None,  # noqa: E711
                        GmailWatchState.status == "paused",
                        and_(
                            GmailWatchState.status == "active",
                            or_(
                                GmailWatchState.expiration_at == None,  # noqa: E711
                                GmailWatchState.expiration_at <= now + timedelta(hours=48),
                                GmailWatchState.last_renewed_at == None,  # noqa: E711
                                GmailWatchState.last_renewed_at <= now - timedelta(
                                    hours=settings.GMAIL_WATCH_RENEWAL_INTERVAL_HOURS,
                                ),
                            ),
                        ),
                    ),
                )
                .order_by(IntegrationConnection.created_at.asc(), IntegrationConnection.id.asc())
                .limit(25)
            )).all()
            due = [connection.id for connection in rows]

            if not due:
                return {"scanned": len(rows), "renewed": 0, "failed": 0, "skipped": len(rows)}

            from app.services.nango_client import NangoClient

            provider = GmailSourceProvider(NangoClient(
                api_key=settings.NANGO_ENV_KEY,
                base_url=settings.NANGO_BASE_URL,
            ))
            renewed = failed = 0
            for connection_id in due:
                try:
                    # A rollback expires ORM instances; reload each owner before use.
                    connection = await session.get(IntegrationConnection, connection_id)
                    if connection is None:
                        raise RuntimeError("Eligible connection disappeared")
                    await provider.renew_watch(session, connection)
                    await session.commit()
                    renewed += 1
                except Exception:
                    await session.rollback()
                    failed += 1
                    logger.warning("Gmail watch renewal failed for an eligible connection")
            return {
                "scanned": len(rows),
                "renewed": renewed,
                "failed": failed,
                "skipped": len(rows) - len(due),
            }
    except Exception:
        logger.warning("Gmail watch renewal job failed")
        return {"scanned": 0, "renewed": 0, "failed": 1, "skipped": 0}


async def reconcile_gmail_histories_job(ctx: dict[str, Any]) -> dict[str, int]:
    """Poll bounded Gmail history as a fallback for lost Pub/Sub notifications."""
    if not settings.NANGO_ENV_KEY.strip() or not settings.NANGO_BASE_URL.strip():
        logger.warning("Gmail history reconciliation skipped: integration is not configured")
        return {"scanned": 0, "processed": 0, "failed": 0, "skipped": 0}

    try:
        async with async_session_factory() as session:
            rows = (await session.exec(
                select(IntegrationConnection.id)
                .join(
                    WorkspaceMembership,
                    and_(
                        WorkspaceMembership.workspace_id == IntegrationConnection.workspace_id,
                        WorkspaceMembership.user_id == IntegrationConnection.user_id,
                    ),
                )
                .join(
                    SourceMonitoringPolicy,
                    and_(
                        SourceMonitoringPolicy.workspace_id == IntegrationConnection.workspace_id,
                        SourceMonitoringPolicy.user_id == IntegrationConnection.user_id,
                        SourceMonitoringPolicy.source == "gmail",
                    ),
                )
                .join(
                    GmailWatchState,
                    GmailWatchState.integration_connection_id == IntegrationConnection.id,
                )
                .where(
                    IntegrationConnection.provider == "google_gmail",
                    IntegrationConnection.status == "active",
                    IntegrationConnection.account_email != None,  # noqa: E711
                    GmailWatchState.status == "active",
                    GmailWatchState.history_id != None,  # noqa: E711
                )
                .order_by(GmailWatchState.updated_at.asc(), IntegrationConnection.id.asc())
                .limit(25)
            )).all()
            connection_ids = [row for row in rows]
            if not connection_ids:
                return {"scanned": 0, "processed": 0, "failed": 0, "skipped": 0}

            from app.services.nango_client import NangoClient

            provider = GmailSourceProvider(NangoClient(
                api_key=settings.NANGO_ENV_KEY,
                base_url=settings.NANGO_BASE_URL,
            ))
            processed = failed = 0
            for connection_id in connection_ids:
                try:
                    connection = await session.get(IntegrationConnection, connection_id)
                    if connection is None:
                        raise RuntimeError("Eligible connection disappeared")
                    await provider.process_history(session, connection)
                    state = (await session.exec(select(GmailWatchState).where(
                        GmailWatchState.integration_connection_id == connection_id,
                    ))).first()
                    if state is not None:
                        state.updated_at = datetime.now(timezone.utc)
                    await session.commit()
                    processed += 1
                except Exception:
                    await session.rollback()
                    failed += 1
                    logger.warning("Gmail history reconciliation failed for an eligible connection")
            return {
                "scanned": len(connection_ids),
                "processed": processed,
                "failed": failed,
                "skipped": 0,
            }
    except Exception:
        logger.warning("Gmail history reconciliation job failed")
        return {"scanned": 0, "processed": 0, "failed": 1, "skipped": 0}
