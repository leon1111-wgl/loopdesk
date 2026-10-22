"""The smoke check the README's "run it yourself" section and the deployment jobs end with (W7):
it must pass against a real server and fail when the server runs another model than expected."""

import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

from app.ops import smoke

BACKEND = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    database = tmp_path_factory.mktemp("smoke") / "smoke.db"
    env = {k: v for k, v in os.environ.items() if not k.startswith("LOOPDESK_")}
    env.update(LOOPDESK_DATABASE_URL=f"sqlite:///{database}", LOOPDESK_SEED_DEMO_TRAFFIC="0", LOOPDESK_LOG_LEVEL="WARNING")
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)], cwd=BACKEND,
                            env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_the_smoke_check_passes_against_a_running_server(server, capsys):
    assert smoke.main([server, "--wait", "60"]) == 0
    out = capsys.readouterr().out
    assert "OK    chat" in out and "model=mock-" in out and "A: " in out


def test_it_fails_when_the_server_runs_another_model(server, capsys):
    assert smoke.main([server, "--provider", "openai_compat", "--wait", "60"]) == 1
    assert "expected provider openai_compat" in capsys.readouterr().out


def test_it_fails_when_nothing_answers():
    with pytest.raises(SystemExit, match="not ready"):
        smoke.wait_ready("http://127.0.0.1:9", 1)
