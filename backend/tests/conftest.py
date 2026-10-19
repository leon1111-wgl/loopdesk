import json
import os

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.rag.retrieval import index_manager
from app.security import rate_limiter
from app.seed import DEMO_PASSWORD


@pytest.fixture()
def settings(tmp_path):
    s = Settings()
    # CI also runs the suite against PostgreSQL by setting LOOPDESK_TEST_DATABASE_URL.
    s.database_url = os.environ.get("LOOPDESK_TEST_DATABASE_URL") or f"sqlite:///{tmp_path / 'test.db'}"
    s.jwt_secret = "test-secret-with-at-least-32-bytes!!"
    s.rate_limit_per_minute = 1000
    s.seed_demo_traffic = False
    s.auto_migrate = False  # fast schema setup; migrations have their own tests
    s.log_format = "text"
    return s


def _reset_database(url: str) -> None:
    if url.startswith("sqlite"):
        return  # a fresh file per test already
    from sqlalchemy import create_engine

    from app import models  # noqa: F401
    from app.db import Base

    from sqlalchemy import text

    engine = create_engine(url)
    Base.metadata.drop_all(engine)
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS alembic_version"))
    engine.dispose()


@pytest.fixture()
def client(settings):
    _reset_database(settings.database_url)
    index_manager._cache.clear()
    rate_limiter._hits.clear()
    app = create_app(settings)
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def login(client):
    def _login(email: str) -> dict:
        r = client.post("/api/auth/login", json={"email": email, "password": DEMO_PASSWORD})
        assert r.status_code == 200, r.text
        return {"Authorization": f"Bearer {r.json()['token']}"}

    return _login


def sse_events(response) -> list[dict]:
    return [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]


@pytest.fixture()
def ask(client):
    """Send one message through the streaming endpoint and return the parsed events."""

    def _ask(headers: dict, text: str, conversation_id: int | None = None, **options) -> tuple[int, list[dict]]:
        if conversation_id is None:
            conversation_id = client.post("/api/conversations", json={}, headers=headers).json()["id"]
        r = client.post(f"/api/conversations/{conversation_id}/stream",
                        json={"content": text, "options": options}, headers=headers)
        assert r.status_code == 200, r.text
        return conversation_id, sse_events(r)

    return _ask


def by_type(events: list[dict], kind: str) -> list[dict]:
    return [e for e in events if e["type"] == kind]
