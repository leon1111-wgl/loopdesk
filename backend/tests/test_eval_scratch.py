"""Evaluation and benchmark tools clean up their throwaway databases. Each `--fresh` run used to
leave a directory behind (`mkdtemp` without cleanup): one development machine had 683 of them,
850 MB, after a day of evaluations."""

import os
import subprocess
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]


def _eval(tmp_path: Path, **env: str) -> subprocess.CompletedProcess:
    environment = {**os.environ, "TMPDIR": str(tmp_path), **env}
    if "LOOPDESK_KEEP_EVAL_DB" not in env:
        environment.pop("LOOPDESK_KEEP_EVAL_DB", None)
    return subprocess.run([sys.executable, "-m", "app.evals.cli", "--fresh", "--tenant", "iris"],
                          cwd=BACKEND, env=environment, capture_output=True, text=True, timeout=300)


def test_a_fresh_evaluation_leaves_nothing_behind(tmp_path):
    result = _eval(tmp_path)
    assert result.returncode == 0, result.stderr[-800:]
    # jieba keeps its dictionary cache in the temp dir on purpose; it is reused by every run
    assert [p.name for p in tmp_path.iterdir() if p.name != "jieba.cache"] == []


def test_the_database_can_be_kept_for_inspection(tmp_path):
    result = _eval(tmp_path, LOOPDESK_KEEP_EVAL_DB="1")
    assert result.returncode == 0, result.stderr[-800:]
    kept = list(tmp_path.glob("loopdesk-eval-*"))
    assert len(kept) == 1 and (kept[0] / "eval.db").exists() and str(kept[0]) in result.stderr


def test_every_tool_takes_its_database_from_the_scratch_helper():
    for path in (BACKEND / "app").rglob("*.py"):
        if path.name != "scratch.py":
            assert "mkdtemp" not in path.read_text(encoding="utf-8"), path
