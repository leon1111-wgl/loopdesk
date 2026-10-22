"""Which recording the Pages demo replays (W7). A real-model recording is made on demand and committed;
the offline one is recorded on every deployment from the code being deployed. The demo replays the
real-model one only while the console being deployed finds everything it asks for in it."""

import copy
import json

from app.ops import demo_fixtures


def _fixtures(model="mock"):
    return {
        "recorded_at": "2026-10-22T10:00:00+00:00", "commit": "abc123def456", "script_notes": [],
        "model": {"provider": model, "strong": f"{model}-strong", "fast": f"{model}-fast", "offline": model == "mock"},
        "accounts": {"accounts": []},
        "users": {"agent@echo.demo": {"role": "agent"}, "customer@echo.demo": {"role": "customer"}},
        "responses": {
            "agent@echo.demo": {"/api/tenant": "t", "/api/runs/05bd54d8ecc84598966ca8bdaad0e6ca": "r1",
                                "/api/conversations/7/messages": "m", "/api/flywheel/feedback": "f"},
            "customer@echo.demo": {"/api/conversations": "c"},
        },
        "bodies": {"t": {"name": "Echo音乐", "auto_refund_limit": 20}, "r1": {"id": "x", "spans": []},
                   "m": {"messages": []}, "f": [{"id": 1, "rating": "up"}], "c": []},
        "chats": {"customer@echo.demo": [{"question": "怎么取消自动续费？", "events": [], "messages": None}]},
    }


def test_a_real_recording_that_covers_the_console_is_replayed():
    offline, real = _fixtures(), _fixtures("openai_compat")
    real["responses"]["agent@echo.demo"]["/api/runs/1928b0af6fa24669be2a97d8d4e1a47d"] = "r1"  # other run ids
    del real["responses"]["agent@echo.demo"]["/api/runs/05bd54d8ecc84598966ca8bdaad0e6ca"]
    chosen, reasons = demo_fixtures.choose(offline, real)
    assert chosen is real and reasons == []


def test_a_new_account_page_or_question_falls_back_to_the_offline_recording():
    offline, real = _fixtures(), _fixtures("openai_compat")
    offline["users"]["admin@harbour.demo"] = {"role": "admin"}
    offline["responses"]["agent@echo.demo"]["/api/refunds/summary"] = "t"
    offline["chats"]["customer@echo.demo"].append({"question": "年卡能退吗？", "events": [], "messages": None})
    chosen, reasons = demo_fixtures.choose(offline, real)
    assert chosen is offline
    text = "\n".join(reasons)
    assert "admin@harbour.demo" in text and "/api/refunds/summary" in text and "年卡能退吗？" in text


def test_a_changed_response_shape_falls_back_too():
    offline, real = _fixtures(), _fixtures("openai_compat")
    offline["bodies"]["t"] = {**offline["bodies"]["t"], "official_domains": []}  # a field the console now reads
    offline["bodies"]["f"] = [{"id": 1, "rating": "up", "reason": None}]
    chosen, reasons = demo_fixtures.choose(offline, real)
    assert chosen is offline
    assert any("/api/tenant" in r and "official_domains" in r for r in reasons)
    assert any("/api/flywheel/feedback" in r and "reason" in r for r in reasons)


def test_no_real_recording_means_the_offline_one():
    offline = _fixtures()
    assert demo_fixtures.choose(offline, None) == (offline, ["no real-model recording"])


def test_the_command_writes_the_choice_and_says_why(tmp_path, capsys):
    offline, real = _fixtures(), _fixtures("openai_compat")
    (tmp_path / "offline.json").write_text(json.dumps(offline))
    (tmp_path / "real.json").write_text(json.dumps(real))
    out = tmp_path / "dist" / "fixtures.json"
    assert demo_fixtures.main(["--offline", str(tmp_path / "offline.json"), "--real", str(tmp_path / "real.json"),
                               "--out", str(out)]) == 0
    assert json.loads(out.read_text())["model"]["provider"] == "openai_compat"
    assert "openai_compat-strong" in capsys.readouterr().out
    stale = copy.deepcopy(offline)
    stale["users"]["admin@harbour.demo"] = {"role": "admin"}
    (tmp_path / "offline.json").write_text(json.dumps(stale))
    assert demo_fixtures.main(["--offline", str(tmp_path / "offline.json"), "--real", str(tmp_path / "real.json"),
                               "--out", str(out)]) == 0
    assert json.loads(out.read_text())["model"]["provider"] == "mock"
    assert "admin@harbour.demo" in capsys.readouterr().out
    assert demo_fixtures.main(["--offline", str(tmp_path / "offline.json"), "--real", str(tmp_path / "missing.json"),
                               "--out", str(out)]) == 0, "a fork without a real recording deploys the offline one"


def test_a_field_inside_a_list_of_records_counts_but_map_keys_do_not():
    """A trace's spans are records with a fixed shape: a new span field the console reads must make a
    stale recording fall back. Keys of a nested dict are often data (counts per tool), so they never do."""
    offline, real = _fixtures(), _fixtures("openai_compat")
    real["bodies"]["r1"] = {"id": "x", "spans": [{"name": "chat", "ms": 3}], "by_tool": {"search_knowledge": 1}}
    offline["bodies"]["r1"] = {"id": "x", "spans": [{"name": "chat", "ms": 3}], "by_tool": {"get_order": 2}}
    assert demo_fixtures.choose(offline, real)[0] is real, "different tools used: not a shape change"
    offline["bodies"]["r1"]["spans"] = [{"name": "chat", "ms": 3, "cached_tokens": 0}]
    chosen, reasons = demo_fixtures.choose(offline, real)
    assert chosen is offline and any("spans[].cached_tokens" in r for r in reasons)
