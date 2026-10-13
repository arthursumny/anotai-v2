"""AnotAI — worker ARQ: pipeline STT -> router/extractor -> dispatcher + cron de lembretes."""

import anyio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from arq import cron
from sqlalchemy import delete
from sqlmodel import select
from telegram import Bot
from telegram.error import TelegramError

from app.core.config import settings
from app.core.database import async_session_factory
from app.core.queue import redis_settings
from app.models.execution_log import ExecutionLog, ExecutionStatus
from app.models.automation_settings import AutomationSettings
from app.models.audit_event import AuditEvent
from app.models.gmail_watch_state import GmailWatchState
from app.models.integration_connection import IntegrationConnection
from app.models.note import Note, NoteStatus
from app.models.source_policy import SourceMonitoringPolicy
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMembership
from app.services.audio_check import check_audible
from app.services.dispatcher import build_secret_scope, load_user_secrets
from app.services.note_pipeline import process_transcription
from app.services.reminder_scheduler import (
    process_followup_due,
    process_reminder_due,
    reconcile_due_items as reconcile_due_items_in_session,
)
from app.services.conversation_intelligence import materialize_due_conversation_followups
from app.services.stt import STTError, get_stt_provider
from app.services.document_recovery import reconcile_missing_documents
from app.services.action_executor import process_pending_actions
from app.services.notification_prefs import should_notify
from app.services.gmail_ingress import GmailSourceProvider
from app.services.nango_client import NangoClient
from app.services.gmail_watch_jobs import reconcile_gmail_histories_job, renew_gmail_watches_job
from app.services.privacy_retention import purge_expired_events
from app.services.action_hygiene import cleanup_action_executions

logger = logging.getLogger("anotai.worker")


def _ensure_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


async def process_audio_job(
    ctx: dict, note_id: str, audio_path: str, user_id: str,
) -> dict:
    """Transcribe once, save a complete document, then prepare its durable actions."""
    path = Path(audio_path)
    try:
        nid, uid = uuid.UUID(note_id), uuid.UUID(user_id)
    except ValueError:
        return {"ok": False, "error": "IDs inválidos"}
    async with async_session_factory() as session:
        note = await session.get(Note, nid)
        if note is None or note.user_id != uid:
            return {"ok": False, "error": "Nota não encontrada"}
        try:
            if not note.raw_transcription.strip():
                audible, reason = await anyio.to_thread.run_sync(check_audible, path)
                if not audible:
                    raise STTError(f"Áudio sem som aproveitável: {reason}")
                note.raw_transcription = await get_stt_provider().transcribe(path)
                session.add(note)
                await session.commit()
            await process_transcription(session, note)
            redis = ctx.get("redis")
            if redis is not None:
                await redis.enqueue_job("run_pending_actions_job")
            return {"ok": True, "status": "processed" if note.template_id else "general"}
        except Exception as exc:
            # ARQ boundary: persist a visible error without inventing a document.
            logger.exception("Falha no processamento da nota %s", note_id)
            await session.rollback()
            await session.refresh(note)
            if not note.body:
                note.status = NoteStatus.PENDING_REVIEW
                session.add(note)
            session.add(ExecutionLog(
                note_id=note.id, action_type="pipeline", status=ExecutionStatus.FAILED,
                payload={}, error_message=str(exc)[:4000],
            ))
            await session.commit()
            return {"ok": False, "error": str(exc)}
        finally:
            path.unlink(missing_ok=True)


async def _send_reminder_telegram(note: Note, session: Any) -> dict[str, Any]:
    """Telegram direto p/ lembretes (usa segredos do dono da nota)."""
    secrets = build_secret_scope(await load_user_secrets(session, note.user_id))
    token = secrets.get("TELEGRAM_BOT_TOKEN", "")
    chat_id = secrets.get("TELEGRAM_CHAT_ID", "")
    if not token or "${" in token or not chat_id or "${" in chat_id:
        raise RuntimeError("Telegram não conectado: conecte seu chat em Segredos")
    due = "sem prazo definido"
    if note.due_date:
        due_at = _ensure_aware(note.due_date)
        if due_at is not None:
            try:
                due = due_at.astimezone(ZoneInfo(settings.DEFAULT_TIMEZONE)).strftime("%d/%m/%Y às %H:%M")
            except Exception:
                due = due_at.strftime("%d/%m/%Y às %H:%M UTC")
    title = note.title or "Tarefa"
    text = f"Lembrete Anotaí: {title}\nPrazo: {due}\nContexto: {note.raw_transcription or ''}"[:4000]
    bot = Bot(token=token)
    try:
        msg = await bot.send_message(chat_id=chat_id, text=text)
        return {"message_id": msg.message_id, "chat_id": chat_id}
    except TelegramError as exc:
        raise RuntimeError(f"Telegram API: {exc}") from exc


