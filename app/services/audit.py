"""Content-free, fail-closed audit event writing."""

import re
import uuid
from collections.abc import Mapping

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.audit_event import AuditEvent
from app.models.workspace import Workspace, WorkspaceMembership

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,254}$")
_SOURCES = {"audio", "gmail", "whatsapp", "workflow", "internal"}
_METADATA_ENUMS = {
    "source": _SOURCES,
    "reason_code": {"retention", "user_delete", "disconnect", "workflow"},
    "channel": {"in_app", "telegram"},
}


def _valid_identifier(value: str, *, maximum: int) -> bool:
    return isinstance(value, str) and len(value) <= maximum and _IDENTIFIER.fullmatch(value) is not None


def _safe_metadata(metadata: Mapping | None) -> dict:
    if metadata is None:
        return {}
    if not isinstance(metadata, Mapping):
        raise ValueError("metadata must be a mapping of approved values")
    result = {}
    for key, value in metadata.items():
        if key not in {"count", "retention_days", *_METADATA_ENUMS}:
            raise ValueError("unsupported audit metadata key")
        if key == "count":
            if type(value) is not int or value < 0:
                raise ValueError("count must be a non-negative integer")
        elif key == "retention_days":
            if type(value) is not int or not 1 <= value <= 365:
                raise ValueError("retention_days must be between 1 and 365")
        elif not isinstance(value, str) or value not in _METADATA_ENUMS[key]:
            raise ValueError(f"invalid audit metadata value for {key}")
        result[key] = value
    return result


async def write_audit(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID | None,
    actor: str = "user",
    event: str,
    action: str,
    status: str,
    source: str,
    target_kind: str,
    target_ref: str | None = None,
    metadata: Mapping | None = None,
) -> AuditEvent:
    """Add a validated audit record to the caller's transaction; never commits."""
    if actor not in {"user", "system"}:
        raise ValueError("invalid audit actor")
    if not all((
        _valid_identifier(event, maximum=80),
        _valid_identifier(action, maximum=80),
        _valid_identifier(status, maximum=40),
        _valid_identifier(source, maximum=40),
        source in _SOURCES,
        _valid_identifier(target_kind, maximum=40),
        target_ref is None or _valid_identifier(target_ref, maximum=255),
    )):
        raise ValueError("invalid audit identifier")
    safe_metadata = _safe_metadata(metadata)

    workspace = await session.get(Workspace, workspace_id)
    if workspace is None:
        raise ValueError("workspace does not exist")
    if actor == "user":
        if user_id is None:
            raise ValueError("user audit events require a user")
        membership = (await session.exec(select(WorkspaceMembership).where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.user_id == user_id,
        ))).first()
        if membership is None:
            raise ValueError("user is not a workspace member")

    record = AuditEvent(
        workspace_id=workspace_id,
        user_id=user_id,
        actor=actor,
        event=event,
        action=action,
        status=status,
        source=source,
        target_kind=target_kind,
        target_ref=target_ref,
        metadata_json=safe_metadata,
    )
    session.add(record)
    return record
