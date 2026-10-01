"""Validated provider execution for claimed action-ledger rows.

Task/reminder writes are idempotent in the caller's transaction. Telegram is
an external at-least-once side effect: if the provider accepts a send but the
ledger transaction later rolls back, a retry may send the same message again.
This service never commits or changes the ledger status.
"""

import hashlib
import re
import uuid
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from telegram import Bot

from app.core.config import settings
from app.models.action_execution import ActionExecution
from app.models.action_item import Reminder, Task
from app.models.integration_connection import IntegrationConnection
from app.models.user import User
from app.models.user_secret import UserSecret
from app.models.workflow import Workflow, WorkflowExecution, WorkflowStep
from app.models.workspace import Workspace, WorkspaceMembership
from app.schemas.workflow_action import ActionValidationError, WorkflowActionType
from app.services.action_registry import validate_action
from app.services.dispatcher import build_secret_scope, load_user_secrets
from app.services.nango_client import NangoClient, NangoClientError


class IntegrationExecutionError(ValueError):
    """Raised when the persisted action is not authorized or valid to execute."""


class IntegrationService:
    async def execute_claimed(
        self, session: AsyncSession, execution_id: uuid.UUID,
    ) -> dict[str, Any]:
        """Execute one claimed ledger row without committing or changing its status."""
        execution = await session.get(ActionExecution, execution_id)
        if execution is None:
            raise IntegrationExecutionError("Action execution não encontrada")
        if execution.status != "claimed":
            raise IntegrationExecutionError("Action execution não está claimed")
        if not isinstance(execution.action_input, dict):
            raise IntegrationExecutionError("Action execution sem input canônico")
        if "type" in execution.action_input:
            raise IntegrationExecutionError("action_input não deve repetir action_type")

        membership = (await session.exec(select(WorkspaceMembership.id).where(
            WorkspaceMembership.workspace_id == execution.workspace_id,
            WorkspaceMembership.user_id == execution.user_id,
        ))).first()
        user = await session.get(User, execution.user_id)
        workspace = await session.get(Workspace, execution.workspace_id)
        if membership is None or user is None or workspace is None:
            raise IntegrationExecutionError("Action execution fora do escopo do usuário")

        await self._validate_origin(session, execution)

        self._validate_policy(execution.policy_snapshot)
        try:
            action = validate_action({"type": execution.action_type, **execution.action_input})
        except ActionValidationError as exc:
            raise IntegrationExecutionError("Action input inválido") from exc

        if execution.action_type == WorkflowActionType.CREATE_TASK.value:
            return await self._create_task(session, execution, workspace, action)
        if execution.action_type == WorkflowActionType.CREATE_REMINDER.value:
            return await self._create_reminder(session, execution, workspace, action)
        if execution.action_type == WorkflowActionType.SEND_TELEGRAM.value:
            return await self._send_telegram(session, execution, action)
        if execution.action_type == WorkflowActionType.CREATE_CALENDAR_EVENT.value:
            return await self._create_calendar_event(session, execution, action)
        raise IntegrationExecutionError("Tipo de action sem executor nesta lane")

    @staticmethod
    def _validate_policy(snapshot: object) -> None:
        if not isinstance(snapshot, dict):
            raise IntegrationExecutionError("Policy snapshot não autoriza a execução")
        automatically_authorized = (
            snapshot.get("authorized") is True
            and snapshot.get("require_confirmation") is False
        )
        explicitly_confirmed = snapshot.get("confirmed_by_user") is True
        if snapshot.get("authorized") is not True and not explicitly_confirmed:
            raise IntegrationExecutionError("Policy snapshot não autoriza a execução")
        if not automatically_authorized and not explicitly_confirmed:
            raise IntegrationExecutionError("Action aguarda confirmação do usuário")

    @staticmethod
    async def _validate_origin(session: AsyncSession, execution: ActionExecution) -> None:
        snapshot = execution.policy_snapshot
        if not isinstance(snapshot, dict):
            raise IntegrationExecutionError("Origem da action ausente")

        if snapshot.get("standalone") is True:
            if (
                execution.trigger_type not in {"REMINDER_DUE", "FOLLOW_UP_DUE"}
                or snapshot.get("workflow_id") is not None
                or execution.workflow_execution_id is not None
                or execution.workflow_step_id is not None
            ):
                raise IntegrationExecutionError("Origem standalone não permitida")
            return

        raw_workflow_id = snapshot.get("workflow_id")
        if not isinstance(raw_workflow_id, (str, uuid.UUID)):
            raise IntegrationExecutionError("Workflow provenance ausente")
        try:
            workflow_id = uuid.UUID(str(raw_workflow_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise IntegrationExecutionError("Workflow provenance inválida") from exc
        workflow = await session.get(Workflow, workflow_id)
        if (
            workflow is None
            or workflow.workspace_id != execution.workspace_id
            or workflow.user_id != execution.user_id
            or workflow.enabled is not True
            or execution.workflow_version is None
            or workflow.version != execution.workflow_version
        ):
            raise IntegrationExecutionError("Workflow ausente, desativado ou desatualizado")

        if execution.workflow_execution_id is not None:
            parent = (await session.exec(select(WorkflowExecution).where(
                WorkflowExecution.id == execution.workflow_execution_id,
                WorkflowExecution.workspace_id == execution.workspace_id,
                WorkflowExecution.user_id == execution.user_id,
                WorkflowExecution.workflow_id == workflow_id,
            ))).first()
            if parent is None:
                raise IntegrationExecutionError("Workflow execution fora do escopo")
        if execution.workflow_step_id is not None:
            step = await session.get(WorkflowStep, execution.workflow_step_id)
            if step is None or step.workflow_id != workflow_id:
                raise IntegrationExecutionError("Workflow step fora do escopo")

    @staticmethod
    def _idempotency_key(execution: ActionExecution, kind: str) -> str:
        digest = hashlib.sha256(execution.idempotency_key.encode("utf-8")).hexdigest()
        return f"integration:{kind}:{digest}"

    @staticmethod
    def _to_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise IntegrationExecutionError("Timestamp da action deve ser timezone-aware")
        return value.astimezone(timezone.utc)

    async def _create_task(
        self, session: AsyncSession, execution: ActionExecution, workspace: Workspace, action: Any,
    ) -> dict[str, str]:
        key = self._idempotency_key(execution, "task")
        existing = (await session.exec(select(Task).where(
            Task.workspace_id == execution.workspace_id,
            Task.idempotency_key == key,
        ))).first()
        if existing is not None:
            if existing.user_id != execution.user_id:
                raise IntegrationExecutionError("Task idempotente pertence a outro usuário")
            return {"entity": "task", "id": str(existing.id), "status": existing.status}

        task = Task(
            workspace_id=execution.workspace_id,
            user_id=execution.user_id,
            idempotency_key=key,
            title=action.title,
            summary=action.summary,
            confidence=action.confidence,
            status="open",
            due_at=self._to_utc(action.due_at),
            timezone=workspace.timezone,
        )
        try:
            async with session.begin_nested():
                session.add(task)
                await session.flush()
        except IntegrityError:
            existing = (await session.exec(select(Task).where(
                Task.workspace_id == execution.workspace_id,
                Task.idempotency_key == key,
            ))).first()
            if existing is None or existing.user_id != execution.user_id:
                raise
            task = existing
        return {"entity": "task", "id": str(task.id), "status": task.status}

    async def _create_reminder(
        self, session: AsyncSession, execution: ActionExecution, workspace: Workspace, action: Any,
    ) -> dict[str, str]:
        key = self._idempotency_key(execution, "reminder")
        existing = (await session.exec(select(Reminder).where(
            Reminder.workspace_id == execution.workspace_id,
            Reminder.idempotency_key == key,
        ))).first()
        if existing is not None:
            if existing.user_id != execution.user_id:
                raise IntegrationExecutionError("Reminder idempotent pertence a outro usuário")
            return {"entity": "reminder", "id": str(existing.id), "status": existing.status}

        remind_at = self._to_utc(action.remind_at)
        if remind_at is None:
            raise IntegrationExecutionError("Reminder exige remind_at")
        reminder = Reminder(
            workspace_id=execution.workspace_id,
            user_id=execution.user_id,
            idempotency_key=key,
            title=action.title,
            summary="",
            confidence=action.confidence,
            status="scheduled",
            remind_at=remind_at,
            timezone=workspace.timezone,
        )
        try:
            async with session.begin_nested():
                session.add(reminder)
                await session.flush()
        except IntegrityError:
            existing = (await session.exec(select(Reminder).where(
                Reminder.workspace_id == execution.workspace_id,
                Reminder.idempotency_key == key,
            ))).first()
            if existing is None or existing.user_id != execution.user_id:
                raise
            reminder = existing
        return {"entity": "reminder", "id": str(reminder.id), "status": reminder.status}

    async def _send_telegram(
        self, session: AsyncSession, execution: ActionExecution, action: Any,
    ) -> dict[str, int]:
        user_secrets = await load_user_secrets(session, execution.user_id)
        secret_scope = build_secret_scope(user_secrets)
        # The platform token is the only permitted bot credential. Chat routing
        # must be a secret owned by this user, never an action-input destination.
        bot_token = settings.TELEGRAM_BOT_TOKEN
        chat_id = user_secrets.get("TELEGRAM_CHAT_ID")
        if not bot_token or not chat_id or not secret_scope.get("TELEGRAM_CHAT_ID"):
            raise IntegrationExecutionError("Telegram não configurado para o usuário")

        bot = Bot(token=bot_token)
        try:
            message = await bot.send_message(chat_id=chat_id, text=action.text[:2000])
        except Exception:
            # Provider exceptions may contain request URLs or other credentials.
            raise IntegrationExecutionError("Falha ao enviar mensagem Telegram") from None
        return {"message_id": int(message.message_id)}

    async def _create_calendar_event(
        self, session: AsyncSession, execution: ActionExecution, action: Any,
    ) -> dict[str, str]:
        provider_key = settings.NANGO_GOOGLE_CALENDAR_PROVIDER_CONFIG_KEY
        if not settings.NANGO_ENV_KEY or not provider_key:
            raise IntegrationExecutionError("Google Calendar não está configurado")
        connections = list((await session.exec(select(IntegrationConnection).where(
            IntegrationConnection.workspace_id == execution.workspace_id,
            IntegrationConnection.user_id == execution.user_id,
            IntegrationConnection.provider == "google_calendar",
            IntegrationConnection.provider_config_key == provider_key,
            IntegrationConnection.status == "active",
        ))).all())
        if len(connections) != 1:
            raise IntegrationExecutionError("Conexão Google Calendar ativa não encontrada")
        connection = connections[0]
        timezone_name = action.timezone or "UTC"
        try:
            zone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise IntegrationExecutionError("Timezone Google Calendar inválido") from exc
        start_at = action.start_at.astimezone(zone)
        end_at = action.end_at.astimezone(zone)
        payload = {
            "summary": action.summary,
            "start": {"dateTime": start_at.isoformat(), "timeZone": zone.key},
            "end": {"dateTime": end_at.isoformat(), "timeZone": zone.key},
        }
        try:
            nango = NangoClient(
                api_key=settings.NANGO_ENV_KEY,
                base_url=settings.NANGO_BASE_URL or "https://api.nango.dev",
            )
            response = await nango.proxy_request(
                "POST",
                "calendar/v3/calendars/primary/events",
                connection_id=connection.nango_connection_id,
                provider_config_key=connection.provider_config_key,
                json_body=payload,
            )
        except (NangoClientError, ValueError):
            raise IntegrationExecutionError("Google Calendar request falhou") from None
        body = response.body
        event_id = body.get("id") if isinstance(body, dict) else None
        if not isinstance(event_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", event_id):
            raise IntegrationExecutionError("Google Calendar retornou evento inválido")
        return {"entity": "calendar_event", "id": event_id}