async def check_scheduled_reminders(ctx: dict) -> dict:
    """Cron ARQ (a cada 2 min): dispara lembretes vencidos ainda não enviados.

    Alvos: `reminder_at <= now`, `status == processed`, `reminder_sent == False`.
    Sucesso marca `reminder_sent`; falha mantém para nova tentativa no próximo
    ciclo. Nunca levanta.
    """
    now = datetime.now(timezone.utc)
    sent, failed, skipped = 0, 0, 0
    async with async_session_factory() as session:
        try:
            result = await session.exec(
                select(Note).where(
                    Note.reminder_sent == False,  # noqa: E712
                    Note.status == NoteStatus.PROCESSED,
                    Note.reminder_at.is_not(None),
                )
            )
            candidates = list(result.all())
        except Exception:
            logger.exception("Falha ao buscar lembretes")
            return {"ok": False, "error": "query"}
        for note in candidates:
            moment = _ensure_aware(note.reminder_at)
            if moment is None or moment > now:
                skipped += 1
                continue
            user = await session.get(User, note.user_id)
            workspace_id = user.workspace_id if user is not None else None
            if workspace_id is not None:
                membership_id = (await session.exec(select(WorkspaceMembership.id).where(
                    WorkspaceMembership.workspace_id == workspace_id,
                    WorkspaceMembership.user_id == note.user_id,
                ))).first()
                workspace = await session.get(Workspace, workspace_id)
                if membership_id is None or workspace is None:
                    skipped += 1
                    continue
                prefs = (await session.exec(select(AutomationSettings).where(
                    AutomationSettings.workspace_id == workspace_id,
                    AutomationSettings.user_id == note.user_id,
                ))).first()
                if not should_notify(
                    now,
                    workspace.timezone,
                    notifications_enabled=prefs.notifications_enabled if prefs else True,
                    quiet_start_minute=prefs.quiet_start_minute if prefs else None,
                    quiet_end_minute=prefs.quiet_end_minute if prefs else None,
                ):
                    skipped += 1
                    continue
            try:
                await _send_reminder_telegram(note, session)
                note.reminder_sent = True
                session.add(note)
                await session.commit()
                sent += 1
            except Exception as exc:  # noqa: BLE001 — próximo ciclo tenta de novo
                logger.warning("Lembrete %s falhou: %s", note.id, exc)
                await session.rollback()
                failed += 1
    return {"ok": True, "sent": sent, "failed": failed, "skipped": skipped}


async def reminder_due_job(ctx: dict, reminder_id: str, expected_target: str) -> dict:
    """Delayed ARQ wake-up; SQL reminder status/target remain authoritative."""
    try:
        async with async_session_factory() as session:
            try:
                return await process_reminder_due(session, reminder_id, expected_target)
            except Exception as exc:
                await session.rollback()
                logger.warning("Reminder due item %s failed (%s)", reminder_id, type(exc).__name__)
                return {"ok": False, "error": "processing_failed"}
    except Exception as exc:
        logger.warning("Reminder due item %s failed (%s)", reminder_id, type(exc).__name__)
        return {"ok": False, "error": "processing_failed"}


async def followup_due_job(ctx: dict, followup_id: str, expected_target: str) -> dict:
    """Delayed ARQ wake-up; resolved or stale follow-ups are no-ops."""
    try:
        async with async_session_factory() as session:
            try:
                return await process_followup_due(session, followup_id, expected_target)
            except Exception as exc:
                await session.rollback()
                logger.warning("Follow-up due item %s failed (%s)", followup_id, type(exc).__name__)
                return {"ok": False, "error": "processing_failed"}
    except Exception as exc:
        logger.warning("Follow-up due item %s failed (%s)", followup_id, type(exc).__name__)
        return {"ok": False, "error": "processing_failed"}


