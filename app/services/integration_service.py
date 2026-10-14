"""Validated provider execution for claimed action-ledger rows.

Task/reminder writes are idempotent in the caller's transaction. Telegram is
an external at-least-once side effect: if the provider accepts a send but the
ledger transaction later rolls back, a retry may send the same message again.
This service never commits or changes the ledger status.
"""

import base64
import hashlib
import re
import uuid
from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import formatdate
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession
from telegram import Bot

from app.core.config import settings
from app.models.action_execution import ActionExecution
from app.models.action_item import Reminder, Task
from app.models.integration_connection import IntegrationConnection
from app.models.user import User
from app.models.workflow import Workflow, WorkflowExecution, WorkflowStep
from app.models.workspace import Workspace, WorkspaceMembership
from app.schemas.template import ActionConfig, NotionConfig, SlackConfig
from app.schemas.workflow_action import (
    ActionValidationError,
    SendEmailAction,
    WorkflowActionType,
)
from app.services.action_registry import validate_action
from app.services.dispatcher import (
    _call_mcp_tool_server,
    build_secret_scope,
    load_user_secrets,
    resolve_secrets_in_config,
)
from app.services.nango_client import NangoClient, NangoClientError
from app.services.template_actions import TemplateActionError, validate_template_origin


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

        template_origin = await self._validate_origin(session, execution)

        self._validate_policy(execution.policy_snapshot)
        try:
            action = validate_action(
                {"type": execution.action_type, **execution.action_input},
                template_authorized=template_origin is not None,
            )
        except ActionValidationError as exc:
            raise IntegrationExecutionError("Action input inválido") from exc

        if execution.action_type == WorkflowActionType.CREATE_TASK.value:
            return await self._create_task(session, execution, workspace, action)
        if execution.action_type == WorkflowActionType.CREATE_REMINDER.value:
            return await self._create_reminder(session, execution, workspace, action)
        if execution.action_type == WorkflowActionType.SEND_TELEGRAM.value:
            if template_origin is not None:
                return await self._send_template_telegram(session, execution, action, template_origin[0])
            return await self._send_telegram(session, execution, action)
        if execution.action_type == WorkflowActionType.CREATE_CALENDAR_EVENT.value:
            return await self._create_calendar_event(session, execution, action)
        if execution.action_type == WorkflowActionType.SEND_EMAIL.value:
            return await self._send_email(session, execution, user, action)
        if execution.action_type == WorkflowActionType.SEND_SLACK_MESSAGE.value:
            if template_origin is None:
                raise IntegrationExecutionError("Slack exige um template configurado")
            return await self._send_slack_message(session, execution, action, template_origin[0])
        if execution.action_type == WorkflowActionType.CREATE_NOTION_PAGE.value:
            if template_origin is None:
                raise IntegrationExecutionError("Notion exige um template configurado")
            return await self._create_notion_page(session, execution, action, template_origin[0])
        if template_origin is not None:
            configured, note, document = template_origin
            if execution.action_type == WorkflowActionType.SEND_WEBHOOK.value:
                return await self._send_webhook(session, execution, action, configured)
            if execution.action_type == WorkflowActionType.CALL_MCP_TOOL.value:
                config = {**configured.config, "arguments_mapping": action.arguments}
                # Provider auth is resolved against the owned server at send time.
                result = await _call_mcp_tool_server(
                    config, {
                        **document.structured_data, "title": note.title,
                        "body": document.rendered_content, "summary": note.summary,
                    }, note, session, rendered_arguments=True,
                )
                return {"tool": result["tool"]}
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
    async def _validate_origin(session: AsyncSession, execution: ActionExecution):
        snapshot = execution.policy_snapshot
        if not isinstance(snapshot, dict):
            raise IntegrationExecutionError("Origem da action ausente")

        if snapshot.get("origin") == "template":
            try:
                return await validate_template_origin(session, execution)
            except TemplateActionError as exc:
                raise IntegrationExecutionError("Template provenance inválida") from exc

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
            source_note_id=(
                uuid.UUID(execution.source_id)
                if execution.policy_snapshot.get("origin") == "template" else None
            ),
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

    async def _send_template_telegram(
        self, session: AsyncSession, execution: ActionExecution, action: Any, configured: ActionConfig,
    ) -> dict[str, int]:
        secrets = await load_user_secrets(session, execution.user_id)
        scope = {**secrets, "TELEGRAM_BOT_TOKEN": settings.TELEGRAM_BOT_TOKEN}
        config, missing = resolve_secrets_in_config(configured.config, scope)
        if missing:
            raise IntegrationExecutionError("Telegram secret não configurado")
        token = config.get("bot_token") or settings.TELEGRAM_BOT_TOKEN
        chat_id = config.get("chat_id") or secrets.get("TELEGRAM_CHAT_ID")
        if not token or not chat_id:
            raise IntegrationExecutionError("Telegram não configurado para o usuário")
        message = await Bot(token=token).send_message(chat_id=chat_id, text=action.text)
        return {"message_id": int(message.message_id)}

    async def _send_webhook(
        self, session: AsyncSession, execution: ActionExecution, action: Any, configured: ActionConfig,
    ) -> dict[str, int]:
        secrets = await load_user_secrets(session, execution.user_id)
        config, missing = resolve_secrets_in_config(configured.config, secrets)
        if missing:
            raise IntegrationExecutionError("Webhook secret não configurado")
        headers = {**config.get("headers", {}), "Idempotency-Key": execution.idempotency_key}
        async with httpx.AsyncClient(timeout=settings.WEBHOOK_TIMEOUT_SECONDS) as client:
            response = await client.request(
                config.get("method", "POST"), config["url"], headers=headers, json=action.body,
            )
            response.raise_for_status()
        return {"status_code": response.status_code}

    async def _send_email(
        self, session: AsyncSession, execution: ActionExecution, user: User, action: SendEmailAction,
    ) -> dict[str, str | None]:
        recipient = action.to or user.email
        SendEmailAction._single_mailbox(recipient)
        provider_key = settings.NANGO_GOOGLE_GMAIL_PROVIDER_CONFIG_KEY
        if not settings.NANGO_ENV_KEY or not provider_key:
            raise IntegrationExecutionError("Gmail não está configurado")
        connections = list((await session.exec(select(IntegrationConnection).where(
            IntegrationConnection.workspace_id == execution.workspace_id,
            IntegrationConnection.user_id == execution.user_id,
            IntegrationConnection.provider == "google_gmail",
            IntegrationConnection.provider_config_key == provider_key,
            IntegrationConnection.status == "active",
        ))).all())
        if len(connections) != 1:
            raise IntegrationExecutionError("Conecte seu Gmail para enviar email")
        connection = connections[0]
        # The sender is the connected Gmail account: Google derives the From
        # header from the OAuth identity, so none is forged here.
        message_id = f"<{self._idempotency_key(execution, 'email').rsplit(':', 1)[-1]}@anotai>"
        message = EmailMessage(policy=SMTP)
        message["To"] = recipient
        message["Subject"] = action.subject
        message["Date"] = formatdate(usegmt=True)
        message["Message-ID"] = message_id
        message.set_content(action.body)
        # Gmail's messages.send expects the RFC 2822 bytes base64url-encoded
        # without padding.
        raw = base64.urlsafe_b64encode(message.as_bytes()).rstrip(b"=").decode("ascii")
        try:
            nango = NangoClient(
                api_key=settings.NANGO_ENV_KEY,
                base_url=settings.NANGO_BASE_URL or "https://api.nango.dev",
            )
            response = await nango.proxy_request(
                "POST",
                "gmail/v1/users/me/messages/send",
                connection_id=connection.nango_connection_id,
                provider_config_key=connection.provider_config_key,
                json_body={"raw": raw},
            )
        except (NangoClientError, ValueError):
            raise IntegrationExecutionError("Gmail request falhou") from None
        body = response.body
        sent_id = body.get("id") if isinstance(body, dict) else None
        thread_id = body.get("threadId") if isinstance(body, dict) else None
        if not isinstance(sent_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", sent_id):
            raise IntegrationExecutionError("Gmail retornou mensagem inválida")
        if thread_id is not None and (
            not isinstance(thread_id, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", thread_id)
        ):
            raise IntegrationExecutionError("Gmail retornou mensagem inválida")
        return {"entity": "email", "id": sent_id, "thread_id": thread_id}

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

    async def _send_slack_message(
        self, session: AsyncSession, execution: ActionExecution, action: Any, configured: ActionConfig,
    ) -> dict[str, str]:
        provider_key = settings.NANGO_SLACK_PROVIDER_CONFIG_KEY
        if not settings.NANGO_ENV_KEY or not provider_key:
            raise IntegrationExecutionError("Slack não está configurado")
        connections = list((await session.exec(select(IntegrationConnection).where(
            IntegrationConnection.workspace_id == execution.workspace_id,
            IntegrationConnection.user_id == execution.user_id,
            IntegrationConnection.provider == "slack",
            IntegrationConnection.provider_config_key == provider_key,
            IntegrationConnection.status == "active",
        ))).all())
        if len(connections) != 1:
            raise IntegrationExecutionError("Conecte seu Slack para enviar mensagens")
        connection = connections[0]
        # The destination channel is the template owner's configuration, never
        # note/LLM content: a generated note cannot pick an arbitrary channel.
        try:
            channel = SlackConfig.model_validate(configured.config).channel
        except ValidationError:
            raise IntegrationExecutionError("Slack sem canal configurado") from None
        try:
            nango = NangoClient(
                api_key=settings.NANGO_ENV_KEY,
                base_url=settings.NANGO_BASE_URL or "https://api.nango.dev",
            )
            response = await nango.proxy_request(
                "POST",
                "chat.postMessage",
                connection_id=connection.nango_connection_id,
                provider_config_key=connection.provider_config_key,
                json_body={"channel": channel, "text": action.text},
            )
        except (NangoClientError, ValueError):
            raise IntegrationExecutionError("Slack request falhou") from None
        body = response.body
        ok = body.get("ok") if isinstance(body, dict) else None
        if ok is not True:
            raise IntegrationExecutionError("Slack recusou a mensagem")
        ts = body.get("ts") if isinstance(body, dict) else None
        channel_id = body.get("channel") if isinstance(body, dict) else None
        if not isinstance(ts, str) or not re.fullmatch(r"[0-9]{10}(\.[0-9]{1,6})?", ts):
            raise IntegrationExecutionError("Slack retornou mensagem inválida")
        if not isinstance(channel_id, str) or not re.fullmatch(r"[A-Za-z0-9]{1,64}", channel_id):
            raise IntegrationExecutionError("Slack retornou mensagem inválida")
        return {"entity": "slack_message", "ts": ts, "channel": channel_id}

    async def _create_notion_page(
        self, session: AsyncSession, execution: ActionExecution, action: Any, configured: ActionConfig,
    ) -> dict[str, str]:
        provider_key = settings.NANGO_NOTION_PROVIDER_CONFIG_KEY
        if not settings.NANGO_ENV_KEY or not provider_key:
            raise IntegrationExecutionError("Notion não está configurado")
        connections = list((await session.exec(select(IntegrationConnection).where(
            IntegrationConnection.workspace_id == execution.workspace_id,
            IntegrationConnection.user_id == execution.user_id,
            IntegrationConnection.provider == "notion",
            IntegrationConnection.provider_config_key == provider_key,
            IntegrationConnection.status == "active",
        ))).all())
        if len(connections) != 1:
            raise IntegrationExecutionError("Conecte seu Notion para criar páginas")
        connection = connections[0]
        # The destination database and the property-name mapping are the template
        # owner's configuration, never note/LLM content: a generated note cannot
        # choose which Notion database receives the page. The property VALUES are
        # already rendered by `_canonical_action`, like every other action here.
        try:
            notion_config = NotionConfig.model_validate(configured.config)
        except ValidationError:
            raise IntegrationExecutionError("Notion sem database configurado") from None
        payload = {
            "parent": {"database_id": notion_config.database_id},
            "properties": {
                notion_config.title_property: {
                    "title": [{"text": {"content": action.title}}],
                },
                **action.properties,
            },
        }
        try:
            nango = NangoClient(
                api_key=settings.NANGO_ENV_KEY,
                base_url=settings.NANGO_BASE_URL or "https://api.nango.dev",
            )
            response = await nango.proxy_request(
                "POST",
                "v1/pages",
                connection_id=connection.nango_connection_id,
                provider_config_key=connection.provider_config_key,
                json_body=payload,
            )
        except (NangoClientError, ValueError):
            raise IntegrationExecutionError("Notion request falhou") from None
        body = response.body
        page_id = body.get("id") if isinstance(body, dict) else None
        if not isinstance(page_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", page_id):
            raise IntegrationExecutionError("Notion retornou página inválida")
        return {"entity": "notion_page", "id": page_id}
