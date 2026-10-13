"""Prepare and authorize template actions; execution belongs to action_executor."""

import json
import re
import uuid
from datetime import timezone
from hashlib import sha256
from typing import Any

from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.action_execution import ActionExecution
from app.models.automation_settings import AutomationSettings
from app.models.generated_document import GeneratedDocument
from app.models.note import Note, NoteStatus
from app.models.template import Template
from app.models.user import User
from app.models.workspace import WorkspaceMembership
from app.schemas.template import ACTION_CONFIG_MODELS, ActionConfig
from app.schemas.workflow_action import ActionValidationError, StructuredAction
from app.services.action_registry import validate_action

_VARIABLE = re.compile(r"(?<!\$)\{([A-Za-z_][A-Za-z0-9_.]*)\}")
_ACTION_TYPES = {
    "email": "SEND_EMAIL",
    "create_task": "CREATE_TASK",
    "telegram": "SEND_TELEGRAM",
    "webhook": "SEND_WEBHOOK",
    "mcp_tool": "CALL_MCP_TOOL",
}


class TemplateActionError(ValueError):
    """The saved generation or template no longer authorizes an action."""


def _digest(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()).hexdigest()


def render_action_value(value: Any, context: dict[str, Any]) -> Any:
    """Interpolate once, preserving JSON types for whole-field placeholders."""
    def lookup(path: str) -> Any:
        current = context
        for part in path.split("."):
            if not isinstance(current, dict) or part not in current:
                raise TemplateActionError("missing_template_variable")
            current = current[part]
        if current is None:
            raise TemplateActionError("missing_template_variable")
        return current

    if isinstance(value, str):
        whole = _VARIABLE.fullmatch(value)
        if whole:
            return lookup(whole.group(1))

        def replace(match: re.Match[str]) -> str:
            item = lookup(match.group(1))
            return item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)

        return _VARIABLE.sub(replace, value)
    if isinstance(value, dict):
        return {key: render_action_value(item, context) for key, item in value.items()}
    if isinstance(value, list):
        return [render_action_value(item, context) for item in value]
    return value


def _context(note: Note, document: GeneratedDocument) -> dict[str, Any]:
    return {
        **document.structured_data,
        "structured": document.structured_data,
        "structured_data": document.structured_data,
        "title": note.title,
        "body": document.rendered_content,
        "summary": note.summary,
        "note_id": str(note.id),
        "transcription": note.raw_transcription,
        "transcricao": note.raw_transcription,
        "created_at": note.created_at.replace(tzinfo=timezone.utc).isoformat(),
    }


def _canonical_action(
    configured: ActionConfig, note: Note, document: GeneratedDocument,
) -> StructuredAction:
    config = ACTION_CONFIG_MODELS[configured.type].model_validate(configured.config)
    context = _context(note, document)
    match configured.type:
        case "email" | "create_task":
            payload = render_action_value(config.model_dump(), context)
        case "telegram":
            payload = {"text": render_action_value(config.message or config.template, context)}
        case "webhook":
            body = config.body if config.body is not None else {
                "note_id": str(note.id), "template_id": str(document.template_id),
                "title": note.title, "body": document.rendered_content,
                "summary": note.summary, "structured_data": document.structured_data,
            }
            payload = {"body": render_action_value(body, context) if config.body is not None else body}
        case "mcp_tool":
            payload = {"arguments": render_action_value(config.arguments_mapping, context)}
    return validate_action(
        {"type": _ACTION_TYPES[configured.type], **payload}, template_authorized=True,
    )


async def _owned_generation(
    session: AsyncSession, note_id: uuid.UUID, user_id: uuid.UUID,
) -> tuple[Note, User, Template, GeneratedDocument]:
    note = await session.get(Note, note_id)
    user = await session.get(User, user_id)
    if note is None or note.user_id != user_id or user is None or user.workspace_id is None:
        raise TemplateActionError("generation_scope")
    if note.status != NoteStatus.PROCESSED or note.template_id is None:
        raise TemplateActionError("generation_not_processed")
    template = await session.get(Template, note.template_id)
    membership = (await session.exec(select(WorkspaceMembership.id).where(
        WorkspaceMembership.workspace_id == user.workspace_id,
        WorkspaceMembership.user_id == user_id,
    ))).first()
    if template is None or template.user_id != user_id or membership is None:
        raise TemplateActionError("template_scope")
    document = (await session.exec(select(GeneratedDocument).where(
        GeneratedDocument.note_id == note.id,
        GeneratedDocument.workspace_id == user.workspace_id,
        GeneratedDocument.user_id == user_id,
    ))).first()
    if (
        document is None or document.status != "generated"
        or document.template_id != template.id
        or document.template_version != str(template.version)
    ):
        raise TemplateActionError("generation_template_version")
    return note, user, template, document


async def _automation_disabled(session: AsyncSession, user: User, action_type: str | None = None) -> bool:
    prefs = (await session.exec(select(AutomationSettings).where(
        AutomationSettings.workspace_id == user.workspace_id,
        AutomationSettings.user_id == user.id,
    ))).first()
    return prefs is not None and (
        prefs.automation_mode == "disabled" or prefs.action_config.get(action_type) == "disabled"
    )


