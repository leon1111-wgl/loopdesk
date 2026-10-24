"""Load-test baseline for the chat API.

Starts a real uvicorn server (offline model, its own database), logs in, then drives N
concurrent clients for a fixed time against POST /api/chat, once per concurrency level.

    python -m app.evals.load_bench --concurrency 1,4,16 --duration 10 --report perf.json
    python -m app.evals.load_bench --database-url postgresql+psycopg://... --workers 4
    python -m app.evals.load_bench --model-latency 6 --concurrency 20,60,120   # slow model, bursts

What it measures is the platform around the model (routing, retrieval, tools, verifier,
tracing, database writes), because the offline model answers instantly. With a real model
the model latency dominates and the numbers below become a floor, not a forecast.

``--model-latency S`` points the server at a fake model that takes S seconds per call
(``fake_model``) and switches to bursts: each level sends ``concurrency`` requests at once,
one per client, and reports how many were answered, how many were turned away (503) and how
slow ``/api/health`` got meanwhile. That measures how many answers one process holds open (N-158),
so these bursts lift the per-tenant and per-user shares unless ``--env`` sets them.

``--fairness`` (with ``--model-latency``) is the noisy-neighbour test (N-181): Leon优选's two
customers send ``concurrency`` requests at once, a second later Iris云's Noah sends 3, and the
report counts each tenant's answers, 503s and 429s under the default shares.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import socket
import statistics
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

from . import fake_model
from .scratch import scratch_database_url

BACKEND_DIR = Path(__file__).resolve().parents[2]
QUESTIONS = [
    ("customer@leon.demo", "退款多久能到账？"),
    ("customer@leon.demo", "SO20261008002 的快递到哪了？"),
    ("customer2@leon.demo", "寄到新疆要额外加运费吗？"),
    ("customer@leon.demo", "积分可以怎么抵扣？"),
    ("customer2@leon.demo", "公司能开增值税专用发票吗？需要什么材料？"),
    ("customer@leon.demo", "帮我查一下订单 SO20261012003"),
]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def _prepare_database(database_url: str) -> None:
    """Migrate and seed once, before any worker starts: with --workers N every worker would
    otherwise run the migrations and the seed at the same time. Non-destructive: an
    existing database keeps its data (the seed skips it)."""
    subprocess.run([sys.executable, "-m", "app.seed"], cwd=BACKEND_DIR, check=True, stdout=subprocess.DEVNULL,
                   env={**os.environ, "LOOPDESK_DATABASE_URL": database_url, "LOOPDESK_SEED_DEMO_TRAFFIC": "0"})


def _start_server(database_url: str, workers: int, cache_hours: float,
                  extra_env: dict[str, str] | None = None) -> tuple[subprocess.Popen, str]:
    port = _free_port()
    env = {
        **os.environ,
        "LOOPDESK_DATABASE_URL": database_url,
        "LOOPDESK_SEED_DEMO": "0",  # seeded by _prepare_database; non-demo mode needs a real secret
        "LOOPDESK_JWT_SECRET": secrets.token_urlsafe(48),
        "LOOPDESK_AUDIT_KEY": secrets.token_urlsafe(48),  # non-demo mode wants its own audit key too
        "LOOPDESK_SEED_DEMO_TRAFFIC": "0",
        "LOOPDESK_RATE_LIMIT_PER_MINUTE": "1000000",
        "LOOPDESK_MOCK_STREAM_DELAY": "0",
        "LOOPDESK_LOG_LEVEL": "WARNING",
        "LOOPDESK_ANSWER_CACHE_TTL_HOURS": str(cache_hours),
        **(extra_env or {}),
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--workers", str(workers),
         "--log-level", "warning"],
        cwd=BACKEND_DIR, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    for _ in range(150):
        try:
            if httpx.get(f"{base}/api/ready", timeout=1).status_code == 200:
                return proc, base
        except httpx.HTTPError:
            pass
        time.sleep(0.2)
    proc.kill()
    raise RuntimeError("server did not become ready")


def _server_ms(response: httpx.Response) -> float | None:
    """The app's own time from the Server-Timing header ("app;dur=12.3")."""
    for part in response.headers.get("server-timing", "").split(","):
        name, _, rest = part.strip().partition(";dur=")
        if name == "app" and rest:
            return float(rest)
    return None


