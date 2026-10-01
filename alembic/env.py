"""AnotAI — Alembic env (usa SYNC_DATABASE_URL, driver psycopg2)."""

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool
from sqlmodel import SQLModel

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Importa os models para registrar o metadata (não remover)
from app.core.config import settings  # noqa: E402
from app.models import execution_log as _el  # noqa: E402,F401
from app.models import note as _note  # noqa: E402,F401
from app.models import mcp_oauth as _mcp_oauth  # noqa: E402,F401
from app.models import mcp_server as _mcp_server  # noqa: E402,F401
from app.models import template as _tpl  # noqa: E402,F401
from app.models import user as _user  # noqa: E402,F401

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

config.set_main_option("sqlalchemy.url", settings.SYNC_DATABASE_URL)
target_metadata = SQLModel.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
