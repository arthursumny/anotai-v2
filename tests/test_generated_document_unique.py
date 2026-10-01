"""Workspace/note idempotency coverage for generated documents."""

import importlib.metadata
import importlib.util
import sys
import uuid
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
from sqlalchemy.exc import IntegrityError
from sqlmodel import select

from app.models.generated_document import GeneratedDocument
from app.models.user import User
from app.models.workspace import Workspace


async def test_workspace_note_pair_is_unique_and_nullable_note_can_repeat(db_session):
    workspace = Workspace(name="Documents")
    db_session.add(workspace)
    await db_session.flush()
    user = User(
        name="Owner", email=f"{uuid.uuid4()}@example.com", hashed_password="unused",
        workspace_id=workspace.id,
    )
    db_session.add(user)
    await db_session.flush()

    def make_document(note_id):
        return GeneratedDocument(
            workspace_id=workspace.id,
            user_id=user.id,
            note_id=note_id,
            template_version="1",
            structured_data={"ok": True},
            rendered_content="rendered",
        )

    note_id = uuid.uuid4()
    db_session.add(make_document(note_id))
    await db_session.flush()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(make_document(note_id))
            await db_session.flush()

    db_session.add_all([make_document(None), make_document(None)])
    await db_session.flush()
    assert len((await db_session.exec(select(GeneratedDocument))).all()) == 3


def test_unique_constraint_migration_is_reversible_and_preserves_rows():
    path = Path(__file__).parents[1] / "alembic/versions/ae52d0193bc7_unique_generated_document_note.py"
    spec = importlib.util.spec_from_file_location("generated_document_unique_migration", path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = sa.create_engine("sqlite:///:memory:")
    documents = sa.Table(
        "generated_documents", sa.MetaData(),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("note_id", sa.Uuid(), nullable=True),
        sa.Column("template_version", sa.String(80), nullable=False),
    )
    workspace_id, note_id = uuid.uuid4(), uuid.uuid4()
    with engine.begin() as connection:
        documents.create(connection)
        connection.execute(sa.insert(documents), [
            {"id": uuid.uuid4(), "workspace_id": workspace_id, "note_id": note_id,
             "template_version": "v7"},
            {"id": uuid.uuid4(), "workspace_id": workspace_id, "note_id": None,
             "template_version": "legacy-null-1"},
            {"id": uuid.uuid4(), "workspace_id": workspace_id, "note_id": None,
             "template_version": "legacy-null-2"},
        ])
        with Operations.context(MigrationContext.configure(connection)):
            migration.upgrade()
            unique_constraints = sa.inspect(connection).get_unique_constraints("generated_documents")
            assert any(
                constraint["name"] == "uq_generated_documents_workspace_note"
                and constraint["column_names"] == ["workspace_id", "note_id"]
                for constraint in unique_constraints
            )
            note_column = next(
                column for column in sa.inspect(connection).get_columns("generated_documents")
                if column["name"] == "note_id"
            )
            assert note_column["nullable"] is True
            assert connection.scalar(sa.select(documents.c.template_version).where(
                documents.c.note_id == note_id,
            )) == "v7"
            with pytest.raises(IntegrityError):
                with connection.begin_nested():
                    connection.execute(sa.insert(documents).values(
                        id=uuid.uuid4(), workspace_id=workspace_id, note_id=note_id,
                        template_version="duplicate",
                    ))
            assert connection.scalar(sa.select(sa.func.count()).select_from(documents)) == 3
            migration.downgrade()
            connection.execute(sa.insert(documents).values(
                id=uuid.uuid4(), workspace_id=workspace_id, note_id=note_id,
                template_version="after-downgrade",
            ))
        assert connection.scalar(sa.select(sa.func.count()).select_from(documents)) == 4