def _chat_stream(client: httpx.Client, question: str, token: str) -> httpx.Response:
    """What the console does: a new conversation, then the SSE stream read to the end."""
    auth = {"Authorization": f"Bearer {token}"}
    created = client.post("/api/conversations", json={}, headers=auth)
    if created.status_code != 200:
        return created
    with client.stream("POST", f"/api/conversations/{created.json()['id']}/stream",
                       json={"content": question}, headers=auth) as response:
        response.read()
    return response


def _run_level(base: str, tokens: dict[str, str], concurrency: int, duration: float, burst: bool = False,
               stream: bool = False) -> dict:
    latencies: list[float] = []
    server: list[float] = []
    errors: dict[str, int] = {}
    health: list[float] = []
    turned_away: list[float] = []  # 503s: how fast a full process says so
    lock = threading.Lock()
    deadline = time.monotonic() + duration
    finished = threading.Event()

    def probe() -> None:  # liveness while the level runs: a probe that hangs gets the pod restarted
        with httpx.Client(base_url=base, timeout=120) as client:
            while not finished.wait(0.5):
                started = time.perf_counter()
                try:
                    client.get("/api/health")
                except httpx.HTTPError:
                    pass
                health.append((time.perf_counter() - started) * 1000)

    def worker(offset: int) -> None:
        with httpx.Client(base_url=base, timeout=120 if burst else 30) as client:
            i = offset
            while time.monotonic() < deadline or (burst and i == offset):
                email, question = QUESTIONS[i % len(QUESTIONS)]
                i += 1
                started = time.perf_counter()
                try:
                    # A burst's questions differ, so none is served from the answer cache.
                    text = f"{question}（第{offset}次）" if burst else question
                    r = _chat_stream(client, text, tokens[email]) if stream else client.post(
                        "/api/chat", json={"content": text}, headers={"Authorization": f"Bearer {tokens[email]}"})
                    key = None if r.status_code == 200 else str(r.status_code)
                    in_app = _server_ms(r)
                except httpx.HTTPError as exc:
                    key, in_app = type(exc).__name__, None
                elapsed = (time.perf_counter() - started) * 1000
                with lock:
                    if key is None:
                        latencies.append(elapsed)
                        if in_app is not None:
                            server.append(in_app)
                    else:
                        errors[key] = errors.get(key, 0) + 1
                        if key == "503":
                            turned_away.append(elapsed)
                if burst:
                    return

    started_at = time.perf_counter()
    prober = threading.Thread(target=probe)
    prober.start()
    threads = [threading.Thread(target=worker, args=(n,)) for n in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    finished.set()
    prober.join()
    if burst:
        duration = time.perf_counter() - started_at
    ok = len(latencies)
    total = ok + sum(errors.values())
    return {
        "concurrency": concurrency, "requests": total, "ok": ok, "errors": errors,
        "error_rate": round(sum(errors.values()) / total, 4) if total else 0.0,
        "rps": round(ok / duration, 1),
        "p50_ms": round(_percentile(latencies, 0.5), 1),
        "p95_ms": round(_percentile(latencies, 0.95), 1),
        "p99_ms": round(_percentile(latencies, 0.99), 1),
        "mean_ms": round(statistics.mean(latencies), 1) if latencies else 0.0,
        # Inside the app; the gap to the client numbers is network, queueing and the HTTP server.
        "server_p50_ms": round(_percentile(server, 0.5), 1),
        "server_p99_ms": round(_percentile(server, 0.99), 1),
        "wall_s": round(duration, 1),
        "health_max_ms": round(max(health), 1) if health else 0.0,
        "rejected_p95_ms": round(_percentile(turned_away, 0.95), 1),
    }


def _fairness(base: str, tokens: dict[str, str], flood: int, endpoint_stream: bool) -> dict:
    """Leon's customers flood, Noah (another tenant) asks a second later; status codes per tenant."""
    outcome: dict[str, dict[str, int]] = {"leon": {}, "iris": {}}
    lock = threading.Lock()

    def ask(tenant: str, email: str, n: int) -> None:
        with httpx.Client(base_url=base, timeout=120) as client:
            text = f"退款多久能到账？（第{n}次）"
            try:
                r = _chat_stream(client, text, tokens[email]) if endpoint_stream else client.post(
                    "/api/chat", json={"content": text}, headers={"Authorization": f"Bearer {tokens[email]}"})
                key = str(r.status_code)
            except httpx.HTTPError as exc:
                key = type(exc).__name__
        with lock:
            outcome[tenant][key] = outcome[tenant].get(key, 0) + 1

    leon = ["customer@leon.demo", "customer2@leon.demo"]
    threads = [threading.Thread(target=ask, args=("leon", leon[n % 2], n)) for n in range(flood)]
    for t in threads:
        t.start()
    time.sleep(1)
    late = [threading.Thread(target=ask, args=("iris", "customer@iris.demo", n)) for n in range(3)]
    for t in late:
        t.start()
    for t in threads + late:
        t.join()
    return {"flood": flood, "leon": outcome["leon"], "iris": outcome["iris"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LoopDesk chat API load test")
    parser.add_argument("--concurrency", default="1,4,16")
    parser.add_argument("--duration", type=float, default=10.0, help="seconds per concurrency level")
    parser.add_argument("--workers", type=int, default=1, help="uvicorn worker processes")
    parser.add_argument("--database-url", help="defaults to a fresh SQLite file")
    parser.add_argument("--cache", choices=["off", "on"], default="off", help="answer cache for repeated FAQs")
    parser.add_argument("--warmup", type=float, default=5.0,
                        help="seconds of unmeasured traffic first, so every worker has loaded jieba and built its index")
    parser.add_argument("--model-latency", type=float,
                        help="seconds per call of a fake OpenAI-compatible model; sends bursts instead (N-158); the admission limit is per process, so with --workers N it is N times larger")
    parser.add_argument("--endpoint", choices=["chat", "stream"], default="chat",
                        help="stream: create a conversation and read the SSE stream, as the console does")
    parser.add_argument("--env", action="append", default=[], metavar="KEY=VALUE",
                        help="extra server environment, e.g. LOOPDESK_MAX_INFLIGHT_RUNS=64")
    parser.add_argument("--fairness", action="store_true",
                        help="with --model-latency: Leon's customers flood, Iris's Noah asks a second later (N-181)")
    parser.add_argument("--report")
    args = parser.parse_args(argv)

    database_url = args.database_url or scratch_database_url("load")
    _prepare_database(database_url)
    extra = dict(item.split("=", 1) for item in args.env)
    burst = args.model_latency is not None
    if burst:
        _, model_url = fake_model.start(args.model_latency)
        shares = {} if args.fairness else {"LOOPDESK_TENANT_INFLIGHT_SHARE": "1", "LOOPDESK_USER_INFLIGHT_RUNS": "1000000"}
        extra = {"LOOPDESK_LLM_PROVIDER": "openai_compat", "LOOPDESK_LLM_BASE_URL": model_url,
                 "LOOPDESK_LLM_API_KEY": "fake", "LOOPDESK_LLM_MODEL": "fake", **shares, **extra}
    elif args.fairness:
        parser.error("--fairness needs --model-latency")
    proc, base = _start_server(database_url, args.workers, 24.0 if args.cache == "on" else 0.0, extra)
    try:
        tokens = {}
        for email in {e for e, _ in QUESTIONS} | ({"customer@iris.demo"} if args.fairness else set()):
            r = httpx.post(f"{base}/api/auth/login", json={"email": email, "password": "loopdesk-demo"}, timeout=10)
            tokens[email] = r.json()["token"]
        if args.warmup > 0 and not burst:  # steady state, not start-up: each worker process warms its own caches
            _run_level(base, tokens, max(int(c) for c in args.concurrency.split(",")), args.warmup)
        levels = [_fairness(base, tokens, int(c), args.endpoint == "stream") if args.fairness
                  else _run_level(base, tokens, int(c), args.duration, burst, args.endpoint == "stream")
                  for c in args.concurrency.split(",")]
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    report = {"database": database_url.split(":", 1)[0], "workers": args.workers, "cache": args.cache,
              "duration_s": args.duration, "warmup_s": args.warmup, "model_latency_s": args.model_latency,
              "endpoint": args.endpoint,
              "env": extra if not burst else {k: v for k, v in extra.items() if "LLM" not in k}, "levels": levels}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.report:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
