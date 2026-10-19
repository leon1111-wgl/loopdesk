import json
import logging

from fastapi.testclient import TestClient

from app.observability import JsonFormatter, request_id_var


def test_every_response_has_a_request_id_and_echoes_a_safe_one(client):
    generated = client.get("/api/health").headers["X-Request-ID"]
    assert len(generated) == 32
    assert client.get("/api/health", headers={"X-Request-ID": "trace-abc-12345"}).headers["X-Request-ID"] == "trace-abc-12345"
    injected = client.get("/api/health", headers={"X-Request-ID": "bad id\nwith newline"}).headers["X-Request-ID"]
    assert injected != "bad id\nwith newline" and len(injected) == 32, "unsafe ids are replaced, not echoed"


def test_unhandled_errors_return_the_request_id(client):
    def boom():
        raise RuntimeError("database exploded")

    from fastapi.routing import APIRoute

    client.app.router.routes.insert(0, APIRoute("/api/_boom", boom))  # before the console's catch-all route
    r = TestClient(client.app, raise_server_exceptions=False).get("/api/_boom", headers={"X-Request-ID": "report-me-0001"})
    assert r.status_code == 500
    assert r.json()["request_id"] == "report-me-0001" and "exploded" not in r.text, "no internals leak to users"


def test_readiness_probe(client):
    r = client.get("/api/ready")
    assert r.status_code == 200 and r.json()["checks"]["database"] == "ok"


def test_agent_runs_record_the_request_id(client, login):
    headers = {**login("customer@leon.demo"), "X-Request-ID": "req-from-gateway-42"}
    run_id = client.post("/api/chat", json={"content": "付款以后多久发货？"}, headers=headers).json()["run_id"]
    trace = client.get(f"/api/runs/{run_id}", headers=login("admin@leon.demo")).json()
    assert trace["request_id"] == "req-from-gateway-42"


def test_json_log_lines_carry_request_context():
    token = request_id_var.set("ctx-req-0001")
    try:
        record = logging.LogRecord("loopdesk.access", logging.INFO, __file__, 1, "request", None, None)
        record.status, record.path = 200, "/api/health"
        line = json.loads(JsonFormatter().format(record))
    finally:
        request_id_var.reset(token)
    assert line["request_id"] == "ctx-req-0001" and line["status"] == 200 and line["path"] == "/api/health"


def test_unknown_api_paths_are_json_404s(client):
    r = client.get("/api/does-not-exist")
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")


def test_streaming_runs_also_record_the_request_id(client, login):
    from tests.conftest import sse_events

    customer = {**login("customer@leon.demo"), "X-Request-ID": "sse-request-0007"}
    conversation_id = client.post("/api/conversations", json={}, headers=customer).json()["id"]
    r = client.post(f"/api/conversations/{conversation_id}/stream", json={"content": "积分可以怎么抵扣？"}, headers=customer)
    assert r.headers["X-Request-ID"] == "sse-request-0007"
    run_id = [e for e in sse_events(r) if e["type"] == "done"][0]["run_id"]
    assert client.get(f"/api/runs/{run_id}", headers=login("admin@leon.demo")).json()["request_id"] == "sse-request-0007"


def test_security_headers_on_every_response(client):
    for r in (client.get("/api/health"), client.get("/api/does-not-exist")):
        assert r.headers["X-Content-Type-Options"] == "nosniff"
        assert r.headers["X-Frame-Options"] == "DENY"
        assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
        assert "script-src 'self'" in r.headers["Content-Security-Policy"]


def test_validation_errors_are_readable_and_never_echo_the_input(client, login):
    r = client.post("/api/chat", json={"content": "退" * 2001}, headers=login("customer@leon.demo"))
    assert r.status_code == 422
    assert r.json()["detail"] == "消息内容不能超过 2000 个字" and r.json()["request_id"]
    assert "退退退" not in r.text
    r = client.post("/api/auth/login", json={"email": "emma@example.com", "password": "secret-" * 30})
    assert r.status_code == 422 and r.json()["detail"] == "密码不能超过 128 个字"
    assert "secret-" not in r.text, "a rejected password must not come back in the response"
    r = client.post("/api/auth/login", json={"email": "emma@example.com"})
    assert r.json()["detail"] == "缺少密码"


def test_responses_carry_server_timing(client):
    r = client.get("/api/health")
    name, _, duration = r.headers["server-timing"].partition(";dur=")
    assert name == "app" and float(duration) >= 0


def test_the_access_log_says_which_tenant_and_user_made_the_request(client, login):
    # Authentication runs in a worker thread on a copy of the request context; the tenant and
    # user it recorded never reached the access log line, which showed null for every request.
    import io

    headers = login("customer@leon.demo")
    me = client.get("/api/auth/me", headers=headers).json()
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    access = logging.getLogger("loopdesk.access")
    access.addHandler(handler)
    try:
        client.get("/api/conversations", headers=headers)
        client.get("/api/health")
    finally:
        access.removeHandler(handler)
    lines = [json.loads(line) for line in stream.getvalue().splitlines()]
    by_path = {line["path"]: line for line in lines}
    assert by_path["/api/conversations"]["tenant_id"] == me["tenant"]["id"]
    assert by_path["/api/conversations"]["user_id"] == me["id"]
    assert by_path["/api/health"]["tenant_id"] is None, "nothing leaks from the previous request"


def test_indexes_are_built_at_startup_and_readiness_waits_for_them(settings, monkeypatch, tmp_path):
    # A restarted process used to make its first customer wait for the index build (about 9 s at
    # 50,000 chunks). It is now built in the background at startup; until then /api/ready says
    # 503, so a load balancer sends no traffic to the instance.
    import threading

    from fastapi.testclient import TestClient

    from app.main import create_app
    from app.rag.retrieval import index_manager
    from tests.conftest import _reset_database

    _reset_database(settings.database_url)
    index_manager._cache.clear()
    settings.prewarm_index = True
    release, building = threading.Event(), threading.Event()
    build = index_manager.get

    def slow_build(db, tenant_id):
        building.set()
        release.wait(10)
        return build(db, tenant_id)

    monkeypatch.setattr(index_manager, "get", slow_build)
    with TestClient(create_app(settings)) as client:
        assert building.wait(10)
        r = client.get("/api/ready")
        assert r.status_code == 503 and r.json()["checks"]["retrieval_index"] == "warming up"
        release.set()
        from app.rag.retrieval import prewarm
        assert prewarm.done.wait(10)
        assert client.get("/api/ready").status_code == 200
    assert len(index_manager._cache) == 2, "both demo tenants are warm"
