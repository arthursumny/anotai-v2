"""API-written workflow steps remain directly consumable by the engine."""

import uuid

from sqlmodel import select

from app.models.action_execution import ActionExecution
from app.models.user import User
from app.services.workflow_engine import prepare_workflow_executions


async def test_workflow_created_through_api_prepares_without_action_type_in_template(
    client, auth_headers, db_session
):
    headers, user_id_text = auth_headers
    response = await client.post(
        "/api/v1/workflows",
        headers=headers,
        json={
            "title": "Prepare an internal task",
            "description": "Create a task when a document is generated",
            "trigger_type": "DOCUMENT_GENERATED",
            "enabled": True,
            "steps": [{
                "position": 0,
                "action_type": "CREATE_TASK",
                "input_template": {"title": "Review generated document", "summary": "Confirm details"},
                "enabled": True,
            }],
        },
    )
    assert response.status_code == 201, response.text
    workflow_data = response.json()
    step_data = workflow_data["steps"][0]
    assert "type" not in step_data["input_template"]

    user = await db_session.get(User, uuid.UUID(user_id_text))
    assert user is not None and user.workspace_id is not None
    prepared = await prepare_workflow_executions(
        db_session,
        user.workspace_id,
        user.id,
        "DOCUMENT_GENERATED",
        source_id="api-document-001",
    )

    assert len(prepared) == 1
    assert str(prepared[0].workflow_id) == workflow_data["id"]
    action_rows = list((await db_session.exec(select(ActionExecution))).all())
    assert len(action_rows) == 1
    assert action_rows[0].action_type == "CREATE_TASK"
    assert action_rows[0].status == "awaiting_confirmation"
    assert action_rows[0].action_input == {
        "title": "Review generated document",
        "summary": "Confirm details",
        "confidence": None,
        "due_at": None,
    }
    assert action_rows[0].workflow_step_id == uuid.UUID(step_data["id"])
    assert action_rows[0].workflow_version == workflow_data["version"]
    assert action_rows[0].policy_snapshot["workflow_id"] == workflow_data["id"]
    assert action_rows[0].policy_snapshot["workflow_version"] == workflow_data["version"]
