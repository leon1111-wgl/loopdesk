"""Alembic environment.

URL resolution: an explicit ``sqlalchemy.url`` set by the caller (app/db.py ``migrate``)
wins, then LOOPDESK_DATABASE_URL, then the development default. SQLite needs
``render_as_batch`` because it cannot ALTER most column properties in place.
"""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from app import models  # noqa: F401  (register every table on Base.metadata)
from app.db import Base

config = context.config
url = config.get_main_option("sqlalchemy.url") or os.environ.get("LOOPDESK_DATABASE_URL", "sqlite:///./loopdesk.db")
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True,
                      render_as_batch=url.startswith("sqlite"), compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config({"sqlalchemy.url": url}, prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          render_as_batch=connection.dialect.name == "sqlite", compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
