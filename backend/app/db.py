from __future__ import annotations

import functools
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


def current_revision() -> str | None:
    """The revision the database is at, or None before the first migration."""
    from sqlalchemy import inspect, text

    engine = get_engine()
    if "alembic_version" not in inspect(engine).get_table_names():
        return None
    with engine.connect() as conn:
        return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


@functools.lru_cache(maxsize=1)
def _revisions() -> tuple[str, frozenset[str]]:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    script = ScriptDirectory.from_config(config)
    return script.get_current_head(), frozenset(r.revision for r in script.walk_revisions())


def head_revision() -> str:
    return _revisions()[0]


def schema_status(current: str | None) -> str:
    """"ok" when this version's code can use a database at ``current``: at this version's latest
    migration, or at one this version does not know, which a newer release's migration job wrote
    (a rolling update: the old replicas keep serving while the new ones start; migrations stay
    backward compatible for one release). Otherwise what is wrong: not migrated yet, or behind."""
    head, known = _revisions()
    if current == head:
        return "ok"
    if current is not None and current not in known:
        # Newer only if this version's tables are all there (a newer release adds, it does not drop):
        # another application's database, or a garbage version row, must not pass.
        from sqlalchemy import inspect

        from . import models  # noqa: F401  (register tables)

        if set(Base.metadata.tables) <= set(inspect(get_engine()).get_table_names()):
            return "ok"
        return f"at {current}, unknown to this version, and its tables are missing"
    return f"at {current}, expected {head}"


def prepare_database(database_url: str, auto_migrate: bool = True, external: bool = False) -> str:
    """Production path: Alembic migrations, at startup or by a release job (``external``: the schema
    is left alone; create_all here would collide with the job's CREATE TABLE). Tests and throwaway
    eval DBs: create_all."""
    if external:
        return "external"
    if auto_migrate:
        return migrate(database_url)
    create_all()
    return "create_all"
