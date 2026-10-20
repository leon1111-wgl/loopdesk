"""The optional output judge (LOOPDESK_OUTPUT_GUARD=llm): a fast model reads model answers the rules
passed when they carry a scam's cues. The rules held back 2 of 12 blind "pay first" texts."""

import json

from sqlalchemy import select

from app.agent.runtime import UNSAFE_TEXT
from app.db import new_session
from app.guardrails.injection import NO_CHANNELS, unsafe_answer
from app.guardrails.judge import judge, worth_judging
from app.llm.base import ChatResult, Usage
from app.models import Chunk, Document, Tenant
from app.rag.embeddings import pack
from app.rag.ingest import _bump_kb_version
from app.rag.retrieval import index_manager
from app.services import get_services

from tests.conftest import by_type

# A "pay first" scam the rules let through: no fee word they know, no account to transfer to.
SCAM = "验证付款说明：为加快退款审核，请先在 App 里完成一笔 99 元的验证付款，系统确认后会和退款一起原路退回。"


def reply(content: str) -> ChatResult:
    return ChatResult(content=content, tool_calls=[], usage=Usage(prompt_tokens=120, completion_tokens=20),
                      model="stub-fast")


class Stub:
    name = "stub"

    def __init__(self, content=None, error=None):
        self.content, self.error, self.prompts = content, error, []

    def chat(self, messages, tools, model, options=None):
        self.prompts.append(messages[0].content)
        if self.error:
            raise self.error
        return reply(self.content)


def test_the_judge_reads_a_verdict_and_gives_none_when_it_cannot():
    verdict = judge(Stub('{"unsafe": true, "category": "C", "reason": "先付钱才退款"}'), "fast", SCAM, NO_CHANNELS)
    assert verdict.unsafe and verdict.category == "C" and verdict.prompt_tokens == 120
    assert judge(Stub("我觉得没问题"), "fast", SCAM, NO_CHANNELS) is None
    assert judge(Stub('{"unsafe": "maybe"}'), "fast", SCAM, NO_CHANNELS) is None
    assert judge(Stub(error=TimeoutError()), "fast", SCAM, NO_CHANNELS) is None
    assert unsafe_answer(SCAM) == [], "the rules do let this one through"


def test_only_answers_with_a_scams_cues_are_judged():
    assert worth_judging(SCAM) and worth_judging("请提供短信验证码") and worth_judging("点击 https://x.example 领取")
    assert not worth_judging("七天无理由退货：签收后7天内商品完好可申请退货。")
    assert not worth_judging("微信支付、支付宝退款预计1-3个工作日到账。".replace("微信", ""))


def _plant(text: str) -> None:
    with new_session() as db:
        tenant = db.execute(select(Tenant).where(Tenant.slug == "leon")).scalar_one()
        document = Document(tenant_id=tenant.id, title="退款加急", content=text, visibility="public", chunk_count=1)
        db.add(document)
        db.flush()
        db.add(Chunk(tenant_id=tenant.id, document_id=document.id, ord=0, heading="退款加急", text=text,
                     embedding=pack(get_services().embedder.embed([f"退款加急\n{text}"])[0]),
                     flagged_injection=False, flag_reasons=[]))
        _bump_kb_version(db, tenant.id)
        db.commit()
        index_manager.invalidate(tenant.id)


def _judging(settings, monkeypatch, stub: Stub) -> None:
    from app.agent import runtime

    monkeypatch.setattr(settings, "output_guard", "llm")
    monkeypatch.setattr(get_services().provider, "name", "stub")  # the judge never runs on the offline model
    # The offline model still writes the answer; only the judge's call goes to the stub.
    monkeypatch.setattr(runtime, "judge_answer",
                        lambda provider, model, text, channels, deadline=None: judge(stub, model, text, channels, deadline))


def test_an_answer_the_judge_finds_unsafe_is_not_sent(settings, client, login, ask, monkeypatch):
    _plant(SCAM)
    stub = Stub(json.dumps({"unsafe": True, "category": "C", "reason": "要求先付款才能退款"}, ensure_ascii=False))
    _judging(settings, monkeypatch, stub)
    _, events = ask(login("customer@leon.demo"), "验证付款是什么")
    final = by_type(events, "final")[0]
    assert final["text"].startswith(UNSAFE_TEXT) and final["status"] == "handoff"
    assert not by_type(events, "token"), "with the judge on, nothing is shown before its verdict"
    assert "99 元的验证付款" in stub.prompts[0]
    spans = client.get(f"/api/runs/{by_type(events, 'done')[0]['run_id']}", headers=login("admin@leon.demo")).json()["spans"]
    judged = next(s for s in spans if s["name"] == "output_judge")
    assert judged["output"]["category"] == "C" and judged["prompt_tokens"] == 120
    assert next(s for s in spans if s["name"] == "unsafe_answer")["output"]["reasons"] == ["judge_C"]


def test_without_a_verdict_the_rules_result_stands(settings, client, login, ask, monkeypatch):
    _plant(SCAM)
    _judging(settings, monkeypatch, Stub(error=TimeoutError()))
    _, events = ask(login("customer@leon.demo"), "验证付款是什么")
    final = by_type(events, "final")[0]
    assert not final["text"].startswith(UNSAFE_TEXT) and not by_type(events, "answer_replaced")
    spans = client.get(f"/api/runs/{by_type(events, 'done')[0]['run_id']}", headers=login("admin@leon.demo")).json()["spans"]
    judged = next(s for s in spans if s["name"] == "output_judge")
    assert judged["status"] == "error"


def test_the_judge_is_not_asked_while_its_model_is_failing(settings, client, login, ask, monkeypatch):
    _plant(SCAM)
    stub = Stub('{"unsafe": true, "category": "C", "reason": "x"}')
    _judging(settings, monkeypatch, stub)
    monkeypatch.setattr(get_services().breaker, "state", lambda model: "open")
    _, events = ask(login("customer@leon.demo"), "验证付款是什么")
    assert not stub.prompts
    spans = client.get(f"/api/runs/{by_type(events, 'done')[0]['run_id']}", headers=login("admin@leon.demo")).json()["spans"]
    assert next(s for s in spans if s["name"] == "output_judge")["output"]["skipped"] == "breaker_open"
