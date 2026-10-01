"""Fail-closed audit writes and private, bounded audit reads."""

import uuid

import pytest
from sqlmodel import select

from app.models.audit_event import AuditEvent
from app.models.user import User
from app.models.workspace import Workspace, WorkspaceMembership
from app.services.audit import write_audit


@pytest.mark.asyncio
async def test_audit_write_accepts_allowlisted_metadata_and_does_not_commit(db_session, auth_headers):
    _headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    record = await write_audit(
        db_session,
        workspace_id=user.workspace_id,
        user_id=user.id,
        event="retention.run",
        action="delete",
        status="ok",
        source="internal",
        target_kind="retention",
        target_ref="batch-123",
        metadata={"count": 2, "retention_days": 30, "reason_code": "retention", "source": "gmail"},
    )
    assert record.metadata_json == {
        "count": 2, "retention_days": 30, "reason_code": "retention", "source": "gmail",
    }
    assert record in db_session.new  # pending in the caller's transaction; writer did not commit


@pytest.mark.asyncio
@pytest.mark.parametrize("metadata", [
    {"content": "secret body"},
    {"title": "private"},
    {"email": "person@example.com"},
    {"phone": "+15551234567"},
    {"token": "secret"},
    {"secret": "secret"},
    {"raw": {"message": "body"}},
    {"count": True},
    {"count": -1},
    {"retention_days": 366},
    {"source": "arbitrary"},
    {"channel": ["telegram"]},
])
async def test_audit_write_rejects_pii_secrets_nested_and_invalid_values(db_session, auth_headers, metadata):
    _headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    with pytest.raises(ValueError):
        await write_audit(
            db_session,
            workspace_id=user.workspace_id,
            user_id=user.id,
            event="privacy.change",
            action="update",
            status="ok",
            source="internal",
            target_kind="policy",
            metadata=metadata,
        )
    assert not (await db_session.exec(select(AuditEvent))).all()


@pytest.mark.asyncio
async def test_audit_write_validates_identifiers_and_membership(db_session, auth_headers):
    _headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    args = dict(
        workspace_id=user.workspace_id,
        user_id=user.id,
        event="privacy.change",
        action="update",
        status="ok",
        source="internal",
        target_kind="policy",
    )
    with pytest.raises(ValueError):
        await write_audit(db_session, **{**args, "target_ref": "person@example.com"})
    with pytest.raises(ValueError):
        await write_audit(db_session, **{**args, "event": "person@jid"})
    membership = (await db_session.exec(select(WorkspaceMembership).where(
        WorkspaceMembership.workspace_id == user.workspace_id,
        WorkspaceMembership.user_id == user.id,
    ))).one()
    await db_session.delete(membership)
    with pytest.raises(ValueError):
        await write_audit(db_session, **args)


@pytest.mark.asyncio
async def test_system_audit_requires_real_workspace_and_can_have_no_user(db_session, auth_headers):
    _headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    record = await write_audit(
        db_session,
        workspace_id=user.workspace_id,
        user_id=None,
        actor="system",
        event="retention.run",
        action="delete",
        status="ok",
        source="internal",
        target_kind="retention",
        metadata={"count": 0},
    )
    assert record.actor == "system" and record.user_id is None
    with pytest.raises(ValueError):
        await write_audit(
            db_session,
            workspace_id=uuid.uuid4(),
            user_id=None,
            actor="system",
            event="retention.run",
            action="delete",
            status="ok",
            source="internal",
            target_kind="retention",
        )


@pytest.mark.asyncio
async def test_audit_get_is_private_safe_and_paged(client, auth_headers, db_session):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    other = User(name="Other member", email=f"{uuid.uuid4()}@example.com", hashed_password="unused",
                 workspace_id=user.workspace_id)
    other_workspace = Workspace(name="Other workspace")
    db_session.add_all([other, other_workspace])
    await db_session.flush()
    db_session.add(WorkspaceMembership(workspace_id=user.workspace_id, user_id=other.id))
    db_session.add_all([
        AuditEvent(
            workspace_id=user.workspace_id, user_id=user.id, actor="user", event=f"own.{i}",
            action="read", status="ok", source="internal", target_kind="policy",
            metadata_json={"content": "plaintext must never be returned"} if i == 0 else {"count": i},
        ) for i in range(3)
    ])
    db_session.add(AuditEvent(
        workspace_id=user.workspace_id, user_id=other.id, actor="user", event="other.user",
        action="read", status="ok", source="internal", target_kind="policy",
    ))
    db_session.add(AuditEvent(
        workspace_id=user.workspace_id, user_id=None, actor="system", event="system.run",
        action="delete", status="ok", source="internal", target_kind="retention",
        metadata_json={"count": 1},
    ))
    db_session.add(AuditEvent(
        workspace_id=other_workspace.id, user_id=None, actor="system", event="foreign.system",
        action="delete", status="ok", source="internal", target_kind="retention",
    ))
    await db_session.commit()

    first = await client.get("/api/v1/audit?limit=2&offset=0", headers=headers)
    assert first.status_code == 200, first.text
    payload = first.json()
    assert len(payload["items"]) == 2
    assert {item["event"] for item in payload["items"]} <= {"own.0", "own.1", "own.2", "system.run"}
    assert all("user_id" not in item and "content" not in item and "email" not in item for item in payload["items"])
    assert "plaintext must never be returned" not in first.text
    assert (await client.get("/api/v1/audit?limit=101", headers=headers)).status_code == 422
    assert (await client.get("/api/v1/audit?offset=-1", headers=headers)).status_code == 422
    assert (await client.get("/api/v1/audit", headers=headers)).status_code == 200
    assert (await client.get("/api/v1/audit")).status_code == 401
