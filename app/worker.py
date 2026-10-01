"""AnotAI — worker ARQ: pipeline STT -> router/extractor/contínuo -> dispatcher + cron de lembretes."""

import asyncio
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
from app.models.note import CaptureMode, KanbanColumn, Note, NoteStatus
from app.models.source_policy import SourceMonitoringPolicy
from app.models.template import Template
from app.models.template_routing_rule import TemplateRoutingRule
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMembership
from app.services.audio_check import check_audible
from app.services.continuous_extractor import ContinuousExtractionError, analyze_continuous_chunk
from app.services.dispatcher import build_secret_scope, dispatch_actions, load_user_secrets
from app.services.event_ingestion import EventCandidate, ingest as ingest_event
from app.services.extractor import ExtractionError, extract_structured
from app.services.general_note import GeneralNoteError, generate_general_note
from app.services.reminder_scheduler import (
    process_followup_due,
    process_reminder_due,
    reconcile_due_items as reconcile_due_items_in_session,
)
from app.services.conversation_intelligence import materialize_due_conversation_followups
from app.services.router import RouterRule, route_transcription
from app.services.stt import STTError, get_stt_provider
from app.services.document_pipeline import process_generated_note
from app.services.document_recovery import reconcile_missing_documents
from app.services.workflow_engine import emit_trigger
from app.services.action_executor import process_pending_actions
from app.services.notification_prefs import should_notify
from app.services.gmail_ingress import GmailSourceProvider
from app.services.nango_client import NangoClient
from app.services.gmail_watch_jobs import reconcile_gmail_histories_job, renew_gmail_watches_job
from app.services.privacy_retention import purge_expired_events
from app.services.action_hygiene import cleanup_action_executions

logger = logging.getLogger("anotai.worker")


