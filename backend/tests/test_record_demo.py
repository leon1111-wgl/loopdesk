"""The static demo on GitHub Pages replays responses recorded from the real backend
(app/ops/record_demo.py). These tests check the recording is complete and safe to publish."""

import json
import os
import re
from pathlib import Path

import pytest

from app.guardrails.injection import detect_injection
from app.ops import record_demo

CHAT_PAGE = Path(__file__).resolve().parents[2] / "frontend" / "src" / "pages" / "ChatPage.tsx"


@pytest.fixture(scope="module")
def recording():
    return record_demo.record()


def _page_suggestions() -> dict[str, list[str]]:
    """The suggestion lists as the chat page defines them."""
    source = CHAT_PAGE.read_text(encoding="utf-8")
    block = source[source.index("const SUGGESTIONS"):source.index("const REASONS")]
    lists: dict[str, list[str]] = {}
    for name, body in re.findall(r"(\w+): \[(.*?)\n  \]", block, flags=re.S):
        lists[name] = re.findall(r'\["(.+?)", "', body)
    extra = block[block.index("const STAFF_EXTRA"):]
    lists["staff_extra"] = re.findall(r'\["(.+?)", "', extra)
    return lists


def test_the_recorder_and_the_chat_page_offer_the_same_questions():
    page = _page_suggestions()
    assert all(page[tenant] == record_demo.SUGGESTED[tenant] for tenant in ("echo", "leon", "iris"))
    assert page["staff_extra"] == record_demo.STAFF_EXTRA


def test_customers_are_not_offered_attack_samples():
    """The customer's chat is the shop's support page: prompt-injection samples are a staff test case (N-046)."""
    page = _page_suggestions()
    customer = [q for tenant in ("echo", "leon", "iris") for q in page[tenant]]
    customer += [q for questions in record_demo.DEMO_ONLY.values() for q in questions]
    assert not [q for q in customer if detect_injection(q).suspicious]
    assert any(detect_injection(q).suspicious for q in page["staff_extra"]), "the staff test bench keeps one"


def test_every_suggestion_replays_for_every_account_that_is_shown_it(recording):
    for email, user in recording["users"].items():
        tenant, staff = user["tenant"]["slug"], user["role"] in ("admin", "agent")
        shown = record_demo.SUGGESTED[tenant] + (record_demo.STAFF_EXTRA if staff and tenant == "leon" else [])
        recorded = {chat["question"]: chat for chat in recording["chats"][email]}
        assert set(shown) <= set(recorded), email
        for chat in recorded.values():
            kinds = [event["type"] for event in chat["events"]]
            assert kinds[-2:] == ["final", "done"] and chat["messages"]["messages"], (email, chat["question"])


def test_the_staff_pages_have_something_to_show(recording):
    bodies, admin = recording["bodies"], recording["responses"]["admin@leon.demo"]
    pending = bodies[admin["/api/approvals?status_filter=pending"]]
    assert [(a["arguments"]["order_id"], a["required_role"]) for a in pending] == [("SO20261008002", "admin")]
    assert [(h["customer"], h["assignee"]) for h in bodies[admin["/api/handoffs"]]] == [("Jack", "Mia")]
    assert {t["status"] for t in bodies[admin["/api/tickets"]]} == {"open", "in_progress"}
    assert bodies[admin["/api/flywheel/patches"]] and bodies[admin["/api/evals/runs"]]
    assert bodies[admin["/api/audit/verify"]]["ok"] is True
    # The demo tables move no money: the refund console has nothing waiting, and its full list says why.
    assert bodies[admin["/api/refunds?status=open"]] == []
    assert bodies[admin["/api/refunds/summary"]] == {"unsettled": 0, "needs_person": 0}
    ledger = bodies[admin["/api/refunds?status=all"]]
    assert ledger and {r["follow_up"] for r in ledger} <= {"demo", None}
    for url in (u for u in admin if u.startswith("/api/runs/")):
        assert bodies[admin[url]]["spans"], url  # each trace can be opened


