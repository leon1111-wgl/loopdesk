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


def _set_revision(url: str, revision: str) -> None:
    from sqlalchemy import text

    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": revision})
    engine.dispose()


def _release_settings(settings, url):
    settings.database_url, settings.seed_demo, settings.audit_key = url, False, "a-separate-audit-key-of-at-least-32-bytes"
    settings.auto_migrate, settings.external_migrations = False, True
    return settings


def test_old_replicas_stay_ready_while_a_newer_release_migrates(settings, tmp_path):
    """A rolling update: the new release's Job migrates first, the old pods keep serving. They used to
    answer 503 ("at <new>, expected <old>") and the Service lost every replica (W7 review, P1)."""
    url = f"sqlite:///{tmp_path / 'rolling.db'}"
    migrate(url)
    _set_revision(url, "ffff_next_release")  # a revision this version's code does not know
    with TestClient(create_app(_release_settings(settings, url))) as client:
        assert client.get("/api/ready").status_code == 200
        assert schema_wait.main(["--timeout", "1"]) == 0, "a rollback to this image starts too"


def test_a_database_behind_this_version_is_not_ready(settings, tmp_path):
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    from app.db import BACKEND_DIR, head_revision

    url = f"sqlite:///{tmp_path / 'behind.db'}"
    migrate(url)
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    previous = ScriptDirectory.from_config(config).get_revision(head_revision()).down_revision
    _set_revision(url, previous)
    with TestClient(create_app(_release_settings(settings, url))) as client:
        r = client.get("/api/ready")
        assert r.status_code == 503 and r.json()["checks"]["migrations"] == f"at {previous}, expected {head_revision()}"
        assert schema_wait.main(["--timeout", "0.5", "--interval", "0.1"]) == 1


def test_a_seed_reset_rebuilds_the_schema_even_when_a_job_migrates(monkeypatch, tmp_path):
    """--reset drops every table; with LOOPDESK_AUTO_MIGRATE=0 nothing put them back and the seed
    failed on "no such table: tenants" (W7 review)."""
    from app import seed
    from app.db import current_revision, head_revision

    url = f"sqlite:///{tmp_path / 'reset.db'}"
    monkeypatch.setattr("app.config._settings", None)
    monkeypatch.setenv("LOOPDESK_DATABASE_URL", url)
    monkeypatch.setenv("LOOPDESK_AUTO_MIGRATE", "0")
    monkeypatch.setenv("LOOPDESK_SEED_DEMO_TRAFFIC", "0")
    seed.main(["--reset"])
    assert current_revision() == head_revision() and "tenants" in _tables(url)


def test_the_eval_gate_leaves_a_production_database_alone(monkeypatch, tmp_path):
    """Without --fresh the CLI runs against LOOPDESK_DATABASE_URL. It created tables there (colliding
    with the migration job) and seeded the demo tenants with their public password (W7 review)."""
    from app.evals import cli

    url = f"sqlite:///{tmp_path / 'prod.db'}"
    monkeypatch.setattr("app.config._settings", None)
    monkeypatch.setenv("LOOPDESK_DATABASE_URL", url)
    monkeypatch.setenv("LOOPDESK_AUTO_MIGRATE", "0")
    monkeypatch.setenv("LOOPDESK_SEED_DEMO", "0")
    assert cli.main(["--tenant", "leon"]) == 2
    assert _tables(url) == set()


def test_an_unknown_revision_without_our_tables_is_not_ready(tmp_path):
    """Unknown means "a newer release migrated" only if this version's tables are all there: a database
    of another application, or a garbage version row, must not pass (W7 re-review)."""
    from sqlalchemy import text

    from app.db import schema_status

    url = f"sqlite:///{tmp_path / 'foreign.db'}"
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text("INSERT INTO alembic_version VALUES ('zzz_some_other_app')"))
    engine.dispose()
    init_engine(url)
    assert schema_status("zzz_some_other_app").startswith("at zzz_some_other_app")
    migrate_url = f"sqlite:///{tmp_path / 'ours.db'}"
    init_engine(migrate_url)
    migrate(migrate_url)
    assert schema_status("ffff_next_release") == "ok"