async def reconcile_due_items(ctx: dict) -> dict:
    """Periodic recovery scan for due durable items whose Redis wake-up was lost."""
    try:
        async with async_session_factory() as session:
            try:
                return await reconcile_due_items_in_session(session)
            except Exception as exc:
                await session.rollback()
                logger.warning("Due-item reconciliation failed (%s)", type(exc).__name__)
                return {
                    "reminders_scanned": 0,
                    "followups_scanned": 0,
                    "processed": 0,
                    "failed": 1,
                }
    except Exception as exc:
        logger.warning("Due-item reconciliation failed (%s)", type(exc).__name__)
        return {
            "reminders_scanned": 0,
            "followups_scanned": 0,
            "processed": 0,
            "failed": 1,
        }


async def check_pending_conversation_items_job(ctx: dict) -> dict[str, Any]:
    """Materialize at most 100 eligible conversation follow-ups; never sends messages."""
    try:
        async with async_session_factory() as session:
            try:
                created = await materialize_due_conversation_followups(
                    session, now=datetime.now(timezone.utc), batch_size=100,
                )
                await session.commit()
                return {"ok": True, "created": created}
            except Exception as exc:
                await session.rollback()
                logger.warning("Pending conversation follow-up scan failed (%s)", type(exc).__name__)
                return {"ok": False, "error": "processing_failed"}
    except Exception as exc:
        logger.warning("Pending conversation follow-up scan failed (%s)", type(exc).__name__)
        return {"ok": False, "error": "processing_failed"}


async def run_pending_actions_job(ctx: dict) -> dict[str, int]:
    """ARQ tick for durable workflow action executions; never logs action data."""
    try:
        return await process_pending_actions(async_session_factory, limit=100)
    except Exception:
        logger.warning("Falha ao processar actions pendentes")
        return {"succeeded": 0, "retry_scheduled": 0, "failed": 0, "skipped": 0}


async def reconcile_missing_documents_job(ctx: dict) -> dict[str, int]:
    """Recover processed intentional audio notes missing document snapshots."""
    async with async_session_factory() as session:
        try:
            return await reconcile_missing_documents(session, limit=100)
        except Exception:
            await session.rollback()
            logger.warning("Falha na recuperação de documentos de áudio")
            return {"scanned": 0, "recovered": 0, "skipped": 0, "failed": 1}


async def privacy_retention_job(ctx: dict) -> dict[str, Any]:
    """Daily bounded retention for normalized events and audit records."""
    totals: dict[str, int] = {}
    failed = 0
    more_events_may_remain = False
    try:
        now = datetime.now(timezone.utc)
        async with async_session_factory() as session:
            try:
                for batch_index in range(10):
                    batch = await purge_expired_events(session, now=now, batch_size=100)
                    for key, value in batch.items():
                        totals[key] = totals.get(key, 0) + value
                    failed += batch.get("failed", 0)
                    await session.commit()
                    if batch.get("failed", 0) or batch.get("events", 0) < 100:
                        break
                    if batch_index == 9:
                        more_events_may_remain = True

                audit_cutoff = now - timedelta(days=settings.AUDIT_RETENTION_DAYS)
                audit_ids = (await session.exec(
                    select(AuditEvent.id)
                    .where(AuditEvent.created_at < audit_cutoff)
                    .order_by(AuditEvent.created_at, AuditEvent.id)
                    .limit(1000)
                )).all()
                if audit_ids:
                    result = await session.execute(delete(AuditEvent).where(AuditEvent.id.in_(audit_ids)))
                    totals["audit_events"] = result.rowcount or 0
                else:
                    totals["audit_events"] = 0
                await session.commit()
            except Exception:
                await session.rollback()
                logger.warning("Privacy retention job failed")
                return {"ok": False, "error": "processing_failed", **totals}
    except Exception:
        logger.warning("Privacy retention job failed")
        return {"ok": False, "error": "processing_failed", **totals}

    if failed:
        logger.warning("Privacy retention incomplete: failed=%d", failed)
    else:
        logger.info(
            "Privacy retention complete: events=%d audit_events=%d",
            totals.get("events", 0), totals.get("audit_events", 0),
        )
    return {
        "ok": failed == 0 and not more_events_may_remain,
        "failed": failed,
        "more_events_may_remain": more_events_may_remain,
        **totals,
    }


