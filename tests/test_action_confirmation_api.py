"""Safe action-ledger listing and authenticated confirmation/dismissal API tests."""

import uuid

import pytest

from app.models.action_execution import ActionExecution
from app.models.user import User
from app.models.workflow import Workflow
from app.models.workspace import Workspace, WorkspaceMembership


async def _execution(
    session,
    user: User,
    workspace: Workspace,
    *,
    status="awaiting_confirmation",
    authorized=False,
    workflow: Workflow | None = None,
    trigger_type="MANUAL",
    action_type="SEND_TELEGRAM",
    standalone=False,
    action_input=None,
    policy_overrides=None,
):
    snapshot = {
        "automation_mode": "suggestive",
        "action_type": "SEND_TELEGRAM",
        "authorized": authorized,
        "require_confirmation": True,
    }
    if standalone:
        snapshot["standalone"] = True
    elif workflow is not None:
        snapshot["workflow_id"] = str(workflow.id)
    snapshot.update(policy_overrides or {})
    execution = ActionExecution(
        workspace_id=workspace.id,
        user_id=user.id,
        trigger_type=trigger_type,
        source_id="source-safe-id",
        action_type=action_type,
        idempotency_key=f"confirm-{uuid.uuid4()}",
        action_input=(
            action_input if action_input is not None else
            {"text": "private telegram text"} if action_type == "SEND_TELEGRAM" else
            {"title": "Review task"} if action_type == "CREATE_TASK" else
            {"summary": "Review appointment", "start_at": "2030-01-01T10:00:00+00:00", "end_at": "2030-01-01T11:00:00+00:00"} if action_type == "CREATE_CALENDAR_EVENT" else
            {"title": "Review reminder", "remind_at": "2030-01-01T10:00:00+00:00"}
        ),
        policy_snapshot=snapshot,
        workflow_version=workflow.version if workflow is not None else None,
        status=status,
        attempts=1,
        last_error="provider exception includes credential private-token" if status == "failed" else None,
    )
    session.add(execution)
    await session.flush()
    return execution


