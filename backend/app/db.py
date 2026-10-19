from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker


class Base(DeclarativeBase):
    pass


_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _sqlite_pragmas(conn, _record) -> None:
    conn.execute("PRAGMA foreign_keys=ON")  # same integrity errors as PostgreSQL
    if conn.execute("PRAGMA database_list").fetchone()[2]:  # file database, not :memory:
        # WAL lets readers proceed while one writer commits; with busy_timeout, concurrent
        # requests wait for the lock instead of failing with "database is locked".
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")


def init_engine(database_url: str) -> Engine:
    """Create the engine once per process. SQLite is the zero-setup default;
    PostgreSQL is used in docker-compose via LOOPDESK_DATABASE_URL."""
    global _engine, _SessionLocal
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    _engine = create_engine(database_url, connect_args=connect_args, pool_pre_ping=True)
    if database_url.startswith("sqlite"):
        # SQLite ignores foreign keys unless asked; enforce them so local runs and tests
        # catch the same integrity errors PostgreSQL would.
        event.listen(_engine, "connect", _sqlite_pragmas)
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


def head_revision() -> str:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    return ScriptDirectory.from_config(config).get_current_head()


def prepare_database(database_url: str, auto_migrate: bool = True) -> str:
    """Production path: Alembic migrations. Tests and throwaway eval DBs: create_all."""
    if auto_migrate:
        return migrate(database_url)
    create_all()
    return "create_all"
