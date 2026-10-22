"""A bad rating stops a cached answer being replayed; admins can see and purge the cache (N-097)."""

from datetime import datetime, timedelta, timezone

from app.db import new_session
from app.models import AnswerCache, AuditLog, EvalCase, Feedback, Message, Run, Span
from tests.conftest import by_type

Q = "退款多久能到账？"


def _tier(events):
    with new_session() as db:
        return db.get(Run, by_type(events, "done")[0]["run_id"]).route_tier


def _rate(client, headers, events, rating="down", **extra):
    message_id = by_type(events, "final")[0]["message_id"]
    r = client.post(f"/api/messages/{message_id}/feedback", json={"rating": rating, **extra}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_a_thumbs_down_on_a_cached_answer_stops_the_replay(client, ask, login):
    emma, jack = login("customer@leon.demo"), login("customer2@leon.demo")
    _, first = ask(emma, Q)
    _, replay = ask(jack, Q)
    assert _tier(replay) == "cache"
    result = _rate(client, jack, replay, reason_category="knowledge_gap")
    assert result["cache_evicted"] == 1
    _, again = ask(emma, Q)
    assert _tier(again) != "cache", "the rated-down answer was replayed"
    # Same knowledge, same configuration: regenerating gives the same answer, so it is not stored again.
    _, fourth = ask(jack, Q)
    assert _tier(fourth) != "cache"
    with new_session() as db:
        rows = db.query(AnswerCache).all()
        assert len(rows) == 1 and rows[0].answer == ""
        assert rows[0].meta == {"evicted": "feedback", "message_id": by_type(replay, "final")[0]["message_id"]}


def test_a_thumbs_down_on_the_answer_that_was_stored_evicts_it_too(client, ask, login):
    emma = login("customer@leon.demo")
    _, first = ask(emma, Q)
    assert _rate(client, emma, first)["cache_evicted"] == 1
    _, again = ask(login("customer2@leon.demo"), Q)
    assert _tier(again) != "cache"


def test_a_thumbs_up_or_a_changed_mind_behave(client, ask, login):
    emma, jack = login("customer@leon.demo"), login("customer2@leon.demo")
    _, first = ask(emma, Q)
    assert _rate(client, emma, first, "up")["cache_evicted"] == 0
    _, replay = ask(jack, Q)
    assert _tier(replay) == "cache", "a thumbs-up keeps the answer"
    # up → down is a new rating on the same feedback row: it still evicts.
    assert _rate(client, emma, first, "down")["cache_evicted"] == 1
    assert _rate(client, emma, first, "down")["cache_evicted"] == 0  # nothing left to evict


def test_a_fix_to_the_knowledge_lets_the_question_be_cached_again(client, ask, login):
    emma, jack = login("customer@leon.demo"), login("customer2@leon.demo")
    _, first = ask(emma, Q)
    _rate(client, emma, first)
    client.post("/api/kb/documents/text", json={"title": "退款补充", "content": "## 退款\n- 原路退回。"},
                headers=login("admin@leon.demo"))
    ask(emma, Q)
    _, replay = ask(jack, Q)
    assert _tier(replay) == "cache", "a new knowledge version is a fresh start"


def test_a_bad_rating_on_a_cached_answer_is_attributed_from_the_run_that_produced_it(client, ask, login):
    emma, jack = login("customer@leon.demo"), login("customer2@leon.demo")
    _, first = ask(emma, Q)
    _, replay = ask(jack, Q)
    source = by_type(first, "done")[0]["run_id"]
    with new_session() as db:  # make the original retrieval look weak: the replay inherits the diagnosis
        assert db.get(Message, by_type(replay, "final")[0]["message_id"]).meta["cache_source_run_id"] == source
        span = db.query(Span).filter(Span.run_id == source, Span.name == "search_knowledge").one()
        span.output = {"output": {"results": []}}
        db.commit()
    assert _rate(client, jack, replay)["attribution"] == "knowledge_gap"
    with new_session() as db:
        assert db.query(Feedback).one().attribution == "knowledge_gap"


def test_admins_list_and_purge_the_cache(client, ask, login):
    admin, emma = login("admin@leon.demo"), login("customer@leon.demo")
    ask(emma, Q)
    ask(login("customer2@leon.demo"), Q)
    ask(emma, "积分可以怎么抵扣？")
    entries = client.get("/api/answer-cache", headers=admin).json()
    assert [e["hits"] for e in entries] == [1, 0] and entries[0]["question"] == "退款多久能到账?"
    assert client.get("/api/answer-cache", headers=emma).status_code == 403
    assert client.get("/api/answer-cache", headers=login("admin@iris.demo")).json() == []
    assert client.delete(f"/api/answer-cache/{entries[0]['id']}", headers=login("admin@iris.demo")).status_code == 404
    assert client.delete(f"/api/answer-cache/{entries[0]['id']}", headers=admin).json() == {"deleted": 1}
    _, again = ask(emma, Q)
    assert _tier(again) != "cache"
    assert client.delete("/api/answer-cache", headers=admin).json() == {"deleted": 2}
    with new_session() as db:
        audits = db.query(AuditLog).filter(AuditLog.action == "answer_cache.purge").all()
        assert len(audits) == 2 and all("退款" not in str(a.detail) for a in audits)


def test_a_correction_on_a_replay_is_filed_under_the_customer_who_got_it(client, ask, login):
    # Review: the eval case took its user from the source run, so Emma's email sat in a case hanging
    # off Jack's feedback, where erasing Emma could not find it.
    emma, jack = login("customer@leon.demo"), login("customer2@leon.demo")
    ask(emma, Q)
    _, replay = ask(jack, Q)
    result = _rate(client, login("agent@leon.demo"), replay, "correction", correction="1-3个工作日原路退回",
                   reason_category="tone")
    assert result["cache_evicted"] == 1
    with new_session() as db:
        case = db.get(EvalCase, result["eval_case_id"])
        assert case.as_user == "customer2@leon.demo" and case.expected_tools == ["search_knowledge"]


def test_the_block_lasts_a_full_ttl_from_the_rating(client, ask, login):
    emma = login("customer@leon.demo")
    _, first = ask(emma, Q)
    with new_session() as db:  # stored 23 hours ago: one hour left
        db.query(AnswerCache).update({"expires_at": datetime.now(timezone.utc) + timedelta(hours=1)})
        db.commit()
    _rate(client, emma, first)
    with new_session() as db:
        left = db.query(AnswerCache).one().expires_at.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)
        assert left > timedelta(hours=23)


