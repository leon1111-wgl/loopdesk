import os

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect

from app import models  # noqa: F401
from app.db import Base, create_all, get_engine, init_engine, migrate
from tests.conftest import _reset_database


def _url(tmp_path, name):
    return os.environ.get("LOOPDESK_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / name}"


def test_migrations_produce_exactly_the_model_schema(tmp_path):
    url = _url(tmp_path, "migrated.db")
    _reset_database(url)
    init_engine(url)
    assert migrate(url) == "upgraded"
    with get_engine().connect() as conn:
        diff = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diff == [], f"models and migrations disagree; run `alembic revision --autogenerate`: {diff}"
    assert migrate(url) == "upgraded", "running migrations again is a no-op"


def test_legacy_create_all_database_is_completed_and_stamped(tmp_path):
    url = _url(tmp_path, "legacy.db")
    _reset_database(url)
    init_engine(url)
    create_all()
    with get_engine().begin() as conn:  # simulate a database from before the answer cache existed
        conn.exec_driver_sql("DROP TABLE answer_cache")
    assert migrate(url) == "stamped"
    tables = set(inspect(get_engine()).get_table_names())
    assert {"answer_cache", "alembic_version"} <= tables
    assert migrate(url) == "upgraded"
