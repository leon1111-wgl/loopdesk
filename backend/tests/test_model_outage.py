"""When the model service fails (review round 4, N-031). Before: every message got "智能客服暂时繁忙，
请稍后再试或转人工客服", and a customer who then typed "转人工" got the same reply, because handing
over was itself a decision the model had to make. A hanging model held each request for 90 s."""

from app.db import new_session
from app.llm.base import LLMRetryableError
from app.models import Run
from app.services import get_services
from tests.conftest import by_type


def _model(monkeypatch, error: Exception | None = None) -> list[str]:
    """Record every model call; with ``error``, every call fails with it."""
    provider = get_services().provider
    real = provider.stream
    calls: list[str] = []

    def stream(messages, tools, model, options=None):
        calls.append(model)
        if error is not None:
            raise error
        yield from real(messages, tools, model, options)

    monkeypatch.setattr(provider, "stream", stream)
    return calls


def _final(events: list[dict]) -> dict:
    return by_type(events, "final")[0]


def test_an_explicit_request_for_a_person_does_not_wait_for_the_model(client, ask, login, monkeypatch):
    calls = _model(monkeypatch, LLMRetryableError("HTTP 500: upstream down"))
    jack = login("customer2@leon.demo")
    conversation_id, events = ask(jack, "转人工")
    assert calls == [], "the model is not consulted, so its outage does not matter"
    assert _final(events)["status"] == "handoff" and "排队" in _final(events)["text"]
    queue = client.get("/api/handoffs", headers=login("agent@leon.demo")).json()
    assert [q["conversation_id"] for q in queue] == [conversation_id]


def test_only_explicit_requests_skip_the_model(ask, login, monkeypatch):
    calls = _model(monkeypatch)
    emma = login("customer@leon.demo")
    for text in ("我要人工客服！", "麻烦帮我转接人工", "能转人工吗", "我要投诉"):
        assert _final(ask(emma, text)[1])["status"] == "handoff", text
    assert calls == []
    for text in ("不用转人工，帮我查下 SO20261008002 的物流", "人工客服几点上班？"):
        ask(emma, text)
    assert len(calls) >= 2, "a negated request or a question about the service goes to the model"


def test_when_the_model_fails_faq_questions_get_the_policy_and_the_rest_a_person(client, ask, login, monkeypatch):
    _model(monkeypatch, LLMRetryableError("HTTP 503: overloaded"))
    emma = login("customer@leon.demo")
    _, events = ask(emma, "退款多久能到账？")
    final = _final(events)
    assert "1-3个工作日" in final["text"] and final["meta"]["citations"], "the policy itself, cited"
    assert "暂时繁忙" not in final["text"] and final["meta"]["degraded"] is True
    with new_session() as db:
        run = db.get(Run, final["run_id"])
        assert run.error and "503" in run.error, "the failure stays visible to operators"

    conversation_id, events = ask(emma, "SO20261008002 到哪了？")
    assert _final(events)["status"] == "handoff", "order questions need tools: a person takes them"
    _, events = ask(emma, "你们 CEO 是谁？")
    assert _final(events)["status"] == "handoff", "nothing relevant to quote: a person takes it"
    queue = client.get("/api/handoffs", headers=login("agent@leon.demo")).json()
    assert conversation_id in [q["conversation_id"] for q in queue]


def test_a_breaker_stops_calling_a_model_that_keeps_failing(ask, login, monkeypatch):
    calls = _model(monkeypatch, LLMRetryableError("HTTP 500"))
    breaker = get_services().breaker
    now = [1000.0]
    monkeypatch.setattr(breaker, "clock", lambda: now[0])
    emma = login("customer@leon.demo")
    for _ in range(breaker.settings.llm_breaker_failures):
        ask(emma, "积分可以怎么抵扣？")
    tried = len(calls)
    _, events = ask(emma, "积分可以怎么抵扣？")
    assert len(calls) == tried, "open: the failing model is not called at all"
    assert _final(events)["meta"]["degraded"] is True, "and the customer still gets an answer at once"

    now[0] += breaker.settings.llm_breaker_cooldown_seconds + 1
    ask(emma, "积分可以怎么抵扣？")
    assert len(calls) > tried, "after the cool-down one trial call goes through"
    tried = len(calls)
    ask(emma, "积分可以怎么抵扣？")
    assert len(calls) == tried, "the trial failed: open again"