def test_customers_do_not_see_whose_run_they_were_replayed(client, ask, login):
    emma, jack = login("customer@leon.demo"), login("customer2@leon.demo")
    ask(emma, Q)
    conversation_id, replay = ask(jack, Q)
    assert "cache_source_run_id" not in by_type(replay, "final")[0]["meta"]
    messages = client.get(f"/api/conversations/{conversation_id}/messages", headers=jack).json()["messages"]
    assert all("cache_id" not in m["meta"] and "cache_source_run_id" not in m["meta"] for m in messages)
    staff = client.get(f"/api/conversations/{conversation_id}/messages", headers=login("admin@leon.demo")).json()["messages"]
    assert any(m["meta"].get("cache_source_run_id") for m in staff)


def test_an_answer_replayed_before_the_change_is_evicted_by_its_question(client, ask, login):
    emma, jack = login("customer@leon.demo"), login("customer2@leon.demo")
    ask(emma, Q)
    _, replay = ask(jack, Q)
    with new_session() as db:  # what a replay saved before cache ids existed looks like
        message = db.get(Message, by_type(replay, "final")[0]["message_id"])
        message.meta = {k: v for k, v in message.meta.items() if k not in ("cache_id", "cache_source_run_id")}
        db.commit()
    assert _rate(client, jack, replay)["cache_evicted"] == 1
