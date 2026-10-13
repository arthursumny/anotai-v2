"""Readable notes and a single native Task board; retire forced audio routing."""

import json
import uuid

import sqlalchemy as sa
from alembic import op

revision = "e7c8a2d4f610"
down_revision = "9c2e7f4a1b63"
branch_labels = None
depends_on = None


def upgrade() -> None:
    connection = op.get_bind()
    with op.batch_alter_table("notes") as batch:
        batch.add_column(sa.Column("body", sa.Text(), nullable=False, server_default=""))
        batch.add_column(sa.Column("summary", sa.Text(), nullable=False, server_default=""))
    with op.batch_alter_table("tasks") as batch:
        batch.add_column(sa.Column("kanban_status", sa.String(20), nullable=False, server_default="todo"))
        batch.add_column(sa.Column("source_note_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key("fk_tasks_source_note", "notes", ["source_note_id"], ["id"], ondelete="SET NULL")
        batch.create_index("ix_tasks_source_note_id", ["source_note_id"])

    metadata = sa.MetaData()
    notes = sa.Table("notes", metadata, autoload_with=connection)
    tasks = sa.Table("tasks", metadata, autoload_with=connection)
    users = sa.Table("users", metadata, autoload_with=connection)
    templates = sa.Table("templates", metadata, autoload_with=connection)
    workspaces = sa.Table("workspaces", metadata, autoload_with=connection)
    memberships = sa.Table("workspace_memberships", metadata, autoload_with=connection)
    # SQLite reflection sees UUID storage as CHAR; retain UUID bind/result semantics.
    for table in (notes, tasks, users, templates, workspaces, memberships):
        for name in ("id", "user_id", "workspace_id", "template_id", "source_event_id", "source_note_id"):
            if name in table.c:
                table.c[name].type = sa.Uuid()
    # Older accounts must enter the same scoped document flow, not a special branch.
    for user in connection.execute(sa.select(users)).mappings():
        if user["workspace_id"] is not None:
            continue
        workspace_id = uuid.uuid4()
        connection.execute(workspaces.insert().values(
            id=workspace_id, name="Pessoal", is_personal=True, timezone="America/Sao_Paulo",
            locale="pt-BR", created_at=user["created_at"],
        ))
        connection.execute(memberships.insert().values(
            id=uuid.uuid4(), workspace_id=workspace_id, user_id=user["id"], role="OWNER",
            created_at=user["created_at"],
        ))
        connection.execute(users.update().where(users.c.id == user["id"]).values(workspace_id=workspace_id))
    scope = {row.id: row.workspace_id for row in connection.execute(sa.select(users.c.id, users.c.workspace_id))}
    note_ids = set(connection.execute(sa.select(notes.c.id)).scalars())
    for note in connection.execute(sa.select(notes)).mappings():
        data = note["structured_data"] or {}
        body = data.get("nota", "") if isinstance(data, dict) else ""
        if not body and data:
            # Historical conversion only: no runtime substitute for failed LLM generation.
            body = "\n\n".join(
                f"## {key}\n\n{value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)}"
                for key, value in data.items() if key != "kanban_task_ids"
            )
        summary = data.get("resumo", "") if isinstance(data, dict) else ""
        values = {"body": body, "summary": summary if isinstance(summary, str) else ""}
        if note["status"] == "no_match":
            values["status"] = "pending_review"
        if note["capture_mode"] == "continuous":
            values["capture_mode"] = "intentional"
        column = note["kanban_status"]
        suggested = note["status"] == "suggested" and note["capture_mode"] == "continuous"
        if column is not None or suggested:
            source = data.get("source_note_id") or data.get("origem_nota_geral") if isinstance(data, dict) else None
            try:
                source_id = uuid.UUID(str(source)) if source else note["id"]
            except ValueError:
                source_id = note["id"]
            if source_id not in note_ids:
                source_id = note["id"]
            original_task_id = data.get("origin_task_id") if isinstance(data, dict) else None
            original_task = None
            if original_task_id:
                try:
                    original_task = connection.execute(sa.select(tasks).where(
                        tasks.c.id == uuid.UUID(str(original_task_id)), tasks.c.user_id == note["user_id"],
                    )).mappings().first()
                except ValueError:
                    original_task = None
            status = "suggested" if suggested else {
                "todo": "open", "in_progress": "in_progress", "done": "done",
            }.get(column, "open")
            task_values = {
                "source_note_id": source_id, "kanban_status": column or "todo",
                "status": status, "due_at": note["due_date"], "remind_at": note["reminder_at"],
            }
            if original_task is not None:
                connection.execute(tasks.update().where(tasks.c.id == original_task["id"]).values(**task_values))
            else:
                connection.execute(tasks.insert().values(
                    id=note["id"], workspace_id=scope[note["user_id"]], user_id=note["user_id"],
                    idempotency_key=f"migrated-note:{note['id']}", title=note["title"] or "Tarefa",
                    summary=body or note["raw_transcription"], timezone="America/Sao_Paulo",
                    created_at=note["created_at"], updated_at=note["created_at"], **task_values,
                ))
            values["kanban_status"] = None
            values["reminder_at"] = None
            if suggested or original_task is not None or (
                isinstance(data, dict) and ("task_index" in data or "origem_nota_geral" in data)
            ):
                values["status"] = "discarded"
        connection.execute(notes.update().where(notes.c.id == note["id"]).values(**values))
    # Accepted suggestions previously pointed to a redundant Note card.
    connection.execute(tasks.update().where(tasks.c.status == "consumed").values(status="open"))
    for template in connection.execute(sa.select(templates)).mappings():
        actions = [
            {**action, "enabled": action.get("enabled", True),
             "require_confirmation": action.get("require_confirmation", False)}
            for action in (template["actions"] or []) if action.get("type") != "whatsapp"
        ]
        connection.execute(templates.update().where(templates.c.id == template["id"]).values(actions=actions))
    op.drop_table("template_routing_rules")


def downgrade() -> None:
    op.create_table(
        "template_routing_rules",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id"), nullable=False),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("template_id", sa.Uuid(), sa.ForeignKey("templates.id"), nullable=False),
        sa.Column("context", sa.String(120)),
        sa.Column("capture_mode", sa.String(20)),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    with op.batch_alter_table("tasks") as batch:
        batch.drop_index("ix_tasks_source_note_id")
        batch.drop_constraint("fk_tasks_source_note", type_="foreignkey")
        batch.drop_column("source_note_id")
        batch.drop_column("kanban_status")
    with op.batch_alter_table("notes") as batch:
        batch.drop_column("summary")
        batch.drop_column("body")