async def action_hygiene_job(ctx: dict) -> dict[str, Any]:
    """Daily bounded cleanup for stale actions and old successful inputs."""
    try:
        async with async_session_factory() as session:
            try:
                changed = await cleanup_action_executions(
                    session, now=datetime.now(timezone.utc), batch_size=100,
                )
                await session.commit()
                return {"ok": True, "changed": changed}
            except Exception:
                await session.rollback()
                logger.warning("Action hygiene job failed")
                return {"ok": False, "error": "processing_failed"}
    except Exception:
        logger.warning("Action hygiene job failed")
        return {"ok": False, "error": "processing_failed"}


async def gmail_history_job(ctx: dict, connection_id: str, history_id: str | None = None) -> dict:
    """Process the persisted Gmail cursor for one authorized local connection.

    ``history_id`` is an untrusted Pub/Sub hint and deliberately never replaces
    the durable cursor managed by ``GmailSourceProvider``.
    """
    try:
        connection_uuid = uuid.UUID(connection_id)
    except (ValueError, TypeError, AttributeError):
        return {"ok": False, "error": "invalid_connection_id"}

    if not settings.NANGO_ENV_KEY or not settings.NANGO_BASE_URL:
        return {"ok": False, "error": "nango_not_configured"}

    try:
        async with async_session_factory() as session:
            try:
                connection = await session.get(IntegrationConnection, connection_uuid)
                if (
                    connection is None
                    or connection.provider != "google_gmail"
                    or connection.status != "active"
                ):
                    return {"ok": False, "error": "connection_unavailable"}

                membership_id = (await session.exec(select(WorkspaceMembership.id).where(
                    WorkspaceMembership.workspace_id == connection.workspace_id,
                    WorkspaceMembership.user_id == connection.user_id,
                ))).first()
                policy_id = (await session.exec(select(SourceMonitoringPolicy.id).where(
                    SourceMonitoringPolicy.workspace_id == connection.workspace_id,
                    SourceMonitoringPolicy.user_id == connection.user_id,
                    SourceMonitoringPolicy.source == "gmail",
                ))).first()
                watch = (await session.exec(select(GmailWatchState).where(
                    GmailWatchState.integration_connection_id == connection.id,
                ))).first()
                if membership_id is None or policy_id is None or watch is None or watch.status != "active":
                    return {"ok": False, "error": "gmail_monitoring_unavailable"}

                provider = GmailSourceProvider(
                    NangoClient(api_key=settings.NANGO_ENV_KEY, base_url=settings.NANGO_BASE_URL)
                )
                processed = await provider.process_history(session, connection)
                return {"ok": True, "processed": processed}
            except Exception:
                await session.rollback()
                logger.warning("Gmail history job failed")
                return {"ok": False, "error": "processing_failed"}
    except Exception:
        logger.warning("Gmail history job failed")
        return {"ok": False, "error": "processing_failed"}


class WorkerSettings:
    job_timeout = 1800
    functions = [
        process_audio_job,
        gmail_history_job,
        renew_gmail_watches_job,
        reconcile_gmail_histories_job,
        check_scheduled_reminders,
        reminder_due_job,
        followup_due_job,
        reconcile_due_items,
        check_pending_conversation_items_job,
        run_pending_actions_job,
        reconcile_missing_documents_job,
        privacy_retention_job,
        action_hygiene_job,
    ]
    cron_jobs = [
        cron(check_scheduled_reminders, minute={*range(0, 60, 2)}),
        cron(reconcile_due_items, minute={*range(0, 60, 5)}, unique=True),
        cron(check_pending_conversation_items_job, minute={*range(0, 60, 5)}, unique=True),
        cron(run_pending_actions_job, minute={*range(0, 60)}, unique=True),
        cron(reconcile_missing_documents_job, minute={0, 10, 20, 30, 40, 50}, unique=True),
        cron(renew_gmail_watches_job, hour=3, minute=0, unique=True),
        cron(privacy_retention_job, hour=4, minute=0, unique=True),
        cron(action_hygiene_job, hour=4, minute=15, unique=True),
        cron(reconcile_gmail_histories_job, minute={*range(0, 60, 15)}, unique=True),
    ]
    redis_settings = redis_settings()