def _parse_dt(value: Any) -> datetime | None:
    """ISO8601 (com ou sem Z) → datetime UTC. None se ausente/inválido."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text[-1] in ("Z", "z"):
        text = text[:-1] + "+00:00"
    try:
        if len(text) == 10:
            dt = datetime.fromisoformat(f"{text}T09:00:00")
        else:
            dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        try:
            dt = dt.replace(tzinfo=ZoneInfo(settings.DEFAULT_TIMEZONE))
        except Exception:
            dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _extract_task_items(structured: dict[str, Any]) -> list[Any]:
    """Encontra listas de tarefas explicitamente produzidas pela IA."""
    for key, value in structured.items():
        if key.strip().casefold() in {"tarefas", "tasks", "proximas_acoes", "next_actions"} and isinstance(value, list):
            return value[:10]
    return []


async def _create_kanban_tasks(note: Note, structured: dict[str, Any], session: Any) -> list[str]:
    """Transforma tarefas extraídas em cartões, uma vez por nota processada."""
    existing_ids = structured.get("kanban_task_ids")
    if isinstance(existing_ids, list) and existing_ids:
        return [str(item) for item in existing_ids]
    tasks = _extract_task_items(structured)
    card_ids: list[str] = []
    task_reminders: list[datetime] = []
    for index, item in enumerate(tasks):
        if isinstance(item, str):
            title = item.strip()
            due_value = None
            reminder_value = None
        elif isinstance(item, dict):
            title = str(item.get("title") or item.get("tarefa") or item.get("task") or item.get("nome") or "").strip()
            due_value = item.get("due_date") or item.get("prazo") or item.get("deadline")
            reminder_value = item.get("reminder_at") or item.get("lembrete")
        else:
            continue
        if not title:
            continue
        due_date = _parse_dt(due_value)
        reminder_at = _parse_dt(reminder_value)
        if due_date is not None:
            if reminder_at is None and due_date > datetime.now(timezone.utc):
                reminder_at = due_date - timedelta(minutes=30)
        if reminder_at is not None:
            task_reminders.append(reminder_at)
        card = Note(
            user_id=note.user_id,
            title=title[:300],
            raw_transcription=note.raw_transcription,
            structured_data={
                "description": title,
                "source_note_id": str(note.id),
                "task_index": index,
            },
            status=NoteStatus.PROCESSED,
            capture_mode=note.capture_mode,
            kanban_status=KanbanColumn.TODO.value,
            due_date=due_date,
            reminder_at=reminder_at,
        )
        session.add(card)
        await session.flush()
        card_ids.append(str(card.id))
    if card_ids:
        note.structured_data = {**structured, "kanban_task_ids": card_ids}
        # Cartões individuais no quadro; o texto completo fica na nota de origem.
        note.kanban_status = None
        note_reminder = _ensure_aware(note.reminder_at)
        if note_reminder and any(
            abs((note_reminder - _ensure_aware(task_reminder)).total_seconds()) <= 300
            for task_reminder in task_reminders
        ):
            # Evita avisos duplicados quando nota e cartão representam a mesma tarefa.
            note.reminder_at = None
        session.add(note)
    return card_ids


def _ensure_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


async def _process_continuous(
    session: Any, note: Note, uid: uuid.UUID, transcription: str
) -> dict:
    """Ramo Always-On: analisa o chunk e cria uma Note `suggested` por item.

    Sem itens acionáveis (ou LLM indisponível): remove a nota-portadora para
    não poluir a interface.
    """
    try:
        items = await analyze_continuous_chunk(transcription, {"user_id": str(uid)})
    except ContinuousExtractionError as exc:
        logger.warning("Extrator contínuo falhou p/ nota %s: %s", note.id, exc)
        items = []

    await session.delete(note)
    created_ids: list[str] = []
    for item in items:
        suggestion = Note(
            user_id=uid,
            title=str(item.get("title", ""))[:300],
            raw_transcription=transcription,
            structured_data={"description": item.get("description", "")},
            status=NoteStatus.SUGGESTED,
            capture_mode=CaptureMode.CONTINUOUS.value,
            suggested_action=str(item.get("suggested_action", "Criar Tarefa no Kanban"))[:200],
            due_date=_parse_dt(item.get("suggested_due_date")),
            reminder_at=_parse_dt(item.get("suggested_reminder_at")),
        )
        session.add(suggestion)
        # flush por item para obter os ids no retorno
        await session.flush()
        created_ids.append(str(suggestion.id))
    await session.commit()
    return {"ok": True, "status": "continuous", "suggestions": created_ids}


async def _ingest_audio_transcription(
    session: Any, note: Note, user_id: uuid.UUID, content: str, capture_mode: str
) -> None:
    """Best-effort normalized event for audio; source identity comes from DB only."""
    try:
        user = await session.get(User, user_id)
        workspace_id = user.workspace_id if user is not None else None
        if workspace_id is None:
            return
        occurred_at = note.created_at
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            occurred_at = datetime.now(timezone.utc)
        await ingest_event(
            EventCandidate(
                workspace_id=workspace_id,
                user_id=user_id,
                source="audio",
                type="AUDIO_TRANSCRIBED",
                occurred_at=occurred_at,
                content=content,
                external_id=str(note.id),
                capture_mode=capture_mode,
            ),
            session,
        )
    except Exception:  # noqa: BLE001 — event capture must not break legacy processing
        logger.warning("Áudio não foi adicionado ao histórico de eventos")


async def _workspace_routing_rules(
    session: Any, user_id: uuid.UUID, capture_mode: str
) -> tuple[uuid.UUID | None, list[RouterRule]]:
    """Load only enabled rules scoped to the user's verified workspace membership."""
    user = await session.get(User, user_id)
    workspace_id = user.workspace_id if user is not None else None
    if workspace_id is None:
        return None, []
    membership_id = (await session.exec(
        select(WorkspaceMembership.id).where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == user_id,
        )
    )).first()
    if membership_id is None:
        return None, []
    rows = (await session.exec(
        select(TemplateRoutingRule).where(
            TemplateRoutingRule.user_id == user_id,
            TemplateRoutingRule.workspace_id == workspace_id,
            TemplateRoutingRule.enabled == True,  # noqa: E712
        )
        .order_by(TemplateRoutingRule.created_at.asc(), TemplateRoutingRule.id.asc())
    )).all()
    return workspace_id, [
        RouterRule(
            template_id=rule.template_id,
            priority=rule.priority,
            workspace=workspace_id,
            context=rule.context,
            capture_mode=rule.capture_mode,
        )
        for rule in rows
    ]


