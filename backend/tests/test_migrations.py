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


def test_upgrade_closes_duplicate_live_handoffs_before_adding_the_unique_index(tmp_path):
    from alembic import command
    from alembic.config import Config

    from app.db import BACKEND_DIR

    url = _url(tmp_path, "dupes.db")
    _reset_database(url)
    init_engine(url)
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0004")
    with get_engine().begin() as conn:  # the state the old handoff bug left behind
        conn.exec_driver_sql("INSERT INTO tenants (id, slug, name, industry, auto_refund_limit, ticket_sla_hours, "
                             "return_window_days, quality_window_days, created_at) "
                             "VALUES (1, 't', 'T', 'x', 50, 24, 7, 15, CURRENT_TIMESTAMP)")
        conn.exec_driver_sql("INSERT INTO users (id, tenant_id, email, name, role, password_hash, created_at) "
                             "VALUES (1, 1, 'c@t', 'C', 'customer', 'x', CURRENT_TIMESTAMP)")
        conn.exec_driver_sql("INSERT INTO conversations (id, tenant_id, user_id, title, channel, status, created_at, "
                             "updated_at) VALUES (1, 1, 1, 't', 'web', 'handoff', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
        for ticket_id in (1, 2, 3):
            conn.exec_driver_sql("INSERT INTO tickets (id, tenant_id, conversation_id, customer_id, kind, title, "
                                 f"description, priority, status, created_at) VALUES ({ticket_id}, 1, 1, 1, 'handoff', "
                                 "'转人工', '', 'high', 'open', CURRENT_TIMESTAMP)")
    command.upgrade(config, "head")
    with get_engine().connect() as conn:
        rows = conn.exec_driver_sql("SELECT id, status FROM tickets ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [(1, "open"), (2, "closed"), (3, "closed")], "the oldest ticket keeps its place"
