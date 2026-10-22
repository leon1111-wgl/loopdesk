"""What the real-model workflows publish (W7): eval reports and the summary table go to public
Actions artifacts and the job summary, the demo fixtures to GitHub Pages. None of it may carry the
model key, the gateway address or a login token, and the numbers must add up across trials."""

import json

import pytest

from app.evals import summarize
from app.ops import leak_check

KEY = "sk-live-0123456789abcdefABCDEF"
GATEWAY = "https://relay.internal.example.net/v1"


def _env():
    return {"LOOPDESK_LLM_API_KEY": KEY, "LOOPDESK_LLM_BASE_URL": GATEWAY, "LOOPDESK_JWT_SECRET": "x" * 40}


def test_a_clean_file_passes(tmp_path):
    path = tmp_path / "report.json"
    path.write_text(json.dumps({"answer": "退款 1-3 个工作日到账", "model": "openai_compat:deepseek-v4-1-flash"}))
    assert leak_check.scan([path], _env()) == []


@pytest.mark.parametrize("text, kind", [
    (f"error calling model with key {KEY}", "LOOPDESK_LLM_API_KEY"),
    ("POST https://relay.internal.example.net/v1/chat/completions -> 502", "LOOPDESK_LLM_BASE_URL"),
    ("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJlLWJ5dGVz", "login token (JWT)"),
    ("api_key=sk-AbCdEfGhIjKlMnOpQrStUvWx", "API key (sk-…)"),
])
def test_a_secret_anywhere_in_a_file_is_refused_without_printing_it(tmp_path, text, kind):
    path = tmp_path / "fixtures.json"
    path.write_text(json.dumps({"bodies": {"a": {"detail": text}}}))
    findings = leak_check.scan([path], _env())
    assert kind in [f["kind"] for f in findings] and {f["file"] for f in findings} == {str(path)}
    assert KEY not in json.dumps(findings) and "relay.internal" not in json.dumps(findings)


def test_json_escaping_does_not_hide_a_secret(tmp_path):
    """A URL inside a JSON string can be written with escaped slashes."""
    path = tmp_path / "fixtures.json"
    path.write_text('{"e": "https:\\/\\/relay.internal.example.net\\/v1 timed out"}')
    assert [f["kind"] for f in leak_check.scan([path], _env())] == ["LOOPDESK_LLM_BASE_URL"]


def test_the_public_preset_hosts_and_short_values_are_not_secrets(tmp_path):
    path = tmp_path / "fixtures.json"
    path.write_text("api.deepseek.com is mentioned in the docs; the key 'x' is everywhere")
    env = {"LOOPDESK_LLM_API_KEY": "x", "LOOPDESK_LLM_BASE_URL": "https://api.deepseek.com/v1"}
    assert leak_check.scan([path], env) == []


def test_the_command_exits_non_zero_on_a_leak(tmp_path, monkeypatch, capsys):
    for name, value in _env().items():
        monkeypatch.setenv(name, value)
    bad = tmp_path / "bad.json"
    bad.write_text(KEY)
    good = tmp_path / "good.json"
    good.write_text("{}")
    assert leak_check.main([str(good)]) == 0
    assert leak_check.main([str(good), str(bad)]) == 1
    out = capsys.readouterr().out
    assert "bad.json" in out and KEY not in out


def _report(passed_keys, cases=("a", "b", "c"), tokens=(9000, 1000), cost=0.026, p50=4000, p95=9000):
    return {
        "model": "openai_compat:deepseek-v4-1-flash", "total": len(cases), "passed": len(passed_keys),
        "metrics": {"pass_rate": round(len(passed_keys) / len(cases), 4), "total_tokens": sum(tokens),
                    "prompt_tokens": tokens[0], "completion_tokens": tokens[1], "estimated_cost": cost,
                    "latency_p50_ms": p50, "latency_p95_ms": p95, "prompt_version": "1.02"},
        "cases": [{"key": k, "passed": k in passed_keys} for k in cases],
    }


def _write_trials(directory, tenant, suite, trials):
    for n, keys in enumerate(trials, start=1):
        (directory / f"{tenant}__{suite}__t{n}.json").write_text(json.dumps(_report(keys)))


def test_trials_are_summarised_per_suite_with_pass_hat_k_and_flaky_cases(tmp_path):
    _write_trials(tmp_path, "leon", "default", [{"a", "b"}, {"a"}, {"a", "b"}])
    _write_trials(tmp_path, "leon", "hard", [{"a", "b", "c"}] * 3)
    summary = summarize.summarize(tmp_path)
    [default] = [s for s in summary["suites"] if (s["tenant"], s["suite"]) == ("leon", "default")]
    assert default["trials"] == 3 and default["cases"] == 3
    assert default["passed"] == [2, 1, 2] and default["mean_passed"] == pytest.approx(5 / 3)
    assert default["pass_hat_k"] == pytest.approx(1 / 3), "only case a passed in every trial"
    assert (default["stable_pass"], default["flaky"], default["stable_fail"]) == (["a"], ["b"], ["c"])
    assert default["tokens_per_trial"] == 10000 and default["cost_per_trial"] == pytest.approx(0.026)
    assert summary["model"] == "openai_compat:deepseek-v4-1-flash"
    assert summary["total_tokens"] == 60000 and summary["total_cost"] == pytest.approx(0.156)


def test_the_markdown_table_reads_like_the_evaluation_doc(tmp_path):
    _write_trials(tmp_path, "leon", "default", [{"a", "b"}, {"a"}, {"a", "b"}])
    text = summarize.markdown(summarize.summarize(tmp_path), commit="abc1234def", prices=(2.0, 8.0))
    assert "| leon | default | 3 | 3 | 1.67 (1–2) | 1/3 | 1 / 1 / 1 |" in text
    assert "abc1234" in text and "deepseek-v4-1-flash" in text and "¥2.0 / ¥8.0" in text
    assert "flaky: b" in text and "always failed: c" in text


def test_trials_with_different_case_sets_are_refused(tmp_path):
    """Comparing trials over different cases would make pass^k meaningless."""
    (tmp_path / "leon__default__t1.json").write_text(json.dumps(_report({"a"})))
    (tmp_path / "leon__default__t2.json").write_text(json.dumps(_report({"a"}, cases=("a", "b"))))
    with pytest.raises(ValueError, match="leon/default"):
        summarize.summarize(tmp_path)