def _key(user: User, note: Note, document: GeneratedDocument, template: Template, index: int) -> str:
    # A retry or a template edit must not create a second action for this
    # generation. Changed versions invalidate old rows instead of resending.
    return "template-action-v1:" + _digest([
        str(user.workspace_id), str(user.id), str(note.id),
        str(document.id), str(template.id), index,
    ])


async def prepare_template_actions(
    session: AsyncSession, note: Note, user_id: uuid.UUID,
) -> list[ActionExecution]:
    """Flush idempotent ledgers after note+document persistence; never commit/send.

    General notes and non-processed notes have no template actions. Invalid
    enabled configurations produce failed rows without poisoning their siblings.
    """
    saved = await session.get(Note, note.id)
    if saved is None or saved.user_id != user_id:
        raise TemplateActionError("generation_scope")
    if saved.status != NoteStatus.PROCESSED or saved.template_id is None:
        return []
    saved, user, template, document = await _owned_generation(session, saved.id, user_id)
    if await _automation_disabled(session, user):
        return []
    rows = []
    for index, raw in enumerate(template.actions):
        if not isinstance(raw, dict) or raw.get("enabled") is not True:
            continue
        raw_type = raw.get("type")
        action_type = _ACTION_TYPES.get(raw_type, "INVALID_TEMPLATE_ACTION") if isinstance(raw_type, str) else "INVALID_TEMPLATE_ACTION"
        if await _automation_disabled(session, user, action_type):
            continue
        key = _key(user, saved, document, template, index)
        existing = (await session.exec(select(ActionExecution).where(
            ActionExecution.workspace_id == user.workspace_id,
            ActionExecution.idempotency_key == key,
        ))).first()
        if existing is not None:
            rows.append(existing)
            continue
        action_input = None
        error = None
        require_confirmation = raw.get("require_confirmation", False) is True
        try:
            configured = ActionConfig.model_validate(raw)
            action = _canonical_action(configured, saved, document)
            action_input = action.model_dump(mode="json", exclude={"type"})
        except (ValidationError, ActionValidationError, TemplateActionError) as exc:
            error = str(exc) if isinstance(exc, TemplateActionError) else "invalid_template_action"
        row = ActionExecution(
            workspace_id=user.workspace_id, user_id=user.id,
            trigger_type="DOCUMENT_GENERATED", source_id=str(saved.id),
            action_type=action_type, idempotency_key=key, action_input=action_input,
            status="failed" if error else ("awaiting_confirmation" if require_confirmation else "pending"),
            last_error=error,
            policy_snapshot={
                "origin": "template", "authorized": True,
                "require_confirmation": require_confirmation,
                "template_id": str(template.id), "template_version": template.version,
                "document_id": str(document.id), "action_index": index,
                "action_version": _digest(raw), "context_version": _digest(_context(saved, document)),
            },
        )
        try:
            async with session.begin_nested():
                session.add(row)
                await session.flush()
        except IntegrityError:
            row = (await session.exec(select(ActionExecution).where(
                ActionExecution.workspace_id == user.workspace_id,
                ActionExecution.idempotency_key == key,
            ))).one()
        rows.append(row)
    return rows


async def validate_template_origin(
    session: AsyncSession, execution: ActionExecution,
) -> tuple[ActionConfig, Note, GeneratedDocument]:
    """Recheck owner, selection, versions, payload and current authorization."""
    snapshot = execution.policy_snapshot
    try:
        note_id = uuid.UUID(execution.source_id)
        note, user, template, document = await _owned_generation(session, note_id, execution.user_id)
        index = snapshot["action_index"]
        if not isinstance(index, int) or isinstance(index, bool) or index < 0:
            raise TemplateActionError("template_action_index")
        raw = template.actions[index]
        configured = ActionConfig.model_validate(raw)
        action = _canonical_action(configured, note, document)
        if (
            execution.workspace_id != user.workspace_id
            or execution.trigger_type != "DOCUMENT_GENERATED"
            or execution.workflow_execution_id is not None or execution.workflow_step_id is not None
            or snapshot.get("template_id") != str(template.id)
            or snapshot.get("document_id") != str(document.id)
            or snapshot.get("template_version") != template.version
            or snapshot.get("action_version") != _digest(raw)
            or snapshot.get("context_version") != _digest(_context(note, document))
            or raw.get("enabled") is not True
            or snapshot.get("require_confirmation") is not configured.require_confirmation
            or execution.action_type != action.type.value
            or execution.action_input != action.model_dump(mode="json", exclude={"type"})
            or execution.idempotency_key != _key(user, note, document, template, index)
            or await _automation_disabled(session, user, execution.action_type)
        ):
            raise TemplateActionError("template_authorization_changed")
    except (ValueError, TypeError, KeyError, IndexError, AttributeError) as exc:
        raise TemplateActionError("template_provenance_invalid") from exc
    return configured, note, document
