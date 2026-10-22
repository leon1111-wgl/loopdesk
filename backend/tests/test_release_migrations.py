"""Migrations run by a release job (Kubernetes Job, W7), not by every replica.

LOOPDESK_AUTO_MIGRATE=0 is the documented setting for several replicas. It used to make each replica
run create_all() instead: after an upgrade that adds a table, the replicas created it first, without
the rest of the migration, and the migration job then failed on "table already exists". Now a replica
started that way leaves the schema alone and is not ready until the job has brought it to the head."""

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect

from app.config import Settings
from app.db import init_engine, migrate
from app.main import create_app
from app.ops import schema_wait


def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_auto_migrate_0_means_a_release_job_migrates(monkeypatch):
    monkeypatch.setenv("LOOPDESK_AUTO_MIGRATE", "0")
    s = Settings.from_env()
    assert (s.auto_migrate, s.external_migrations) == (False, True)
    monkeypatch.delenv("LOOPDESK_AUTO_MIGRATE")
    assert Settings.from_env().external_migrations is False


def test_a_replica_leaves_the_schema_to_the_job_and_waits_for_it(settings, tmp_path):
    url = f"sqlite:///{tmp_path / 'release.db'}"
    settings.database_url, settings.seed_demo, settings.audit_key = url, False, "a-separate-audit-key-of-at-least-32-bytes"
    settings.auto_migrate, settings.external_migrations = False, True
    with TestClient(create_app(settings)) as client:
        assert _tables(url) == set(), "no create_all(): the job's CREATE TABLE must not collide with it"
        r = client.get("/api/ready")
        assert r.status_code == 503 and r.json()["checks"]["migrations"] != "ok"
        assert schema_wait.main(["--timeout", "1", "--interval", "0.2"]) == 1

        migrate(url)  # what the migration Job runs
        assert client.get("/api/ready").status_code == 200
        assert schema_wait.main(["--timeout", "1"]) == 0


def test_the_wait_command_reads_the_configured_database(monkeypatch, tmp_path):
    url = f"sqlite:///{tmp_path / 'job.db'}"
    init_engine(url)
    migrate(url)
    monkeypatch.setenv("LOOPDESK_DATABASE_URL", url)
    assert schema_wait.main(["--timeout", "1"]) == 0
