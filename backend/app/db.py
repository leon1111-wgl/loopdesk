from __future__ import annotations

from collections.abc import Iterator

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
