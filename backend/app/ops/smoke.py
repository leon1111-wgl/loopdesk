"""Check a running LoopDesk from outside: ready, healthy, one real answer (W7).

    python3 backend/app/ops/smoke.py http://localhost:8000                       # any model
    python3 backend/app/ops/smoke.py http://localhost:8000 --provider openai_compat

The README's "run it yourself" commands end with it, and CI runs it against `docker compose up`
(offline model), the real-model workflow against the same stack with the configured model, and the
Kubernetes job against the manifests. Standard library only, so it runs on any machine with Python 3,
outside the image. It signs in as the demo agent of Echo音乐 (staff see which model answered), asks
one question and prints the answer, the model and the tokens. Exit code 1 on any failure."""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request

AGENT = ("agent@echo.demo", "loopdesk-demo")
QUESTION = "怎么取消自动续费？"


def _call(base: str, path: str, body: dict | None = None, token: str | None = None, timeout: float = 90) -> tuple[int, dict]:
    request = urllib.request.Request(base.rstrip("/") + path, method="POST" if body is not None else "GET",
                                     data=json.dumps(body).encode() if body is not None else None)
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"{}")


def wait_ready(base: str, seconds: float) -> dict:
    deadline, last = time.monotonic() + seconds, "no answer"
    while time.monotonic() < deadline:
        try:
            status, body = _call(base, "/api/ready", timeout=5)
            if status == 200:
                return body
            last = json.dumps(body.get("checks", body), ensure_ascii=False)
        except (OSError, ValueError) as exc:  # not listening yet, or a proxy's HTML error page
            last = type(exc).__name__
        time.sleep(2)
    raise SystemExit(f"FAIL  not ready after {seconds:.0f} s: {last}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke-test a running LoopDesk")
    parser.add_argument("base_url")
    parser.add_argument("--provider", help="fail unless /api/health reports this model provider")
    parser.add_argument("--wait", type=float, default=180, help="seconds to wait for /api/ready")
    args = parser.parse_args(argv)
    base = args.base_url

    began = time.monotonic()
    ready = wait_ready(base, args.wait)
    print(f"OK    ready in {time.monotonic() - began:.0f} s: {json.dumps(ready['checks'], ensure_ascii=False)}")
    _, health = _call(base, "/api/health")
    print(f"OK    health: provider={health.get('llm_provider')} models={health.get('models')} "
          f"database={health.get('database')}")
    if args.provider and health.get("llm_provider") != args.provider:
        print(f"FAIL  expected provider {args.provider}, the server runs {health.get('llm_provider')}")
        return 1

    status, login = _call(base, "/api/auth/login", {"email": AGENT[0], "password": AGENT[1]})
    if status != 200:
        print(f"FAIL  login as {AGENT[0]}: HTTP {status} {login.get('detail')}")
        return 1
    began = time.monotonic()
    status, reply = _call(base, "/api/chat", {"content": QUESTION}, token=login["token"])
    seconds = time.monotonic() - began
    answer = (reply.get("answer") or "").strip()
    usage = reply.get("usage") or {}
    if status != 200 or not answer:
        print(f"FAIL  chat: HTTP {status} {reply.get('detail') or reply}")
        return 1
    print(f"OK    chat in {seconds:.1f} s, status={reply.get('status')} model={usage.get('model')} "
          f"tokens={usage.get('prompt_tokens', 0)}+{usage.get('completion_tokens', 0)} "
          f"tools={reply.get('tools')} evidence={(reply.get('verification') or {}).get('verdict')}")
    print(f"      Q: {QUESTION}\n      A: {answer[:300]}")
    if reply.get("status") == "failed":
        print("FAIL  the run failed (see the trace of run %s)" % reply.get("run_id"))
        return 1
    if args.provider and args.provider != "mock" and str(usage.get("model", "")).startswith("mock"):
        print("FAIL  the offline model answered")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
