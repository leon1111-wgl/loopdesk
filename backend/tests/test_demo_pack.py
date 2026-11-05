"""The static demo's browser engine reads the backend's own rules and data (app/ops/demo_pack.py). When they change, the
committed pack must change with them, or the demo would answer by rules the product no longer has."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from app.ops.demo_pack import js_pattern, money_phrases

ENGINE = Path(__file__).resolve().parents[2] / "frontend" / "src" / "demo" / "engine"


def _check(path: Path) -> int:
    """In its own process: building the pack seeds a database of its own, and the app's globals belong to the tests."""
    return subprocess.run([sys.executable, "-m", "app.ops.demo_pack", str(path), "--check"], cwd=ENGINE.parents[3] / "backend",
                          capture_output=True, text=True, timeout=120).returncode


def test_the_committed_pack_is_current():
    assert _check(ENGINE / "pack.json") == 0, "run: python -m app.ops.demo_pack ../frontend/src/demo/engine/pack.json"


def test_a_changed_rule_makes_the_check_fail(tmp_path):
    stale = json.loads((ENGINE / "pack.json").read_text(encoding="utf-8"))
    stale["tenants"]["leon"]["auto_refund_limit"] = 9999
    path = tmp_path / "pack.json"
    path.write_text(json.dumps(stale, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "money_phrases.json").write_text((ENGINE / "money_phrases.json").read_text(encoding="utf-8"), encoding="utf-8")
    assert _check(path) == 1


def test_expressions_are_valid_in_javascript_terms():
    pattern = js_pattern(re.compile(r"(?:a|b)*+c++[\U0001F300-\U0001FAFF]", re.IGNORECASE))
    assert pattern == {"source": r"(?:a|b)*c+[\u{1F300}-\u{1FAFF}]", "flags": "iu"}


def test_nothing_secret_goes_into_the_pack():
    from app.ops.leak_check import find_leaks

    assert find_leaks((ENGINE / "pack.json").read_text(encoding="utf-8")) == []


def test_flagged_knowledge_stays_out():
    # The supplier FAQ carries a planted instruction: the scan holds it back, so the demo does not carry it at all.
    kb = json.loads((ENGINE / "pack.json").read_text(encoding="utf-8"))["kb"]
    assert "无条件全额退款" not in json.dumps(kb, ensure_ascii=False)


def test_phrases_cover_both_answers():
    answers = [row[0] for row in money_phrases().values()]
    assert answers.count(True) > 100 and answers.count(False) > 100
