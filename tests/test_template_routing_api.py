"""Template metadata/versioning, routing-rule ownership, and migration tests."""

import importlib.metadata
import importlib.util
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

# The repository's Alembic scripts directory shadows the installed package.
_alembic_package = importlib.metadata.distribution("alembic").locate_file("alembic")
_alembic_spec = importlib.util.spec_from_file_location(
    "alembic", str(_alembic_package / "__init__.py"),
    submodule_search_locations=[str(_alembic_package)],
)
assert _alembic_spec and _alembic_spec.loader
_alembic_module = importlib.util.module_from_spec(_alembic_spec)
sys.modules["alembic"] = _alembic_module
_alembic_spec.loader.exec_module(_alembic_module)

import sqlalchemy as sa
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.models.workspace import Workspace


def _template_payload(**updates):
    payload = {
        "title": "Meeting notes",
        "purpose": "Capture a meeting",
        "semantic_trigger": "meeting notes",
        "output_schema": {"type": "object", "properties": {}},
        "category": "work",
        "use_cases": ["meeting", "planning"],
        "routing_description": "Use for meeting transcripts",
    }
    payload.update(updates)
    return payload


async def _second_user(client):
    email = f"second-{uuid.uuid4().hex}@example.com"
    response = await client.post("/api/v1/auth/register", json={
        "name": "Second user", "email": email, "password": "senha-forte-123",
    })
    assert response.status_code == 201, response.text
    response = await client.post(
        "/api/v1/auth/login", data={"username": email, "password": "senha-forte-123"},
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


async def test_template_metadata_and_version_increment_only_on_real_patch(client, auth_headers):
    headers, _user_id = auth_headers
    response = await client.post("/api/v1/templates", headers=headers, json=_template_payload())
    assert response.status_code == 201, response.text
    template = response.json()
    assert template["version"] == 1
    assert template["category"] == "work"
    assert template["use_cases"] == ["meeting", "planning"]
    assert template["routing_description"] == "Use for meeting transcripts"

    unchanged = await client.patch(
        f"/api/v1/templates/{template['id']}", headers=headers,
        json={"category": "work", "use_cases": ["meeting", "planning"]},
    )
    assert unchanged.status_code == 200, unchanged.text
    assert unchanged.json()["version"] == 1

    changed = await client.patch(
        f"/api/v1/templates/{template['id']}", headers=headers,
        json={"routing_description": "Use for reviewed meeting transcripts"},
    )
    assert changed.status_code == 200, changed.text
    assert changed.json()["version"] == 2
    assert changed.json()["routing_description"] == "Use for reviewed meeting transcripts"


async def test_routing_rule_crud_is_scoped_to_current_user_and_owned_template(client, auth_headers):
    user_headers, _user_id = auth_headers
    template_response = await client.post(
        "/api/v1/templates", headers=user_headers, json=_template_payload(),
    )
    assert template_response.status_code == 201
    template_id = template_response.json()["id"]

    created = await client.post(
        f"/api/v1/templates/{template_id}/routing-rules",
        headers=user_headers,
        json={"context": "meeting", "capture_mode": "intentional", "priority": 50},
    )
    assert created.status_code == 201, created.text
    rule = created.json()
    assert rule["template_id"] == template_id
    assert rule["context"] == "meeting"
    assert "user_id" not in rule and "workspace_id" not in rule

    listed = await client.get(
        f"/api/v1/templates/{template_id}/routing-rules", headers=user_headers,
    )
    assert listed.status_code == 200
    assert [entry["id"] for entry in listed.json()] == [rule["id"]]

    updated = await client.patch(
        f"/api/v1/templates/{template_id}/routing-rules/{rule['id']}",
        headers=user_headers, json={"enabled": False, "priority": 10},
    )
    assert updated.status_code == 200
    assert updated.json()["enabled"] is False
    assert updated.json()["priority"] == 10

    other_headers = await _second_user(client)
    other_list = await client.get(
        f"/api/v1/templates/{template_id}/routing-rules", headers=other_headers,
    )
    assert other_list.status_code == 404
    other_patch = await client.patch(
        f"/api/v1/templates/{template_id}/routing-rules/{rule['id']}",
        headers=other_headers, json={"enabled": True},
    )
    assert other_patch.status_code == 404
    other_template = await client.post(
        f"/api/v1/templates/{template_id}/routing-rules",
        headers=other_headers, json={"priority": 1},
    )
    assert other_template.status_code == 404

    deleted = await client.delete(
        f"/api/v1/templates/{template_id}/routing-rules/{rule['id']}", headers=user_headers,
    )
    assert deleted.status_code == 204
    assert (await client.get(
        f"/api/v1/templates/{template_id}/routing-rules", headers=user_headers,
    )).json() == []


async def test_routing_rule_rejects_client_workspace_and_invalid_values(client, auth_headers):
    headers, _user_id = auth_headers
    template = (await client.post(
        "/api/v1/templates", headers=headers, json=_template_payload(),
    )).json()
    path = f"/api/v1/templates/{template['id']}/routing-rules"
    forged = await client.post(path, headers=headers, json={
        "workspace_id": str(uuid.uuid4()), "priority": 10,
    })
    assert forged.status_code == 422
    bad_priority = await client.post(path, headers=headers, json={"priority": 1001})
    assert bad_priority.status_code == 422
    bad_context = await client.post(path, headers=headers, json={"context": "x" * 121})
    assert bad_context.status_code == 422


def test_workspace_iana_timezone_defaults_and_validation():
    workspace = Workspace(name="Timezone test")
    assert workspace.timezone == "UTC"
    assert workspace.locale == "pt-BR"
    assert Workspace(name="Local", timezone="America/Sao_Paulo").timezone == "America/Sao_Paulo"
    with pytest.raises(ValueError, match="identificador IANA"):
        Workspace(name="Invalid", timezone="Mars/Olympus")


def test_migration_backfills_metadata_and_downgrade_preserves_templates():
    migration_path = Path(__file__).parents[1] / "alembic/versions/c3a91f2d7e40_template_routing_and_workspace_locale.py"
    spec = importlib.util.spec_from_file_location("template_routing_migration", migration_path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    users = sa.Table("users", metadata, sa.Column("id", sa.Uuid(), primary_key=True))
    workspaces = sa.Table(
        "workspaces", metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("is_personal", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    templates = sa.Table(
        "templates", metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("title", sa.String(200), nullable=False),
    )
    workspace_id, user_id, template_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    with engine.begin() as connection:
        metadata.create_all(connection)
        connection.execute(sa.insert(users).values(id=user_id))
        connection.execute(sa.insert(workspaces).values(
            id=workspace_id, name="Legacy", is_personal=True,
            created_at=datetime.now(timezone.utc),
        ))
        connection.execute(sa.insert(templates).values(
            id=template_id, user_id=user_id, title="Legacy template",
        ))
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            migrated_template = connection.execute(sa.text(
                "SELECT version, use_cases FROM templates WHERE id = :id"
            ), {"id": template_id.hex}).one()
            assert migrated_template.version == 1
            assert migrated_template.use_cases == "[]"
            locale = connection.execute(sa.text(
                "SELECT timezone, locale FROM workspaces WHERE id = :id"
            ), {"id": workspace_id.hex}).one()
            assert (locale.timezone, locale.locale) == ("UTC", "pt-BR")
            assert "template_routing_rules" in sa.inspect(connection).get_table_names()
            migration.downgrade()
        names = set(sa.inspect(connection).get_table_names())
        assert connection.scalar(sa.select(sa.func.count()).select_from(templates)) == 1
        assert connection.scalar(sa.select(templates.c.title)) == "Legacy template"
    assert "template_routing_rules" not in names
