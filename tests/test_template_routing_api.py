"""Template metadata/versioning, workspace locale, and migration coverage.

The deterministic routing rules this file used to cover were removed: model
selection is now done by the OpenAI router in ``app/services/router.py``, and
``/api/v1/templates/{id}/routing-rules`` no longer exists.
"""

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