async def _second_user(client):
    email = f"confirmation-{uuid.uuid4().hex}@example.com"
    response = await client.post("/api/v1/auth/register", json={
        "name": "Other user", "email": email, "password": "senha-forte-123",
    })
    assert response.status_code == 201, response.text
    response = await client.post(
        "/api/v1/auth/login", data={"username": email, "password": "senha-forte-123"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def test_action_listing_and_detail_expose_only_safe_projection(client, auth_headers, db_session):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    assert user is not None and user.workspace_id is not None
    workspace = await db_session.get(Workspace, user.workspace_id)
    assert workspace is not None
    workflow = Workflow(
        workspace_id=workspace.id, user_id=user.id, title="Confirmable",
        trigger_type="MANUAL", enabled=True,
    )
    db_session.add(workflow)
    await db_session.flush()
    execution = await _execution(db_session, user, workspace, workflow=workflow)
    failed_execution = await _execution(
        db_session, user, workspace, workflow=workflow, status="failed",
    )

    listed = await client.get("/api/v1/workflows/executions", headers=headers)
    assert listed.status_code == 200
    row = next(item for item in listed.json() if item["id"] == str(execution.id))
    assert row == {
        "id": str(execution.id),
        "trigger_type": "MANUAL",
        "action_type": "SEND_TELEGRAM",
        "status": "awaiting_confirmation",
        "attempts": 1,
        "created_at": row["created_at"],
        "last_error": None,
        "source_id": "source-safe-id",
    }
    detail = await client.get(f"/api/v1/workflows/executions/{execution.id}", headers=headers)
    assert detail.status_code == 200
    assert "action_input" not in detail.json()
    assert "policy_snapshot" not in detail.json()
    assert detail.json()["preview_available"] is True
    assert detail.json()["preview"] == {
        "action_type": "SEND_TELEGRAM", "text": "private telegram text",
    }
    assert "token" not in detail.text and "private-token" not in detail.text
    failed_row = next(item for item in listed.json() if item["id"] == str(failed_execution.id))
    assert failed_row["last_error"] == "A execução falhou"
    assert "private-token" not in str(failed_row)


async def test_malformed_action_input_has_no_preview_and_cannot_be_confirmed(
    client, auth_headers, db_session,
):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    assert user is not None and user.workspace_id is not None
    workspace = await db_session.get(Workspace, user.workspace_id)
    assert workspace is not None
    workflow = Workflow(
        workspace_id=workspace.id, user_id=user.id, title="Malformed input",
        trigger_type="MANUAL", enabled=True,
    )
    db_session.add(workflow)
    await db_session.flush()
    execution = await _execution(
        db_session, user, workspace, workflow=workflow,
        action_input={"text": "Should not approve", "token": "never-expose-this"},
    )

    detail = await client.get(f"/api/v1/workflows/executions/{execution.id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["preview_available"] is False
    assert detail.json()["preview"] is None
    assert "never-expose-this" not in detail.text
    confirm = await client.post(
        f"/api/v1/workflows/executions/{execution.id}/confirm", headers=headers,
    )
    assert confirm.status_code == 409
    await db_session.refresh(execution)
    assert execution.status == "awaiting_confirmation"


async def test_detail_preview_is_allowlisted_for_each_supported_action(client, auth_headers, db_session):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    assert user is not None and user.workspace_id is not None
    workspace = await db_session.get(Workspace, user.workspace_id)
    assert workspace is not None
    workflow = Workflow(
        workspace_id=workspace.id, user_id=user.id, title="Preview workflow",
        trigger_type="MANUAL", enabled=True,
    )
    db_session.add(workflow)
    await db_session.flush()

    cases = [
        (
            "CREATE_TASK",
            {"title": "Review task", "summary": "Check total", "due_at": "2030-01-01T10:00:00+00:00", "confidence": 0.9},
            {"action_type": "CREATE_TASK", "title": "Review task", "summary": "Check total", "due_at": "2030-01-01T10:00:00+00:00"},
        ),
        (
            "CREATE_REMINDER",
            {"title": "Review reminder", "remind_at": "2030-02-01T11:00:00+00:00", "confidence": 0.8},
            {"action_type": "CREATE_REMINDER", "title": "Review reminder", "remind_at": "2030-02-01T11:00:00+00:00"},
        ),
        (
            "SEND_TELEGRAM",
            {"text": "Review the approved item."},
            {"action_type": "SEND_TELEGRAM", "text": "Review the approved item."},
        ),
        (
            "CREATE_CALENDAR_EVENT",
            {
                "summary": "Review appointment",
                "start_at": "2030-01-01T10:00:00+00:00",
                "end_at": "2030-01-01T11:00:00+00:00",
                "timezone": "America/Sao_Paulo",
            },
            {
                "action_type": "CREATE_CALENDAR_EVENT",
                "summary": "Review appointment",
                "start_at": "2030-01-01T10:00:00+00:00",
                "end_at": "2030-01-01T11:00:00+00:00",
                "timezone": "America/Sao_Paulo",
            },
        ),
    ]
    for action_type, action_input, expected_preview in cases:
        execution = await _execution(
            db_session, user, workspace, workflow=workflow,
            action_type=action_type, action_input=action_input,
        )
        response = await client.get(
            f"/api/v1/workflows/executions/{execution.id}", headers=headers,
        )
        assert response.status_code == 200
        assert response.json()["preview_available"] is True
        assert response.json()["preview"] == expected_preview


async def test_calendar_event_preview_confirmation_is_safe_and_default_is_suggestive(
    client, auth_headers, db_session, monkeypatch,
):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    assert user is not None and user.workspace_id is not None
    workspace = await db_session.get(Workspace, user.workspace_id)
    assert workspace is not None
    workflow = Workflow(
        workspace_id=workspace.id, user_id=user.id, title="Calendar confirmation",
        trigger_type="MANUAL", enabled=True,
    )
    db_session.add(workflow)
    await db_session.flush()
    execution = await _execution(
        db_session, user, workspace, workflow=workflow,
        action_type="CREATE_CALENDAR_EVENT",
        action_input={
            "summary": "Team sync", "start_at": "2030-02-01T12:00:00+00:00",
            "end_at": "2030-02-01T13:00:00+00:00", "timezone": "UTC",
        },
    )

    defaults = await client.get("/api/v1/workflows/settings", headers=headers)
    assert defaults.status_code == 200
    assert defaults.json()["automation_mode"] == "suggestive"
    detail = await client.get(f"/api/v1/workflows/executions/{execution.id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["preview_available"] is True
    assert detail.json()["preview"] == {
        "action_type": "CREATE_CALENDAR_EVENT", "summary": "Team sync",
        "start_at": "2030-02-01T12:00:00+00:00", "end_at": "2030-02-01T13:00:00+00:00",
        "timezone": "UTC",
    }
    assert all(field not in detail.text for field in ("attendees", "calendarId", "connectionId", "token"))

    def unexpected_provider_call(*_args, **_kwargs):
        pytest.fail("confirmation endpoint must not invoke a provider")

    monkeypatch.setattr("app.services.integration_service.IntegrationService.execute_claimed", unexpected_provider_call)
    confirmed = await client.post(f"/api/v1/workflows/executions/{execution.id}/confirm", headers=headers)
    assert confirmed.status_code == 200 and confirmed.json()["status"] == "pending"
    await db_session.refresh(execution)
    assert execution.status == "pending"


@pytest.mark.parametrize("invalid_input", [
    {"summary": "Missing end", "start_at": "2030-02-01T12:00:00+00:00"},
    {"summary": "Extra field", "start_at": "2030-02-01T12:00:00+00:00", "end_at": "2030-02-01T13:00:00+00:00", "attendees": ["private@example.com"]},
    {"summary": "Naive time", "start_at": "2030-02-01T12:00:00", "end_at": "2030-02-01T13:00:00+00:00"},
])
async def test_invalid_calendar_input_has_no_preview_or_confirmation(
    client, auth_headers, db_session, invalid_input,
):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    assert user is not None and user.workspace_id is not None
    workspace = await db_session.get(Workspace, user.workspace_id)
    assert workspace is not None
    workflow = Workflow(
        workspace_id=workspace.id, user_id=user.id, title="Invalid calendar",
        trigger_type="MANUAL", enabled=True,
    )
    db_session.add(workflow)
    await db_session.flush()
    execution = await _execution(
        db_session, user, workspace, workflow=workflow,
        action_type="CREATE_CALENDAR_EVENT", action_input=invalid_input,
    )
    detail = await client.get(f"/api/v1/workflows/executions/{execution.id}", headers=headers)
    assert detail.status_code == 200 and detail.json()["preview_available"] is False
    assert detail.json()["preview"] is None
    rejected = await client.post(
        f"/api/v1/workflows/executions/{execution.id}/confirm", headers=headers,
    )
    assert rejected.status_code == 409
    await db_session.refresh(execution)
    assert execution.status == "awaiting_confirmation"


async def test_confirm_is_conditional_idempotent_and_preserves_action_input(client, auth_headers, db_session):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    assert user is not None and user.workspace_id is not None
    workspace = await db_session.get(Workspace, user.workspace_id)
    assert workspace is not None
    workflow = Workflow(
        workspace_id=workspace.id, user_id=user.id, title="Manual approval",
        trigger_type="MANUAL", enabled=True,
    )
    db_session.add(workflow)
    await db_session.flush()
    execution = await _execution(
        db_session, user, workspace, workflow=workflow, authorized=False,
    )
    original_input = dict(execution.action_input)

    response = await client.post(f"/api/v1/workflows/executions/{execution.id}/confirm", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "pending"
    repeated = await client.post(f"/api/v1/workflows/executions/{execution.id}/confirm", headers=headers)
    assert repeated.status_code == 200
    assert repeated.json()["status"] == "pending"
    await db_session.refresh(execution)
    assert execution.policy_snapshot["confirmed_by_user"] is True
    assert execution.policy_snapshot["authorized"] is False
    assert execution.action_input == original_input


async def test_confirm_rejects_deleted_disabled_or_stale_workflow(client, auth_headers, db_session):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    assert user is not None and user.workspace_id is not None
    workspace = await db_session.get(Workspace, user.workspace_id)
    assert workspace is not None

    for state in ("deleted", "disabled", "stale"):
        workflow = Workflow(
            workspace_id=workspace.id, user_id=user.id, title=f"{state} workflow",
            trigger_type="MANUAL", enabled=True,
        )
        db_session.add(workflow)
        await db_session.flush()
        execution = await _execution(db_session, user, workspace, workflow=workflow)
        if state == "deleted":
            await db_session.delete(workflow)
        elif state == "disabled":
            workflow.enabled = False
        else:
            workflow.version += 1
        await db_session.flush()

        response = await client.post(
            f"/api/v1/workflows/executions/{execution.id}/confirm", headers=headers,
        )
        assert response.status_code == 409
        await db_session.refresh(execution)
        assert execution.status == "awaiting_confirmation"


async def test_confirm_allows_only_scheduler_standalone_provenance(client, auth_headers, db_session):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    assert user is not None and user.workspace_id is not None
    workspace = await db_session.get(Workspace, user.workspace_id)
    assert workspace is not None
    standalone = await _execution(
        db_session, user, workspace, standalone=True, trigger_type="REMINDER_DUE",
    )
    accepted = await client.post(
        f"/api/v1/workflows/executions/{standalone.id}/confirm", headers=headers,
    )
    assert accepted.status_code == 200
    assert accepted.json()["status"] == "pending"

    invalid = await _execution(
        db_session, user, workspace, standalone=True, trigger_type="MANUAL",
    )
    rejected = await client.post(
        f"/api/v1/workflows/executions/{invalid.id}/confirm", headers=headers,
    )
    assert rejected.status_code == 409


async def test_dismiss_is_terminal_idempotent_and_pending_is_untouched(client, auth_headers, db_session):
    headers, user_id = auth_headers
    user = await db_session.get(User, uuid.UUID(user_id))
    assert user is not None and user.workspace_id is not None
    workspace = await db_session.get(Workspace, user.workspace_id)
    assert workspace is not None
    workflow = Workflow(
        workspace_id=workspace.id, user_id=user.id, title="Dismiss workflow",
        trigger_type="MANUAL", enabled=True,
    )
    db_session.add(workflow)
    await db_session.flush()
    execution = await _execution(db_session, user, workspace, workflow=workflow)
    dismissed = await client.post(
        f"/api/v1/workflows/executions/{execution.id}/dismiss", headers=headers,
    )
    assert dismissed.status_code == 200
    assert dismissed.json()["status"] == "failed"
    assert dismissed.json()["last_error"] == "Execução dispensada pelo usuário"
    repeated = await client.post(
        f"/api/v1/workflows/executions/{execution.id}/dismiss", headers=headers,
    )
    assert repeated.status_code == 200
    assert "private-token" not in repeated.text

    pending = await _execution(
        db_session, user, workspace, workflow=workflow, status="pending",
    )
    rejected = await client.post(
        f"/api/v1/workflows/executions/{pending.id}/dismiss", headers=headers,
    )
    assert rejected.status_code == 409
    await db_session.refresh(pending)
    assert pending.status == "pending"


async def test_cross_user_and_forged_request_payloads_are_rejected(client, auth_headers, db_session):
    headers, _user_id = auth_headers
    other_headers = await _second_user(client)
    other_list = await client.get("/api/v1/workflows/executions", headers=other_headers)
    assert other_list.status_code == 200 and other_list.json() == []
    other_uid = (await client.get("/api/v1/auth/me", headers=other_headers)).json()["id"]
    other_user = await db_session.get(User, uuid.UUID(other_uid))
    assert other_user is not None and other_user.workspace_id is not None
    workspace = await db_session.get(Workspace, other_user.workspace_id)
    assert workspace is not None
    workflow = Workflow(
        workspace_id=workspace.id, user_id=other_user.id, title="Other owner",
        trigger_type="MANUAL", enabled=True,
    )
    db_session.add(workflow)
    await db_session.flush()
    execution = await _execution(
        db_session, other_user, workspace, workflow=workflow,
        action_type="CREATE_CALENDAR_EVENT",
        action_input={
            "summary": "Private event", "start_at": "2030-01-01T10:00:00+00:00",
            "end_at": "2030-01-01T11:00:00+00:00",
        },
    )

    for method, path in (
        (client.get, f"/api/v1/workflows/executions/{execution.id}"),
        (client.post, f"/api/v1/workflows/executions/{execution.id}/confirm"),
        (client.post, f"/api/v1/workflows/executions/{execution.id}/dismiss"),
    ):
        response = await method(path, headers=headers)
        assert response.status_code == 404

    forged = await client.post(
        f"/api/v1/workflows/executions/{execution.id}/confirm",
        headers=other_headers,
        json={"action_input": {"text": "forged", "chat_id": "attacker"}},
    )
    assert forged.status_code == 422
