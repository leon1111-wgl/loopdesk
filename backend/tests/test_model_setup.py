"""Connecting a real model (review round 4, N-032). Demo traffic, on by default, called the model
synchronously inside create_app: with a wrong key the server took about 100 s to start, with a
hanging endpoint it had not started after 6 minutes, and /api/health was unreachable meanwhile."""

import threading
import time

import httpx

from app.config import Settings


def test_demo_traffic_is_on_by_default_only_for_the_offline_model(monkeypatch):
    monkeypatch.delenv("LOOPDESK_SEED_DEMO_TRAFFIC", raising=False)
    monkeypatch.setenv("LOOPDESK_LLM_PROVIDER", "openai_compat")
    assert Settings.from_env().seed_demo_traffic is False
    monkeypatch.setenv("LOOPDESK_LLM_PROVIDER", "mock")
    assert Settings.from_env().seed_demo_traffic is True
    monkeypatch.setenv("LOOPDESK_LLM_PROVIDER", "openai_compat")
    monkeypatch.setenv("LOOPDESK_SEED_DEMO_TRAFFIC", "1")
    assert Settings.from_env().seed_demo_traffic is True, "still there when asked for"


def test_demo_traffic_against_a_real_model_does_not_hold_up_startup(settings, monkeypatch):
    from app import main
    from tests.conftest import _reset_database

    # Demo traffic only follows a first seeding. On PostgreSQL the test database outlives each test
    # (SQLite gets a new file), so start from an empty one, as the client fixture does.
    _reset_database(settings.database_url)
    started, release = threading.Event(), threading.Event()

    def hanging_model(db, services):
        started.set()
        release.wait(10)

    monkeypatch.setattr(main, "seed_demo_traffic", hanging_model)
    settings.seed_demo_traffic = True
    settings.llm_provider, settings.llm_base_url, settings.llm_api_key = "openai_compat", "http://127.0.0.1:9/v1", "x"
    began = time.monotonic()
    try:
        main.create_app(settings)
        assert time.monotonic() - began < 5, "the server starts at once"
        assert started.wait(5), "the demo traffic still runs, in the background"
    finally:
        release.set()


def _provider(status: int, body: dict):
    from app.llm.openai_compat import OpenAICompatProvider

    transport = httpx.MockTransport(lambda request: httpx.Response(status, json=body))
    return OpenAICompatProvider("http://model.test/v1", "key", max_retries=0, transport=transport)


def test_the_model_check_says_what_to_fix():
    from app.ops.llm_check import check

    settings = Settings()
    settings.llm_model_fast = settings.llm_model_strong = "hunyuan-lite"
    [result] = check(settings, _provider(401, {"error": {"message": "invalid api key"}}))
    assert result["ok"] is False and result["kind"] == "rejected" and "密钥" in result["hint"]
    [result] = check(settings, _provider(503, {"error": {"message": "overloaded"}}))
    assert result["kind"] == "unavailable"
    assert all(r["ok"] for r in check(Settings())), "the offline model passes"
