"""Prepare durable workflow and action execution ledgers without dispatching."""

import re
import uuid
import logging
from dataclasses import dataclass
from hashlib import sha256
import json
from math import isfinite
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.action_execution import ActionExecution, ActionExecutionStatus
from app.models.automation_settings import AutomationSettings
from app.models.workflow import Workflow, WorkflowExecution, WorkflowStep
from app.models.workspace import WorkspaceMembership
from app.schemas.workflow_action import ActionValidationError
from app.services.action_registry import validate_action
from app.schemas.workflow_action import StructuredAction

SUPPORTED_TRIGGERS = frozenset({
    "MANUAL",
    "DOCUMENT_GENERATED",
    "REMINDER_DUE",
    "TASK_CREATED",
    "FOLLOW_UP_DUE",
})
ALLOWED_CONDITION_KEYS = frozenset({"source", "event_type", "minimum_confidence"})
ALLOWED_CONTEXT_KEYS = frozenset({"source", "event_type", "confidence"})
_SAFE_SOURCE_VALUE = re.compile(r"^[A-Za-z0-9_.:-]{1,120}$")
_SAFE_SOURCE_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,320}$")
logger = logging.getLogger("anotai.workflow_engine")


class WorkflowPreparationError(ValueError):
    """Raised when an enabled workflow cannot safely be prepared."""


@dataclass(frozen=True, slots=True)
class _PreparedWorkflow:
    workflow: Workflow
    steps: tuple[tuple[WorkflowStep, Any, ActionExecutionStatus | None, bool], ...]


def _validate_trigger_context(context: dict[str, Any] | None) -> dict[str, Any]:
    if context is None:
        return {}
    if not isinstance(context, dict) or set(context) - ALLOWED_CONTEXT_KEYS:
        raise WorkflowPreparationError("Contexto de trigger inválido")
    normalized = dict(context)
    for key in ("source", "event_type"):
        value = normalized.get(key)
        if value is not None and (
            not isinstance(value, str) or not _SAFE_SOURCE_VALUE.fullmatch(value)
        ):
            raise WorkflowPreparationError("Contexto de trigger inválido")
    confidence = normalized.get("confidence")
    if confidence is not None and (
        isinstance(confidence, bool)
        or not isinstance(confidence, (float, int))
        or not isfinite(confidence)
        or not 0 <= confidence <= 1
    ):
        raise WorkflowPreparationError("Contexto de trigger inválido")
    return normalized


def _condition_matches(conditions: Any, context: dict[str, Any]) -> bool:
    if not isinstance(conditions, dict) or set(conditions) - ALLOWED_CONDITION_KEYS:
        raise WorkflowPreparationError("Condições de workflow inválidas")
    for key in ("source", "event_type"):
        expected = conditions.get(key)
        if expected is not None:
            if not isinstance(expected, str) or not _SAFE_SOURCE_VALUE.fullmatch(expected):
                raise WorkflowPreparationError("Condições de workflow inválidas")
            if context.get(key) != expected:
                return False
    minimum = conditions.get("minimum_confidence")
    if minimum is not None:
        if (
            isinstance(minimum, bool)
            or not isinstance(minimum, (int, float))
            or not isfinite(minimum)
            or not 0 <= minimum <= 1
        ):
            raise WorkflowPreparationError("Condições de workflow inválidas")
        confidence = context.get("confidence")
        if confidence is None or confidence < minimum:
            return False
    return True


def _action_authorized(action_config: Any, action_type: str) -> bool:
    if not isinstance(action_config, dict):
        return False
    # AutomationSettings API contract: dispositions are automatic,
    # require_confirmation, or disabled. Only explicit automatic authorizes.
    return action_config.get(action_type) == "automatic"


def _action_status(
    action_type: str,
    mode: str,
    authorized: bool,
    require_confirmation: bool,
) -> ActionExecutionStatus:
    if require_confirmation:
        return "awaiting_confirmation"
    if not authorized:
        return "awaiting_confirmation"
    if action_type in {"CREATE_TASK", "CREATE_REMINDER"} and mode in {"semi_automatic", "automatic"}:
        return "pending"
    if action_type == "SEND_TELEGRAM" and mode == "automatic":
        return "pending"
    if action_type == "CREATE_CALENDAR_EVENT" and mode == "automatic":
        return "pending"
    return "awaiting_confirmation"


