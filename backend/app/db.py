from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def init_engine(database_url: str) -> Engine:
    """Create the engine once per process. SQLite is the zero-setup default;
    PostgreSQL is used in docker-compose via LOOPDESK_DATABASE_URL."""
    global _engine, _SessionLocal
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    _engine = create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)
    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("init_engine() has not been called")
    return _engine


def new_session() -> Session:
    if _SessionLocal is None:
        raise RuntimeError("init_engine() has not been called")
    return _SessionLocal()


def get_db() -> Iterator[Session]:
    db = new_session()
    try:
        yield db
    finally:
        db.close()


def create_all() -> None:
    from . import models  # noqa: F401  (register tables)

    Base.metadata.create_all(get_engine())


BACKEND_DIR = Path(__file__).resolve().parent.parent


def migrate(database_url: str) -> str:
    """Bring the schema to the latest Alembic revision.

    A database created by an older version with ``create_all()`` has tables but no
    ``alembic_version`` row; it is completed (missing tables created) and stamped as
    current instead of being migrated, which would fail on "table already exists".
    """
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import inspect

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    tables = set(inspect(get_engine()).get_table_names())
    if tables and "alembic_version" not in tables:
        create_all()
        command.stamp(config, "head")
        return "stamped"
    command.upgrade(config, "head")
    return "upgraded"


def prepare_database(database_url: str, auto_migrate: bool = True) -> str:
    """Production path: Alembic migrations. Tests and throwaway eval DBs: create_all."""
    if auto_migrate:
        return migrate(database_url)
    create_all()
    return "create_all"
