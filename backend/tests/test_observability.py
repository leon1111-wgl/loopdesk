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