async def _process_document_safely(session: Any, note: Note, user_id: uuid.UUID) -> None:
    """Materialize after the note commit without jeopardizing legacy note flow."""
    document = None
    try:
        document = await process_generated_note(session, note.id, user_id)
        await session.commit()
    except Exception:  # noqa: BLE001 — document/intelligence is best effort
        logger.warning("Documento gerado não foi persistido para a nota %s", note.id)
        try:
            await session.rollback()
            await session.refresh(note)
        except Exception:
            logger.warning("Nota %s não pôde ser recarregada após falha documental", note.id)
        return
    if document is None:
        return
    try:
        await emit_trigger(
            session,
            document.workspace_id,
            document.user_id,
            "DOCUMENT_GENERATED",
            source_id=document.id,
            context={"source": "audio", "event_type": "DOCUMENT_GENERATED"},
        )
        await session.commit()
    except Exception:  # noqa: BLE001 — workflow preparation can't undo saved note/document
        logger.warning("Workflow documental não pôde ser preparado para a nota %s", note.id)
        try:
            await session.rollback()
            await session.refresh(note)
        except Exception:
            logger.warning("Nota %s não pôde ser recarregada após falha de workflow", note.id)


async def process_audio_job(
    ctx: dict, note_id: str, audio_path: str, user_id: str, capture_mode: str = "intentional"
) -> dict:
    """Job ARQ. Retorna resumo; nunca levanta (falhas ficam na Note/ExecutionLog)."""
    path = Path(audio_path)
    mode = (capture_mode or "intentional").strip().lower()
    if mode not in (CaptureMode.INTENTIONAL.value, CaptureMode.CONTINUOUS.value):
        mode = CaptureMode.INTENTIONAL.value
    try:
        nid = uuid.UUID(note_id)
        uid = uuid.UUID(user_id)
    except ValueError:
        logger.error("IDs inválidos: note_id=%s user_id=%s", note_id, user_id)
        return {"ok": False, "error": "IDs inválidos"}

    async with async_session_factory() as session:
        note = await session.get(Note, nid)
        if note is None or note.user_id != uid:
            logger.error("Nota %s não encontrada ou não pertence ao usuário", note_id)
            return {"ok": False, "error": "Nota não encontrada"}
        try:
            # 0. Barreira de audibilidade (defesa em profundidade: o endpoint
            #    já filtra, mas jobs antigos/diretos podem trazer silêncio).
            audible, reason = await asyncio.to_thread(check_audible, path)
            if not audible:
                raise STTError(f"Áudio sem som detectado ({reason}); nada enviado ao Whisper")

            # 1. STT
            stt = get_stt_provider()
            transcription = await stt.transcribe(path)
            note.raw_transcription = transcription
            note.capture_mode = mode
            await _ingest_audio_transcription(session, note, uid, transcription, mode)

            # 1b. Ramo contínuo: extrator semântico → sugestões (sem router/templates)
            if mode == CaptureMode.CONTINUOUS.value:
                return await _process_continuous(session, note, uid, transcription)

            # 2. Router — templates do usuário
            result = await session.exec(select(Template).where(Template.user_id == uid))
            templates = list(result.all())
            workspace_id, routing_rules = await _workspace_routing_rules(session, uid, mode)
            if workspace_id is None:
                # Workspace-less accounts retain the exact legacy routing path.
                routing = await route_transcription(transcription, templates)
            else:
                routing = await route_transcription(
                    transcription,
                    templates,
                    routing_rules,
                    workspace=workspace_id,
                    capture_mode=mode,
                )

            if not routing.matched or routing.template_id is None:
                # Sem template: nota geral interpretada pela IA (nunca "sem match").
                note.template_id = None
                note.status = NoteStatus.PROCESSED
                try:
                    general = await generate_general_note(transcription, {"user_id": str(uid)})
                except GeneralNoteError as exc:
                    logger.warning("Nota geral curta p/ nota %s: %s", note.id, exc)
                    general = {
                        "title": transcription.strip()[:80] or "Nota de voz",
                        "body": transcription.strip(),
                        "tasks": [],
                        "due_date": None,
                        "reminder_at": None,
                        "send_to_kanban": False,
                    }
                note.title = str(general.get("title", "") or "Nota de voz")[:300]
                note.structured_data = {
                    "tipo": "nota_geral",
                    "nota": general.get("body", ""),
                    "tarefas": general.get("tasks", []),
                }
                note.due_date = _parse_dt(general.get("due_date"))
                note.reminder_at = _parse_dt(general.get("reminder_at"))
                if note.reminder_at is not None:
                    note.reminder_sent = False
                note.kanban_status = (
                    KanbanColumn.TODO.value if general.get("send_to_kanban") else None
                )
                if _extract_task_items(note.structured_data):
                    await _create_kanban_tasks(note, note.structured_data, session)
                session.add(note)
                await session.commit()
                await _process_document_safely(session, note, uid)
                return {"ok": True, "status": "general", "reason": routing.reason}

            template = await session.get(Template, routing.template_id)
            if template is None:
                # Template removido entre router e extração: cai na nota geral.
                note.template_id = None
                note.status = NoteStatus.PROCESSED
                try:
                    general = await generate_general_note(transcription, {"user_id": str(uid)})
                except GeneralNoteError:
                    general = {
                        "title": transcription.strip()[:80] or "Nota de voz",
                        "body": transcription.strip(),
                        "tasks": [],
                        "due_date": None,
                        "reminder_at": None,
                        "send_to_kanban": False,
                    }
                note.title = str(general.get("title", "") or "Nota de voz")[:300]
                note.structured_data = {
                    "tipo": "nota_geral",
                    "nota": general.get("body", ""),
                    "tarefas": general.get("tasks", []),
                }
                note.due_date = _parse_dt(general.get("due_date"))
                note.reminder_at = _parse_dt(general.get("reminder_at"))
                if note.reminder_at is not None:
                    note.reminder_sent = False
                note.kanban_status = (
                    KanbanColumn.TODO.value if general.get("send_to_kanban") else None
                )
                if _extract_task_items(note.structured_data):
                    await _create_kanban_tasks(note, note.structured_data, session)
                session.add(note)
                await session.commit()
                await _process_document_safely(session, note, uid)
                return {"ok": True, "status": "general", "reason": "template removido"}

            # 3. Extração
            try:
                structured = await extract_structured(template, transcription)
            except ExtractionError as exc:
                note.template_id = template.id
                note.structured_data = None
                note.status = NoteStatus.PENDING_REVIEW
                session.add(note)
                session.add(
                    ExecutionLog(
                        note_id=note.id,
                        action_type="extraction",
                        status=ExecutionStatus.FAILED,
                        payload={"template_id": str(template.id)},
                        error_message=str(exc)[:4000],
                    )
                )
                await session.commit()
                return {"ok": False, "status": "extraction_failed", "error": str(exc)}

            note.template_id = template.id
            note.structured_data = structured
            note.status = NoteStatus.PROCESSED
            if _extract_task_items(structured):
                await _create_kanban_tasks(note, structured, session)
            session.add(note)
            await session.commit()
            await session.refresh(note)

            await _process_document_safely(session, note, uid)

            # 4. Dispatcher (persiste os próprios logs)
            logs = await dispatch_actions(note, template, session)
            ok_actions = sum(1 for lg in logs if lg.status == ExecutionStatus.SUCCESS)
            return {"ok": True, "status": "processed", "actions_ok": ok_actions, "actions_total": len(logs)}
        except Exception as exc:  # noqa: BLE001 — job nunca deve quebrar o worker
            logger.exception("Falha no job da nota %s", note_id)
            try:
                if not (note.raw_transcription or "").strip():
                    # Sem transcrição não há nota: apaga em vez de poluir a lista.
                    await session.delete(note)
                    await session.commit()
                    return {"ok": False, "error": str(exc), "discarded": True}
                note.status = NoteStatus.PENDING_REVIEW
                session.add(note)
                session.add(
                    ExecutionLog(
                        note_id=note.id,
                        action_type="pipeline",
                        status=ExecutionStatus.FAILED,
                        payload={},
                        error_message=str(exc)[:4000],
                    )
                )
                await session.commit()
            except Exception:
                logger.exception("Falha ao persistir erro do job %s", note_id)
            return {"ok": False, "error": str(exc)}
        finally:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


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

    Alvos: `reminder_at <= now`, `status == processed`, `kanban != done`,
    `reminder_sent == False`. Sucesso marca `reminder_sent`; falha mantém
    para nova tentativa no próximo ciclo. Nunca levanta.
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
            if note.kanban_status == KanbanColumn.DONE.value:
                skipped += 1
                continue
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