def test_a_model_failure_after_an_approved_refund_still_reports_the_refund(client, ask, login, monkeypatch):
    # The refund has been paid when the model is asked to word the reply. Before, the customer read
    # "暂时繁忙，请稍后再试" about a refund that had gone through.
    _, events = ask(login("customer@leon.demo"), "SO20261008002 我要退款")
    approval_id = by_type(events, "approval_required")[0]["approval_id"]
    _model(monkeypatch, LLMRetryableError("HTTP 502"))
    r = client.post(f"/api/approvals/{approval_id}/decide", json={"decision": "approve", "comment": "核实无误"},
                    headers=login("admin@leon.demo"))
    assert r.status_code == 200
    text = r.json()["final"]["text"]
    assert "已受理" in text and "¥1299.00" in text and "繁忙" not in text


def test_the_follow_up_rewrite_does_not_wait_on_a_failing_model(ask, login, monkeypatch):
    import time

    services = get_services()
    real_chat = services.provider.chat
    asked: list[dict] = []

    def chat(messages, tools, model, options=None):
        if (options or {}).get("task") != "rewrite":  # the offline model answers through chat() too
            return real_chat(messages, tools, model, options)
        asked.append(options)
        raise LLMRetryableError("HTTP 500")

    monkeypatch.setattr(services.provider, "name", "hunyuan")  # a real model is asked to rewrite
    monkeypatch.setattr(services.provider, "chat", chat)
    emma = login("customer@leon.demo")
    conversation_id, _ = ask(emma, "退款多久能到账？")
    ask(emma, "那运费谁出？", conversation_id=conversation_id)
    assert asked and asked[-1]["deadline"] - time.monotonic() < 5, "seconds, not the whole run"

    fast = services.settings.llm_model_fast
    for _ in range(services.settings.llm_breaker_failures):
        services.breaker.record(fast, ok=False)
    ask(emma, "那发票呢？", conversation_id=conversation_id)
    assert len(asked) == 1, "an open breaker: the rule rewrite, no call"


def test_the_dashboard_counts_failures_even_when_customers_were_served(client, ask, login, monkeypatch):
    # N-037: with every run failing, the overview showed resolution, handoff and refusal rates of 0 and
    # nothing about failures. Customers are now served when the model fails (N-031), which would hide
    # an outage completely: the overview counts runs that hit an error, by cause, and so does /metrics.
    _model(monkeypatch, LLMRetryableError("HTTP 503: overloaded"))
    emma, admin = login("customer@leon.demo"), login("admin@leon.demo")
    ask(emma, "退款多久能到账？")  # the policy, quoted
    ask(emma, "SO20261008002 到哪了？")  # a person
    overview = client.get("/api/metrics/overview", headers=admin).json()
    failures = overview["failures"]
    assert (failures["runs"], failures["last_15m"], failures["rate"]) == (2, 2, 1.0)
    assert failures["kinds"] == {"unavailable": 2} and "503" in failures["last_error"]
    assert failures["last_kind"] == "unavailable"
    assert overview["resolution_rate"] == 0.0, "a quoted policy during an outage is not self-service"
    assert "loopdesk_run_errors_total 2" in client.get("/metrics").text


def test_a_rejected_request_is_told_apart_from_an_unavailable_model(client, ask, login, monkeypatch):
    from app.llm.base import LLMError

    _model(monkeypatch, LLMError("HTTP 400: unsupported parameter stream_options"))
    ask(login("customer@leon.demo"), "积分可以怎么抵扣？")
    failures = client.get("/api/metrics/overview", headers=login("admin@leon.demo")).json()["failures"]
    assert failures["kinds"] == {"rejected": 1}, "4xx: configuration or parameters, retrying will not help"


def test_the_breaker_counts_consecutive_failures_however_slow_they_are():
    # Found with a really unreachable model: "5 failures within 30 s" never opens for a model that
    # hangs (each failure takes the whole 45 s budget) or under light traffic, so every customer
    # kept waiting. Consecutive failures open it; any success starts the count again.
    from app.config import Settings
    from app.llm.breaker import CircuitBreaker

    settings = Settings()
    now = [0.0]
    breaker = CircuitBreaker(settings, clock=lambda: now[0])
    for _ in range(settings.llm_breaker_failures):
        assert breaker.allow("m")
        breaker.record("m", ok=False)
        now[0] += 45
    assert not breaker.allow("m"), "open, although the failures were minutes apart"

    other = CircuitBreaker(settings, clock=lambda: now[0])
    for _ in range(3 * settings.llm_breaker_failures):
        other.record("m", ok=False)
        other.record("m", ok=True)  # an occasional failure among successes never opens it
    assert other.allow("m") and other.state("m") == "closed"
