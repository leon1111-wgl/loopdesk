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


def test_upgrade_detaches_duplicate_channel_conversations_before_adding_the_unique_index(tmp_path):
    from alembic import command
    from alembic.config import Config

    from app.db import BACKEND_DIR

    url = _url(tmp_path, "chat-dupes.db")
    _reset_database(url)
    init_engine(url)
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0010")
    with get_engine().begin() as conn:  # what two concurrent first messages of one Feishu chat left behind
        conn.exec_driver_sql("INSERT INTO tenants (id, slug, name, industry, auto_refund_limit, ticket_sla_hours, "
                             "return_window_days, quality_window_days, created_at) "
                             "VALUES (1, 't', 'T', 'x', 50, 24, 7, 15, CURRENT_TIMESTAMP)")
        conn.exec_driver_sql("INSERT INTO users (id, tenant_id, email, name, role, password_hash, created_at) "
                             "VALUES (1, 1, 'f@t.channel', 'F', 'customer', 'x', CURRENT_TIMESTAMP)")
        for conversation_id in (1, 2):
            conn.exec_driver_sql("INSERT INTO conversations (id, tenant_id, user_id, title, channel, external_id, status, "
                                 f"created_at, updated_at) VALUES ({conversation_id}, 1, 1, 'c', 'feishu', 'oc_1', 'bot', "
                                 "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
    command.upgrade(config, "head")
    with get_engine().connect() as conn:
        rows = conn.exec_driver_sql("SELECT id, external_id FROM conversations ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [(1, "oc_1"), (2, None)], "the oldest keeps the chat, the duplicate its history"


def test_upgrade_turns_refunds_into_a_ledger_that_needs_no_local_order(tmp_path):
    # N-028: a refund can be for an order that only the merchant's own system knows, so the ledger
    # carries its customer instead of reaching it through the demo orders table.
    import pytest
    from alembic import command
    from alembic.config import Config
    from sqlalchemy.exc import IntegrityError

    from app.db import BACKEND_DIR

    url = _url(tmp_path, "ledger.db")
    _reset_database(url)
    init_engine(url)
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0020")
    with get_engine().begin() as conn:  # a refund made by an earlier version
        conn.exec_driver_sql("INSERT INTO tenants (id, slug, name, industry, auto_refund_limit, ticket_sla_hours, "
                             "return_window_days, quality_window_days, created_at) "
                             "VALUES (1, 't', 'T', 'x', 50, 24, 7, 15, CURRENT_TIMESTAMP)")
        conn.exec_driver_sql("INSERT INTO users (id, tenant_id, email, name, role, password_hash, created_at) "
                             "VALUES (7, 1, 'c@t', 'C', 'customer', 'x', CURRENT_TIMESTAMP)")
        conn.exec_driver_sql("INSERT INTO orders (id, tenant_id, customer_id, items, amount, status, region, paid_at, "
                             "shipment) VALUES ('SO1', 1, 7, '[]', 10, 'refunding', 'domestic', CURRENT_TIMESTAMP, '{}')")
        # No explicit id: on PostgreSQL that would leave the sequence behind for the inserts below.
        conn.exec_driver_sql("INSERT INTO refunds (tenant_id, order_id, amount, reason, status, created_at) "
                             "VALUES (1, 'SO1', 10, '', 'processing', CURRENT_TIMESTAMP)")
    command.upgrade(config, "head")

    def refund(order_id: str, key: str | None, status: str) -> None:
        with get_engine().begin() as conn:
            conn.exec_driver_sql(
                "INSERT INTO refunds (tenant_id, order_id, customer_id, amount, reason, status, idempotency_key, "
                f"error, created_at) VALUES (1, '{order_id}', 7, 5, '', '{status}', "
                f"{'NULL' if key is None else repr(key)}, '', CURRENT_TIMESTAMP)")

    with get_engine().connect() as conn:
        assert tuple(conn.exec_driver_sql("SELECT customer_id, idempotency_key FROM refunds WHERE order_id = 'SO1'")
                     .one()) == (7, None), "the customer comes from the order"
    refund("SO-REMOTE", "k-1", "pending")  # no row in orders: the merchant's system has it
    with pytest.raises(IntegrityError):
        refund("SO-REMOTE", "k-2", "pending")  # one live refund per order
    refund("SO-REMOTE", "k-3", "failed")  # a refused one does not hold the order
    refund("SO1", None, "processing")  # rows from before the ledger keep their history, outside the rule


def test_downgrading_below_the_ledger_is_refused_while_it_holds_outside_orders(tmp_path):
    import pytest
    from alembic import command
    from alembic.config import Config

    from app.db import BACKEND_DIR

    url = _url(tmp_path, "ledger-down.db")
    _reset_database(url)
    init_engine(url)
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    with get_engine().begin() as conn:
        conn.exec_driver_sql("INSERT INTO tenants (id, slug, name, industry, auto_refund_limit, ticket_sla_hours, "
                             "return_window_days, quality_window_days, created_at) "
                             "VALUES (1, 't', 'T', 'x', 50, 24, 7, 15, CURRENT_TIMESTAMP)")
        conn.exec_driver_sql("INSERT INTO users (id, tenant_id, email, name, role, password_hash, created_at) "
                             "VALUES (7, 1, 'c@t', 'C', 'customer', 'x', CURRENT_TIMESTAMP)")
        conn.exec_driver_sql("INSERT INTO refunds (tenant_id, order_id, customer_id, amount, reason, status, "
                             "idempotency_key, error, attempts, created_at) VALUES (1, 'SO-REMOTE', 7, 5, '', "
                             "'processing', 'k-1', '', 1, CURRENT_TIMESTAMP)")
    with pytest.raises(RuntimeError, match="outside the orders table"):
        command.downgrade(config, "0020")
