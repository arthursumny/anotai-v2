"""Authenticated CRUD for workspace-owned workflow definitions and settings."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import text, update
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.core.database import get_session
from app.core.deps import get_current_user
from app.models.action_execution import ActionExecution
from app.models.automation_settings import AutomationSettings
from app.models.user import User
from app.models.workflow import Workflow, WorkflowExecution, WorkflowStep
from app.models.workspace import WorkspaceMembership
from app.services.action_registry import validate_action
from app.schemas.workflow import (
    ActionExecutionDetailRead,
    AutomationSettingsRead,
    AutomationSettingsReplace,
    ActionExecutionRead,
    WorkflowCreate,
    WorkflowRead,
    WorkflowStepRead,
    WorkflowUpdate,
)

router = APIRouter()


async def _workspace_for_user(session: AsyncSession, current: User) -> uuid.UUID:
    workspace_id = current.workspace_id
    if workspace_id is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Workspace não disponível")
    membership = await session.exec(select(WorkspaceMembership.id).where(
        WorkspaceMembership.workspace_id == workspace_id,
        WorkspaceMembership.user_id == current.id,
    ))
    if membership.first() is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Membership do workspace necessária")
    return workspace_id


async def _owned_workflow(
    session: AsyncSession, workflow_id: uuid.UUID, current: User, workspace_id: uuid.UUID,
) -> Workflow:
    workflow = (await session.exec(select(Workflow).where(
        Workflow.id == workflow_id,
        Workflow.workspace_id == workspace_id,
        Workflow.user_id == current.id,
    ))).first()
    if workflow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Workflow não encontrado")
    return workflow


async def _owned_execution(
    session: AsyncSession, execution_id: uuid.UUID, current: User, workspace_id: uuid.UUID,
) -> ActionExecution:
    execution = (await session.exec(select(ActionExecution).where(
        ActionExecution.id == execution_id,
        ActionExecution.workspace_id == workspace_id,
        ActionExecution.user_id == current.id,
    ))).first()
    if execution is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Execução não encontrada")
    return execution


def _action_execution_read(execution: ActionExecution) -> ActionExecutionRead:
    if execution.last_error is None:
        safe_error = None
    elif execution.last_error == "user_dismissed":
        safe_error = "Execução dispensada pelo usuário"
    else:
        safe_error = "A execução falhou"
    return ActionExecutionRead(
        id=execution.id,
        trigger_type=execution.trigger_type,
        action_type=execution.action_type,
        status=execution.status,
        attempts=execution.attempts,
        created_at=execution.created_at,
        last_error=safe_error,
        source_id=execution.source_id,
    )


def _action_preview(execution: ActionExecution) -> dict | None:
    action_input = execution.action_input
    if not isinstance(action_input, dict) or "type" in action_input:
        return None
    try:
        action = validate_action({"type": execution.action_type, **action_input})
    except Exception:
        return None
    action_type = getattr(action.type, "value", str(action.type))
    if action_type != execution.action_type:
        return None
    if action_type == "CREATE_TASK":
        task_due_at = getattr(action, "due_at", None)
        due_at = task_due_at.isoformat() if task_due_at is not None else None
        return {
            "action_type": action_type,
            "title": getattr(action, "title"),
            "summary": getattr(action, "summary"),
            "due_at": due_at,
        }
    if action_type == "CREATE_REMINDER":
        return {
            "action_type": action_type,
            "title": getattr(action, "title"),
            "remind_at": getattr(action, "remind_at").isoformat(),
        }
    if action_type == "SEND_TELEGRAM":
        text_preview = getattr(action, "text")[:2000]
        if len(text_preview) > 2000:
            return None
        return {"action_type": action_type, "text": text_preview}
    if action_type == "CREATE_CALENDAR_EVENT":
        allowed_fields = {"summary", "start_at", "end_at", "timezone"}
        if not {"summary", "start_at", "end_at"}.issubset(action_input):
            return None
        if set(action_input) - allowed_fields:
            return None
        preview = {
            "action_type": action_type,
            "summary": getattr(action, "summary"),
            "start_at": getattr(action, "start_at").isoformat(),
            "end_at": getattr(action, "end_at").isoformat(),
        }
        action_timezone = getattr(action, "timezone", None)
        if action_timezone is not None:
            preview["timezone"] = action_timezone
        return preview
    return None


def _action_execution_detail(execution: ActionExecution) -> ActionExecutionDetailRead:
    preview = _action_preview(execution)
    return ActionExecutionDetailRead(
        **_action_execution_read(execution).model_dump(),
        preview_available=preview is not None,
        preview=preview,
    )


async def _has_valid_confirmation_provenance(
    session: AsyncSession, execution: ActionExecution, workspace_id: uuid.UUID, user_id: uuid.UUID,
) -> bool:
    snapshot = execution.policy_snapshot
    if not isinstance(snapshot, dict):
        return False
    if snapshot.get("standalone") is True:
        return (
            execution.trigger_type in {"REMINDER_DUE", "FOLLOW_UP_DUE"}
            and snapshot.get("workflow_id") is None
            and execution.workflow_execution_id is None
            and execution.workflow_step_id is None
        )
    raw_workflow_id = snapshot.get("workflow_id")
    if not isinstance(raw_workflow_id, (str, uuid.UUID)):
        return False
    try:
        workflow_id = uuid.UUID(str(raw_workflow_id))
    except (TypeError, ValueError, AttributeError):
        return False
    workflow = (await session.exec(select(Workflow).where(
        Workflow.id == workflow_id,
        Workflow.workspace_id == workspace_id,
        Workflow.user_id == user_id,
        Workflow.enabled == True,  # noqa: E712
    ).with_for_update())).first()
    if workflow is None or execution.workflow_version is None or workflow.version != execution.workflow_version:
        return False
    if execution.workflow_execution_id is not None:
        parent = (await session.exec(select(WorkflowExecution.id).where(
            WorkflowExecution.id == execution.workflow_execution_id,
            WorkflowExecution.workspace_id == workspace_id,
            WorkflowExecution.user_id == user_id,
            WorkflowExecution.workflow_id == workflow_id,
        ))).first()
        if parent is None:
            return False
    if execution.workflow_step_id is not None:
        step = await session.get(WorkflowStep, execution.workflow_step_id)
        if step is None or step.workflow_id != workflow_id:
            return False
    return True


async def _reject_client_payload(request: Request) -> None:
    if await request.body():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Este endpoint não aceita payload de action",
        )


async def _workflow_read(session: AsyncSession, workflow: Workflow) -> WorkflowRead:
    steps = await session.exec(select(WorkflowStep).where(
        WorkflowStep.workflow_id == workflow.id,
    ).order_by(text("position")))
    return WorkflowRead(
        id=workflow.id,
        title=workflow.title,
        description=workflow.description,
        trigger_type=workflow.trigger_type,
        conditions=workflow.conditions,
        enabled=workflow.enabled,
        version=workflow.version,
        steps=[WorkflowStepRead.model_validate(step) for step in steps.all()],
        created_at=workflow.created_at,
        updated_at=workflow.updated_at,
    )


def _validated_step_values(step) -> dict:
    return {
        "position": step.position,
        "action_type": str(step.action_type),
        "input_template": step.input_template,
        "require_confirmation": step.require_confirmation,
        "enabled": step.enabled,
    }


@router.get("/settings", response_model=AutomationSettingsRead)
async def get_automation_settings(
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> AutomationSettingsRead:
    workspace_id = await _workspace_for_user(session, current)
    settings = (await session.exec(select(AutomationSettings).where(
        AutomationSettings.workspace_id == workspace_id,
        AutomationSettings.user_id == current.id,
    ))).first()
    if settings is None:
        return AutomationSettingsRead()
    return AutomationSettingsRead(
        automation_mode=settings.automation_mode,
        action_thresholds=settings.action_thresholds,
        action_config=settings.action_config,
        notifications_enabled=settings.notifications_enabled,
        quiet_start_minute=settings.quiet_start_minute,
        quiet_end_minute=settings.quiet_end_minute,
    )


@router.put("/settings", response_model=AutomationSettingsRead)
async def replace_automation_settings(
    data: AutomationSettingsReplace,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> AutomationSettingsRead:
    workspace_id = await _workspace_for_user(session, current)
    settings = (await session.exec(select(AutomationSettings).where(
        AutomationSettings.workspace_id == workspace_id,
        AutomationSettings.user_id == current.id,
    ))).first()
    action_thresholds = {str(action): threshold for action, threshold in data.action_thresholds.items()}
    action_config = {str(action): mode for action, mode in data.action_config.items()}
    if settings is None:
        settings = AutomationSettings(
            workspace_id=workspace_id,
            user_id=current.id,
            automation_mode=data.automation_mode,
            action_thresholds=action_thresholds,
            action_config=action_config,
            notifications_enabled=data.notifications_enabled,
            quiet_start_minute=data.quiet_start_minute,
            quiet_end_minute=data.quiet_end_minute,
        )
    else:
        settings.automation_mode = data.automation_mode
        settings.action_thresholds = action_thresholds
        settings.action_config = action_config
        settings.notifications_enabled = data.notifications_enabled
        settings.quiet_start_minute = data.quiet_start_minute
        settings.quiet_end_minute = data.quiet_end_minute
        settings.updated_at = datetime.now(timezone.utc)
    session.add(settings)
    await session.commit()
    await session.refresh(settings)
    return AutomationSettingsRead(
        automation_mode=settings.automation_mode,
        action_thresholds=settings.action_thresholds,
        action_config=settings.action_config,
        notifications_enabled=settings.notifications_enabled,
        quiet_start_minute=settings.quiet_start_minute,
        quiet_end_minute=settings.quiet_end_minute,
    )


@router.get("", response_model=list[WorkflowRead])
async def list_workflows(
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> list[WorkflowRead]:
    workspace_id = await _workspace_for_user(session, current)
    result = await session.exec(select(Workflow).where(
        Workflow.workspace_id == workspace_id,
        Workflow.user_id == current.id,
    ).order_by(text("created_at DESC")))
    return [await _workflow_read(session, workflow) for workflow in result.all()]


@router.post("", response_model=WorkflowRead, status_code=status.HTTP_201_CREATED)
async def create_workflow(
    data: WorkflowCreate,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> WorkflowRead:
    workspace_id = await _workspace_for_user(session, current)
    workflow = Workflow(
        workspace_id=workspace_id,
        user_id=current.id,
        title=data.title,
        description=data.description,
        trigger_type=data.trigger_type,
        conditions=data.conditions.model_dump(exclude_none=True),
        enabled=data.enabled,
    )
    session.add(workflow)
    await session.flush()
    for step in data.steps:
        session.add(WorkflowStep(workflow_id=workflow.id, **_validated_step_values(step)))
    await session.commit()
    await session.refresh(workflow)
    return await _workflow_read(session, workflow)


# Static execution paths precede /{workflow_id} to avoid UUID-route capture.
@router.get("/executions", response_model=list[ActionExecutionRead])
async def list_action_executions(
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> list[ActionExecutionRead]:
    workspace_id = await _workspace_for_user(session, current)
    rows = await session.exec(select(ActionExecution).where(
        ActionExecution.workspace_id == workspace_id,
        ActionExecution.user_id == current.id,
    ).order_by(text("created_at DESC")).offset(offset).limit(limit))
    return [_action_execution_read(row) for row in rows.all()]


@router.get("/executions/{execution_id}", response_model=ActionExecutionDetailRead)
async def get_action_execution(
    execution_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> ActionExecutionDetailRead:
    workspace_id = await _workspace_for_user(session, current)
    execution = await _owned_execution(session, execution_id, current, workspace_id)
    return _action_execution_detail(execution)


@router.post("/executions/{execution_id}/confirm", response_model=ActionExecutionRead)
async def confirm_action_execution(
    execution_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> ActionExecutionRead:
    await _reject_client_payload(request)
    workspace_id = await _workspace_for_user(session, current)
    execution = await _owned_execution(session, execution_id, current, workspace_id)
    if _action_preview(execution) is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Preview da action indisponível")
    if execution.status == "pending" and isinstance(execution.policy_snapshot, dict) and (
        execution.policy_snapshot.get("confirmed_by_user") is True
    ):
        return _action_execution_read(execution)
    if execution.status != "awaiting_confirmation":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Execução não aguarda confirmação")
    if not await _has_valid_confirmation_provenance(session, execution, workspace_id, current.id):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Proveniência do workflow inválida ou desatualizada")

    snapshot = dict(execution.policy_snapshot)
    snapshot["confirmed_by_user"] = True
    result = await session.exec(update(ActionExecution).where(
        ActionExecution.id == execution.id,
        ActionExecution.workspace_id == workspace_id,
        ActionExecution.user_id == current.id,
        ActionExecution.status == "awaiting_confirmation",
    ).values(
        status="pending",
        policy_snapshot=snapshot,
        updated_at=datetime.now(timezone.utc),
    ))
    if result.rowcount != 1:
        await session.rollback()
        execution = await _owned_execution(session, execution_id, current, workspace_id)
        if execution.status == "pending" and isinstance(execution.policy_snapshot, dict) and (
            execution.policy_snapshot.get("confirmed_by_user") is True
        ):
            return _action_execution_read(execution)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Execução alterada durante a confirmação")
    await session.commit()
    execution = await _owned_execution(session, execution_id, current, workspace_id)
    return _action_execution_read(execution)


@router.post("/executions/{execution_id}/dismiss", response_model=ActionExecutionRead)
async def dismiss_action_execution(
    execution_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> ActionExecutionRead:
    await _reject_client_payload(request)
    workspace_id = await _workspace_for_user(session, current)
    execution = await _owned_execution(session, execution_id, current, workspace_id)
    if execution.status == "failed" and execution.last_error == "user_dismissed":
        return _action_execution_read(execution)
    if execution.status != "awaiting_confirmation":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Execução não aguarda decisão")

    result = await session.exec(update(ActionExecution).where(
        ActionExecution.id == execution.id,
        ActionExecution.workspace_id == workspace_id,
        ActionExecution.user_id == current.id,
        ActionExecution.status == "awaiting_confirmation",
    ).values(
        status="failed",
        last_error="user_dismissed",
        updated_at=datetime.now(timezone.utc),
    ))
    if result.rowcount != 1:
        await session.rollback()
        execution = await _owned_execution(session, execution_id, current, workspace_id)
        if execution.status == "failed" and execution.last_error == "user_dismissed":
            return _action_execution_read(execution)
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Execução alterada durante a decisão")
    await session.commit()
    execution = await _owned_execution(session, execution_id, current, workspace_id)
    return _action_execution_read(execution)


@router.get("/{workflow_id}", response_model=WorkflowRead)
async def get_workflow(
    workflow_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> WorkflowRead:
    workspace_id = await _workspace_for_user(session, current)
    workflow = await _owned_workflow(session, workflow_id, current, workspace_id)
    return await _workflow_read(session, workflow)


@router.patch("/{workflow_id}", response_model=WorkflowRead)
async def update_workflow(
    workflow_id: uuid.UUID,
    data: WorkflowUpdate,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> WorkflowRead:
    workspace_id = await _workspace_for_user(session, current)
    workflow = await _owned_workflow(session, workflow_id, current, workspace_id)
    patch = data.model_dump(exclude_unset=True)
    changed = False

    for field in ("title", "description", "trigger_type", "enabled"):
        if field not in patch:
            continue
        value = patch[field]
        if value is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"{field} não pode ser null")
        if getattr(workflow, field) != value:
            setattr(workflow, field, value)
            changed = True
    if "conditions" in patch:
        conditions = data.conditions.model_dump(exclude_none=True) if data.conditions is not None else {}
        if workflow.conditions != conditions:
            workflow.conditions = conditions
            changed = True

    if data.steps is not None:
        existing_steps = await session.exec(select(WorkflowStep).where(
            WorkflowStep.workflow_id == workflow.id,
        ).order_by(text("position")))
        existing_step_rows = list(existing_steps.all())
        old_steps = [_validated_step_values(step) for step in existing_step_rows]
        new_steps = [_validated_step_values(step) for step in data.steps]
        if old_steps != new_steps:
            for step in existing_step_rows:
                await session.delete(step)
            await session.flush()
            for step in data.steps:
                session.add(WorkflowStep(workflow_id=workflow.id, **_validated_step_values(step)))
            changed = True

    if changed:
        workflow.version += 1
        workflow.updated_at = datetime.now(timezone.utc)
        session.add(workflow)
        await session.commit()
        await session.refresh(workflow)
    return await _workflow_read(session, workflow)


@router.delete("/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_workflow(
    workflow_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    current: User = Depends(get_current_user),
) -> None:
    workspace_id = await _workspace_for_user(session, current)
    workflow = await _owned_workflow(session, workflow_id, current, workspace_id)
    steps = await session.exec(select(WorkflowStep).where(WorkflowStep.workflow_id == workflow.id))
    for step in steps.all():
        await session.delete(step)
    await session.delete(workflow)
    await session.commit()
