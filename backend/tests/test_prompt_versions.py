from app.agent.prompts import PROMPT_VERSIONS, choose_prompt_version
from app.config import Settings, prompt_version_name
from app.db import new_session
from app.evals import cli
from app.models import EvalRun, Run
from tests.conftest import by_type


def test_every_prompt_version_keeps_the_placeholders_and_safety_rules():
    for version, template in PROMPT_VERSIONS.items():
        for placeholder in ("{tenant}", "{role}", "{today}"):
            assert placeholder in template, version
        for rule in ("search_knowledge", "request_refund", "没有找到相关依据", "忽略指令"):
            assert rule in template, (version, rule)
        assert template.rstrip().splitlines()[-1].startswith("当前会话"), "variable facts stay in the last line"


def test_versions_are_written_1_00_2_00_and_the_earlier_names_still_work(monkeypatch):
    assert list(PROMPT_VERSIONS) == ["1.00", "2.00"]
    assert [prompt_version_name(v) for v in ("v1", "V2", "2", "2.0", "1.05", " 1.00 ")] == [
        "1.00", "2.00", "2.00", "2.00", "1.05", "1.00"]
    assert prompt_version_name("beta") == "beta" and prompt_version_name(None) is None
    # PROMPT_VERSION=v2 in an existing deployment keeps naming the same template
    monkeypatch.setenv("LOOPDESK_PROMPT_VERSION", "v2")
    monkeypatch.setenv("LOOPDESK_PROMPT_CANARY_VERSION", "v1")
    settings = Settings.from_env()
    assert (settings.prompt_version, settings.prompt_canary_version) == ("2.00", "1.00")
    assert choose_prompt_version("v1", "v2", 100, "x") == "2.00"


def test_canary_split_is_stable_and_roughly_proportional():
    picks = [choose_prompt_version("1.00", "2.00", 30, f"conversation:{i}") for i in range(1000)]
    share = picks.count("2.00") / len(picks)
    assert 0.25 < share < 0.35
    assert all(choose_prompt_version("1.00", "2.00", 30, f"conversation:{i}") == picks[i] for i in range(50))
    assert choose_prompt_version("1.00", "9.00", 50, "x") == "1.00", "unknown canary falls back to the default"
    assert choose_prompt_version("1.00", "2.00", 0, "x") == "1.00"


def test_runs_record_the_prompt_version_and_cache_is_versioned(settings, client, ask, login):
    customer = login("customer@leon.demo")
    _, first = ask(customer, "积分可以怎么抵扣？")
    settings.prompt_canary_version, settings.prompt_canary_percent = "2.00", 100
    _, second = ask(customer, "积分可以怎么抵扣？")
    done = by_type(second, "done")[0]
    with new_session() as db:
        assert db.get(Run, done["run_id"]).route_tier != "cache", "an answer cached under 1.00 is not replayed for 2.00"
    admin = login("admin@leon.demo")
    trace = client.get(f"/api/runs/{done['run_id']}", headers=admin).json()
    router = next(s for s in trace["spans"] if s["kind"] == "router")
    assert router["output"]["prompt_version"] == "2.00" and trace["prompt_version"] == "2.00"
    earlier = by_type(first, "done")[0]["run_id"]
    with new_session() as db:  # a run recorded before versions were renamed
        run = db.get(Run, earlier)
        run.state = {**run.state, "prompt_version": "v1"}
        db.commit()
    assert client.get(f"/api/runs/{earlier}", headers=admin).json()["prompt_version"] == "1.00"
    overview = client.get("/api/metrics/overview", headers=admin).json()
    assert overview["prompt_versions"]["1.00"]["runs"] >= 1 and overview["prompt_versions"]["2.00"]["runs"] == 1
    assert "v1" not in overview["prompt_versions"], "old and new names of one version are one row"


def test_eval_api_accepts_a_prompt_version(client, login):
    admin = login("admin@leon.demo")
    run = client.post("/api/evals/runs", json={"prompt_version": "v2"}, headers=admin).json()
    assert run["metrics"]["prompt_version"] == "2.00" and run["metrics"]["pass_rate"] == 1.0
    assert client.post("/api/evals/runs", json={"prompt_version": "9.00"}, headers=admin).status_code == 400
    prompts = client.get("/api/evals/prompts", headers=admin).json()
    assert prompts["default"] == "1.00" and [v["version"] for v in prompts["versions"]] == ["1.00", "2.00"]
    with new_session() as db:  # an eval run recorded before versions were renamed
        stored = db.get(EvalRun, run["id"])
        stored.metrics = {**stored.metrics, "prompt_version": "v2"}
        db.commit()
    assert client.get(f"/api/evals/runs/{run['id']}", headers=admin).json()["metrics"]["prompt_version"] == "2.00"


def test_offline_ab_compare_passes_and_reports_token_change(capsys):
    assert cli.main(["--fresh", "--compare", "1.00,v2", "--gate", "0.95"]) == 0
    out = capsys.readouterr().out
    assert "regressions: none" in out and "token change 2.00 vs 1.00" in out
