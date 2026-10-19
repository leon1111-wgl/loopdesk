"""A customer who closes the tab mid-answer must not leave a run stranded."""

import json
import threading
import time

import httpx

from app.api.chat import detached
from app.evals.load_bench import _prepare_database, _start_server


def test_a_detached_run_finishes_after_the_reader_leaves():
    finished = threading.Event()

    def produce():
        for i in range(5):
            yield {"i": i}
            time.sleep(0.01)
        finished.set()

    stream = detached(produce)
    assert next(stream) == {"i": 0}
    stream.close()  # the client went away
    assert finished.wait(2), "the run kept going without a reader"


def test_closing_the_tab_mid_answer_still_saves_the_answer(tmp_path):
    # A real server: an in-process test client does not deliver a disconnect the way
    # uvicorn does. The mock streams slowly so the client leaves mid-answer.
    url = f"sqlite:///{tmp_path / 'stream.db'}"
    _prepare_database(url)
    proc, base = _start_server(url, workers=1, cache_hours=0, extra_env={"LOOPDESK_MOCK_STREAM_DELAY": "0.02"})
    try:
        def login(email):
            return {"Authorization": "Bearer " + httpx.post(
                f"{base}/api/auth/login", json={"email": email, "password": "loopdesk-demo"}).json()["token"]}

        emma, leon = login("customer@leon.demo"), login("admin@leon.demo")
        conversation_id = httpx.post(f"{base}/api/conversations", json={}, headers=emma).json()["id"]
        run_id = None
        with httpx.Client(timeout=30) as client:
            with client.stream("POST", f"{base}/api/conversations/{conversation_id}/stream",
                               json={"content": "七天无理由退货有什么条件？"}, headers=emma) as response:
                for line in response.iter_lines():
                    if line.startswith("data: ") and json.loads(line[6:])["type"] == "token":
                        run_id = json.loads(line[6:]).get("run_id") or run_id
                        break  # the answer has started streaming; the customer closes the tab
                    if line.startswith("data: ") and json.loads(line[6:])["type"] == "run_started":
                        run_id = json.loads(line[6:])["run_id"]
        deadline, messages = time.time() + 15, []
        while time.time() < deadline:
            messages = httpx.get(f"{base}/api/conversations/{conversation_id}/messages", headers=emma).json()["messages"]
            if len(messages) == 2:
                break
            time.sleep(0.2)
        assert [m["role"] for m in messages] == ["user", "assistant"], "the answer is waiting when the customer returns"
        assert httpx.get(f"{base}/api/runs/{run_id}", headers=leon).json()["status"] == "completed"
    finally:
        proc.terminate()
        proc.wait(timeout=10)