def test_the_main_demo_has_a_refund_to_review_and_a_person_at_work(recording):
    bodies, admin = recording["bodies"], recording["responses"]["admin@echo.demo"]
    pending = bodies[admin["/api/approvals?status_filter=pending"]]
    assert [a["arguments"]["order_id"] for a in pending] == ["SO20261015102"]
    assert [(h["customer"], h["assignee"]) for h in bodies[admin["/api/handoffs"]]] == [("Ryan", "Chloe")]
    assert [t["status"] for t in bodies[admin["/api/tickets"]]] == ["open"]
    [run] = bodies[admin["/api/evals/runs"]]  # the main demo's evaluation page is not empty
    assert run["total"] > 0


def test_nothing_secret_is_published(recording):
    text = json.dumps(recording, ensure_ascii=False)
    assert "eyJ" not in text, "no login token (JWTs start with eyJ)"
    assert "password_hash" not in text and "pbkdf2" not in text.lower()
    customer = recording["responses"]["customer@leon.demo"]
    assert not any(url.startswith(("/api/runs", "/api/approvals", "/api/audit", "/api/refunds")) for url in customer), \
        "a customer's recording holds only what a customer may see"


def test_the_recording_says_which_model_answered_and_when(recording):
    """The demo's banner names the model (W7): the offline one here, the real one when recorded on Actions."""
    assert recording["model"] == {"provider": "mock", "strong": "mock-strong", "fast": "mock-fast", "offline": True}
    assert recording["recorded_at"] and "commit" in recording
    assert recording["script_notes"] == [], "the offline model follows the script exactly"


class _TerseModel:
    """A real model that never calls a tool: the recorder must still finish (W7)."""

    name = "openai_compat"

    def chat(self, messages, tools, model, options=None):
        from app.llm.base import ChatResult, Usage

        return ChatResult("您好，这个问题我需要再核实一下，请稍候。", [], Usage(40, 12), model)

    def stream(self, messages, tools, model, options=None):
        from app.llm.base import StreamEvent

        result = self.chat(messages, tools, model, options)
        yield StreamEvent(type="delta", text=result.content)
        yield StreamEvent(type="done", result=result)


@pytest.fixture()
def configured_model(monkeypatch):
    for name in list(os.environ):
        if name.startswith("LOOPDESK_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("LOOPDESK_LLM_BASE_URL", "https://relay.private.example.net/v1")
    monkeypatch.setenv("LOOPDESK_LLM_API_KEY", "sk-recording-test-0123456789abcdef")
    monkeypatch.setenv("LOOPDESK_LLM_MODEL", "deepseek-v4-1-flash")
    monkeypatch.setattr("app.services.build_provider", lambda settings: _TerseModel())


def test_a_configured_model_records_what_it_did_and_never_its_key(configured_model):
    data = record_demo.record(model="configured")
    assert data["model"] == {"provider": "openai_compat", "strong": "deepseek-v4-1-flash",
                             "fast": "deepseek-v4-1-flash", "offline": False}
    assert data["script_notes"], "steps the model gave no occasion for are skipped and listed, not invented"
    assert all(note.startswith("skipped: ") for note in data["script_notes"])
    text = json.dumps(data, ensure_ascii=False)
    assert "sk-recording-test" not in text and "relay.private" not in text
    for email, chats in data["chats"].items():
        assert chats and all(c["events"][-1]["type"] == "done" for c in chats), email


def test_a_recording_that_carries_the_key_is_not_written(configured_model, monkeypatch, tmp_path):
    real = record_demo.record

    def leaky(model="mock"):
        data = real(model)
        data["bodies"]["x"] = {"detail": "upstream https://relay.private.example.net/v1 returned 502"}
        return data

    monkeypatch.setattr(record_demo, "record", leaky)
    out = tmp_path / "fixtures.json"
    with pytest.raises(SystemExit, match="LOOPDESK_LLM_BASE_URL"):
        record_demo.main([str(out), "--model", "configured"])
    assert not out.exists()


def test_a_real_recording_from_the_same_code_is_replayed(recording, configured_model):
    """Even a model that does far less than the script (no approvals, no handoffs) leaves every page and
    question the console asks for, so the deployment replays it rather than falling back (W7)."""
    from app.ops.demo_fixtures import choose

    real = record_demo.record(model="configured")
    chosen, reasons = choose(recording, real)
    assert reasons == [] and chosen is real