def _idempotency_digest(namespace: str, **components: str) -> str:
    canonical = json.dumps(
        {"namespace": namespace, **components},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _workflow_idempotency_key(
    workspace_id: uuid.UUID, trigger_type: str, source_id: str, workflow_id: uuid.UUID
) -> str:
    digest = _idempotency_digest(
        "workflow-v1",
        workspace_id=str(workspace_id),
        workflow_id=str(workflow_id),
        trigger_type=trigger_type,
        source_id=source_id,
    )
    return f"workflow-v1:{digest}"


def _action_idempotency_key(
    workspace_id: uuid.UUID, workflow: Workflow, source_id: str, step: WorkflowStep
) -> str:
    digest = _idempotency_digest(
        "action-v1",
        workspace_id=str(workspace_id),
        workflow_id=str(workflow.id),
        trigger_type=workflow.trigger_type,
        source_id=source_id,
        step_id=str(step.id),
    )
    return f"action-v1:{digest}"


async def _get_or_create_workflow_execution(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    workflow: Workflow,
    trigger_type: str,
    source_id: str,
    key: str,
    context: dict[str, Any],
) -> WorkflowExecution:
    existing = (await session.exec(
        select(WorkflowExecution).where(
            WorkflowExecution.workspace_id == workspace_id,
            WorkflowExecution.workflow_id == workflow.id,
            WorkflowExecution.idempotency_key == key,
        )
    )).first()
    if existing is not None:
        return existing
    # Only finite, non-content trigger metadata is retained.
    metadata = {key: context[key] for key in ("source", "event_type") if key in context}
    execution = WorkflowExecution(
        workspace_id=workspace_id,
        user_id=user_id,
        workflow_id=workflow.id,
        trigger_type=trigger_type,
        source_id=source_id,
        idempotency_key=key,
        status="pending",
        metadata_json=metadata,
    )
    try:
        async with session.begin_nested():
            session.add(execution)
            await session.flush()
    except IntegrityError:
        existing = (await session.exec(
            select(WorkflowExecution).where(
                WorkflowExecution.workspace_id == workspace_id,
                WorkflowExecution.workflow_id == workflow.id,
                WorkflowExecution.idempotency_key == key,
            )
        )).first()
        if existing is None:
            raise
        return existing
    return execution


async def _get_or_create_action_execution(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    workflow_execution: WorkflowExecution,
    workflow: Workflow,
    step: WorkflowStep,
    action: StructuredAction,
    source_id: str,
    status: ActionExecutionStatus,
    authorized: bool,
    mode: str,
    key: str,
) -> ActionExecution:
    existing = (await session.exec(
        select(ActionExecution).where(
            ActionExecution.workspace_id == workspace_id,
            ActionExecution.idempotency_key == key,
        )
    )).first()
    if existing is not None:
        return existing
    ledger = ActionExecution(
        workspace_id=workspace_id,
        user_id=user_id,
        workflow_execution_id=workflow_execution.id,
        workflow_step_id=step.id,
        trigger_type=workflow.trigger_type,
        source_id=source_id,
        action_type=step.action_type,
        idempotency_key=key,
        action_input=action.model_dump(mode="json", exclude={"type"}),
        workflow_version=workflow.version,
        status=status,
        policy_snapshot={
            "automation_mode": mode,
            "action_type": step.action_type,
            "authorized": authorized,
            "require_confirmation": step.require_confirmation,
            "workflow_id": str(workflow.id),
            "workflow_version": workflow.version,
        },
    )
    try:
        async with session.begin_nested():
            session.add(ledger)
            await session.flush()
    except IntegrityError:
        existing = (await session.exec(
            select(ActionExecution).where(
                ActionExecution.workspace_id == workspace_id,
                ActionExecution.idempotency_key == key,
            )
        )).first()
        if existing is None:
            raise
        return existing
    return ledger


async def prepare_workflow_executions(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    trigger_type: str,
    *,
    source_id: str | uuid.UUID | None = None,
    context: dict[str, Any] | None = None,
) -> list[WorkflowExecution]:
    """Validate and persist execution ledgers only; never dispatch providers."""
    if not isinstance(workspace_id, uuid.UUID) or not isinstance(user_id, uuid.UUID):
        raise PermissionError("Escopo workspace/user inválido")
    if trigger_type not in SUPPORTED_TRIGGERS:
        raise WorkflowPreparationError("Tipo de trigger não suportado")
    membership_id = (await session.exec(
        select(WorkspaceMembership.id).where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == user_id,
        )
    )).first()
    if membership_id is None:
        raise PermissionError("Usuário não pertence ao workspace")
    trigger_context = _validate_trigger_context(context)
    if source_id is None:
        raise WorkflowPreparationError("source_id explícito é obrigatório")
    if not isinstance(source_id, (str, uuid.UUID)):
        raise WorkflowPreparationError("source_id inválido")
    normalized_source_id = str(source_id)
    if not _SAFE_SOURCE_ID.fullmatch(normalized_source_id):
        raise WorkflowPreparationError("source_id inválido")

    settings_row = (await session.exec(
        select(AutomationSettings).where(
            AutomationSettings.workspace_id == workspace_id,
            AutomationSettings.user_id == user_id,
        )
    )).first()
    mode = str(settings_row.automation_mode).lower() if settings_row is not None else "suggestive"
    action_config = settings_row.action_config if settings_row is not None else {}
    if mode == "disabled":
        return []
    if mode not in {"suggestive", "semi_automatic", "automatic"}:
        mode = "suggestive"

    workflows = list((await session.exec(
        select(Workflow).where(
            Workflow.workspace_id == workspace_id,
            Workflow.user_id == user_id,
            Workflow.enabled == True,  # noqa: E712
            Workflow.trigger_type == trigger_type,
        ).order_by(Workflow.created_at.asc(), Workflow.id.asc())  # type: ignore[attr-defined]
    )).all())

    # Build and validate every selected workflow in memory before creating any ledger.
    plans: list[_PreparedWorkflow] = []
    for workflow in workflows:
        try:
            if not _condition_matches(workflow.conditions, trigger_context):
                continue
            steps = list((await session.exec(
                select(WorkflowStep).where(
                    WorkflowStep.workflow_id == workflow.id,
                    WorkflowStep.enabled == True,  # noqa: E712
                ).order_by(WorkflowStep.position.asc(), WorkflowStep.id.asc())  # type: ignore[attr-defined]
            )).all())
            if not steps:
                continue
            prepared_steps: list[tuple[WorkflowStep, Any, ActionExecutionStatus | None, bool]] = []
            for step in steps:
                if not isinstance(step.input_template, dict) or "type" in step.input_template:
                    raise WorkflowPreparationError("input_template não pode conter type")
                action_payload = {"type": step.action_type, **step.input_template}
                try:
                    action = validate_action(action_payload)
                except ActionValidationError as exc:
                    raise WorkflowPreparationError("Workflow contém ação inválida") from exc
                raw_action_type = getattr(action, "type", None)
                if raw_action_type is None:
                    raise WorkflowPreparationError("action_type não corresponde ao input_template")
                action_type = raw_action_type.value if hasattr(raw_action_type, "value") else str(raw_action_type)
                if action_type != step.action_type:
                    raise WorkflowPreparationError("action_type não corresponde ao input_template")
                authorized = _action_authorized(action_config, action_type)
                disposition = action_config.get(action_type) if isinstance(action_config, dict) else None
                status = None if disposition == "disabled" else _action_status(
                    action_type, mode, authorized, step.require_confirmation
                )
                prepared_steps.append((step, action, status, authorized))
            plans.append(_PreparedWorkflow(workflow=workflow, steps=tuple(prepared_steps)))
        except WorkflowPreparationError:
            # Invalid definitions poison only their own workflow, not the other
            # enabled workflows sharing this trigger. Do not expose payloads.
            logger.warning("Skipping invalid workflow definition workflow_id=%s category=definition", workflow.id)
            continue

    executions: list[WorkflowExecution] = []
    for plan in plans:
        workflow_key = _workflow_idempotency_key(
            workspace_id, trigger_type, normalized_source_id, plan.workflow.id
        )
        execution = await _get_or_create_workflow_execution(
            session,
            workspace_id=workspace_id,
            user_id=user_id,
            workflow=plan.workflow,
            trigger_type=trigger_type,
            source_id=normalized_source_id,
            key=workflow_key,
            context=trigger_context,
        )
        executions.append(execution)
        for step, action, status, authorized in plan.steps:
            if status is None:
                continue
            action_key = _action_idempotency_key(workspace_id, plan.workflow, normalized_source_id, step)
            await _get_or_create_action_execution(
                session,
                workspace_id=workspace_id,
                user_id=user_id,
                workflow_execution=execution,
                workflow=plan.workflow,
                step=step,
                action=action,
                source_id=normalized_source_id,
                status=status,
                authorized=authorized,
                mode=mode,
                key=action_key,
            )
    return executions


async def emit_trigger(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    trigger_type: str,
    *,
    source_id: str | uuid.UUID | None = None,
    context: dict[str, Any] | None = None,
) -> list[WorkflowExecution]:
    """Server-side trigger entry point; this remains preparation-only."""
    return await prepare_workflow_executions(
        session,
        workspace_id,
        user_id,
        trigger_type,
        source_id=source_id,
        context=context,
    )
