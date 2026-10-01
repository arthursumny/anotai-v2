"""Authenticated workflow/settings API validation and ownership tests."""

import uuid


def _workflow_payload(**updates):
    payload = {
        "title": "Receipt review",
        "description": "Create a review task from generated documents",
        "trigger_type": "DOCUMENT_GENERATED",
        "conditions": {
            "source": "gmail",
            "event_type": "invoice",
            "minimum_confidence": 0.8,
        },
        "steps": [
            {
                "position": 0,
                "action_type": "CREATE_TASK",
                "input_template": {"title": "Review invoice", "summary": "Confirm invoice details"},
            },
            {
                "position": 1,
                "action_type": "SEND_TELEGRAM",
                "input_template": {"text": "An invoice needs review."},
            },
        ],
    }
    payload.update(updates)
    return payload


async def _register_other_user(client):
    email = f"workflow-{uuid.uuid4().hex}@example.com"
    created = await client.post("/api/v1/auth/register", json={
        "name": "Other workflow owner", "email": email, "password": "senha-forte-123",
    })
    assert created.status_code == 201, created.text
    login = await client.post(
        "/api/v1/auth/login", data={"username": email, "password": "senha-forte-123"},
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def test_workflow_crud_and_atomic_step_replacement(client, auth_headers):
    headers, _user_id = auth_headers
    created = await client.post("/api/v1/workflows", headers=headers, json=_workflow_payload())
    assert created.status_code == 201, created.text
    workflow = created.json()
    assert workflow["enabled"] is False
    assert workflow["trigger_type"] == "DOCUMENT_GENERATED"
    assert [step["position"] for step in workflow["steps"]] == [0, 1]
    assert workflow["steps"][0]["enabled"] is False
    assert workflow["steps"][0]["require_confirmation"] is False
    assert workflow["steps"][1]["require_confirmation"] is True

    listed = await client.get("/api/v1/workflows", headers=headers)
    assert listed.status_code == 200
    assert [entry["id"] for entry in listed.json()] == [workflow["id"]]
    fetched = await client.get(f"/api/v1/workflows/{workflow['id']}", headers=headers)
    assert fetched.status_code == 200

    patched = await client.patch(f"/api/v1/workflows/{workflow['id']}", headers=headers, json={
        "title": "Updated receipt flow",
        "steps": [{
            "position": 3,
            "action_type": "CREATE_REMINDER",
            "input_template": {"title": "Follow up", "remind_at": "2030-01-01T10:00:00+00:00"},
            "enabled": True,
        }],
    })
    assert patched.status_code == 200, patched.text
    assert patched.json()["title"] == "Updated receipt flow"
    assert patched.json()["version"] == 2
    assert len(patched.json()["steps"]) == 1
    assert patched.json()["steps"][0]["position"] == 3

    deleted = await client.delete(f"/api/v1/workflows/{workflow['id']}", headers=headers)
    assert deleted.status_code == 204
    assert (await client.get(f"/api/v1/workflows/{workflow['id']}", headers=headers)).status_code == 404


async def test_telegram_confirmation_can_be_explicitly_disabled_but_defaults_on(client, auth_headers):
    headers, _user_id = auth_headers
    telegram_step = {
        "position": 0,
        "action_type": "SEND_TELEGRAM",
        "input_template": {"text": "Send the reviewed update."},
    }
    created = await client.post("/api/v1/workflows", headers=headers, json=_workflow_payload(
        steps=[{**telegram_step, "require_confirmation": False}],
    ))
    assert created.status_code == 201, created.text
    assert created.json()["steps"][0]["require_confirmation"] is False

    updated = await client.patch(
        f"/api/v1/workflows/{created.json()['id']}", headers=headers,
        json={"steps": [
            {**telegram_step, "position": 1, "require_confirmation": False},
        ]},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["steps"][0]["require_confirmation"] is False

    omitted_default = await client.post("/api/v1/workflows", headers=headers, json=_workflow_payload(
        steps=[telegram_step],
    ))
    assert omitted_default.status_code == 201, omitted_default.text
    assert omitted_default.json()["steps"][0]["require_confirmation"] is True

    settings = await client.get("/api/v1/workflows/settings", headers=headers)
    assert settings.status_code == 200
    assert settings.json() == {
        "automation_mode": "suggestive", "action_thresholds": {}, "action_config": {},
        "notifications_enabled": True, "quiet_start_minute": None, "quiet_end_minute": None,
    }


async def test_workflows_are_scoped_to_workspace_and_user(client, auth_headers):
    headers, _user_id = auth_headers
    created = await client.post(
        "/api/v1/workflows", headers=headers,
        json=_workflow_payload(steps=[]),
    )
    assert created.status_code == 201, created.text
    workflow_id = created.json()["id"]
    other_headers = await _register_other_user(client)

    assert (await client.get(f"/api/v1/workflows/{workflow_id}", headers=other_headers)).status_code == 404
    assert (await client.patch(
        f"/api/v1/workflows/{workflow_id}", headers=other_headers, json={"enabled": True},
    )).status_code == 404
    assert (await client.delete(
        f"/api/v1/workflows/{workflow_id}", headers=other_headers,
    )).status_code == 404
    assert (await client.get("/api/v1/workflows", headers=other_headers)).json() == []


async def test_workflow_contract_rejects_forged_scope_and_invalid_conditions(client, auth_headers):
    headers, _user_id = auth_headers
    forged = await client.post("/api/v1/workflows", headers=headers, json=_workflow_payload(
        workspaceId=str(uuid.uuid4()), userId=str(uuid.uuid4()),
    ))
    assert forged.status_code == 422

    bad_trigger = await client.post("/api/v1/workflows", headers=headers, json=_workflow_payload(
        trigger_type="WEBHOOK_RECEIVED",
    ))
    assert bad_trigger.status_code == 422
    bad_condition = await client.post("/api/v1/workflows", headers=headers, json=_workflow_payload(
        conditions={"source": "gmail", "expression": "__import__('os').system('x')"},
    ))
    assert bad_condition.status_code == 422
    bad_threshold = await client.post("/api/v1/workflows", headers=headers, json=_workflow_payload(
        conditions={"minimum_confidence": 1.5},
    ))
    assert bad_threshold.status_code == 422


async def test_steps_use_action_registry_and_disallow_urls_secrets_or_providers(client, auth_headers):
    headers, _user_id = auth_headers
    base = "/api/v1/workflows"
    for step in (
        {"position": 0, "action_type": "HTTP_REQUEST", "input_template": {"url": "https://evil.example"}},
        {"position": 0, "action_type": "SEND_TELEGRAM", "input_template": {"text": "https://example.com"}},
        {"position": 0, "action_type": "CREATE_TASK", "input_template": {"title": "Review", "provider_id": "p1"}},
        {"position": 0, "action_type": "CREATE_TASK", "input_template": {"title": "Review", "token": "secret"}},
    ):
        response = await client.post(base, headers=headers, json=_workflow_payload(steps=[step]))
        assert response.status_code == 422

    duplicate_positions = await client.post(base, headers=headers, json=_workflow_payload(steps=[
        {"position": 0, "action_type": "CREATE_TASK", "input_template": {"title": "One"}},
        {"position": 0, "action_type": "CREATE_TASK", "input_template": {"title": "Two"}},
    ]))
    assert duplicate_positions.status_code == 422


async def test_settings_get_put_allow_only_safe_action_modes_and_thresholds(client, auth_headers):
    headers, _user_id = auth_headers
    path = "/api/v1/workflows/settings"
    default = await client.get(path, headers=headers)
    assert default.status_code == 200
    assert default.json() == {
        "automation_mode": "suggestive", "action_thresholds": {}, "action_config": {},
        "notifications_enabled": True, "quiet_start_minute": None, "quiet_end_minute": None,
    }

    replaced = await client.put(path, headers=headers, json={
        "automation_mode": "semi_automatic",
        "action_thresholds": {"CREATE_TASK": 0.75, "SEND_TELEGRAM": 1.0},
        "action_config": {
            "CREATE_TASK": "automatic",
            "SEND_TELEGRAM": "require_confirmation",
        },
    })
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["automation_mode"] == "semi_automatic"
    assert replaced.json()["action_thresholds"]["CREATE_TASK"] == 0.75
    assert replaced.json()["action_config"]["SEND_TELEGRAM"] == "require_confirmation"

    invalid_mode = await client.put(path, headers=headers, json={"automation_mode": "unbounded"})
    invalid_threshold = await client.put(path, headers=headers, json={
        "action_thresholds": {"CREATE_TASK": 2.0},
    })
    invalid_action = await client.put(path, headers=headers, json={
        "action_config": {"SEND_TELEGRAM": "https://evil.example/token"},
    })
    invalid_type = await client.put(path, headers=headers, json={
        "action_config": {"HTTP_REQUEST": "automatic"},
    })
    secret_payload = await client.put(path, headers=headers, json={
        "action_config": {"SEND_TELEGRAM": {"destination": "123", "token": "secret"}},
    })
    assert all(response.status_code == 422 for response in (
        invalid_mode, invalid_threshold, invalid_action, invalid_type, secret_payload,
    ))

    persisted = await client.get(path, headers=headers)
    assert persisted.json()["automation_mode"] == "semi_automatic"
