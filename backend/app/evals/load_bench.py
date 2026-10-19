"""Load-test baseline for the chat API.

Starts a real uvicorn server (offline model, its own database), logs in, then drives N
concurrent clients for a fixed time against POST /api/chat, once per concurrency level.

    python -m app.evals.load_bench --concurrency 1,4,16 --duration 10 --report perf.json
    python -m app.evals.load_bench --database-url postgresql+psycopg://... --workers 4

What it measures is the platform around the model (routing, retrieval, tools, verifier,
tracing, database writes), because the offline model answers instantly. With a real model
the model latency dominates and the numbers below become a floor, not a forecast.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

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


def _start_server(database_url: str, workers: int, cache_hours: float) -> tuple[subprocess.Popen, str]:
    port = _free_port()
    env = {
        **os.environ,
        "LOOPDESK_DATABASE_URL": database_url,
        "LOOPDESK_SEED_DEMO": "1",
        "LOOPDESK_SEED_DEMO_TRAFFIC": "0",
        "LOOPDESK_RATE_LIMIT_PER_MINUTE": "1000000",
        "LOOPDESK_MOCK_STREAM_DELAY": "0",
        "LOOPDESK_LOG_LEVEL": "WARNING",
        "LOOPDESK_ANSWER_CACHE_TTL_HOURS": str(cache_hours),
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


def _run_level(base: str, tokens: dict[str, str], concurrency: int, duration: float) -> dict:
    latencies: list[float] = []
    errors: dict[str, int] = {}
    lock = threading.Lock()
    deadline = time.monotonic() + duration

    def worker(offset: int) -> None:
        with httpx.Client(base_url=base, timeout=30) as client:
            i = offset
            while time.monotonic() < deadline:
                email, question = QUESTIONS[i % len(QUESTIONS)]
                i += 1
                started = time.perf_counter()
                try:
                    r = client.post("/api/chat", json={"content": question},
                                    headers={"Authorization": f"Bearer {tokens[email]}"})
                    key = None if r.status_code == 200 else str(r.status_code)
                except httpx.HTTPError as exc:
                    key = type(exc).__name__
                elapsed = (time.perf_counter() - started) * 1000
                with lock:
                    if key is None:
                        latencies.append(elapsed)
                    else:
                        errors[key] = errors.get(key, 0) + 1

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(concurrency)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
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
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="LoopDesk chat API load test")
    parser.add_argument("--concurrency", default="1,4,16")
    parser.add_argument("--duration", type=float, default=10.0, help="seconds per concurrency level")
    parser.add_argument("--workers", type=int, default=1, help="uvicorn worker processes")
    parser.add_argument("--database-url", help="defaults to a fresh SQLite file")
    parser.add_argument("--cache", choices=["off", "on"], default="off", help="answer cache for repeated FAQs")
    parser.add_argument("--report")
    args = parser.parse_args(argv)

    database_url = args.database_url or f"sqlite:///{Path(tempfile.mkdtemp()) / 'load.db'}"
    proc, base = _start_server(database_url, args.workers, 24.0 if args.cache == "on" else 0.0)
    try:
        tokens = {}
        for email in {e for e, _ in QUESTIONS}:
            r = httpx.post(f"{base}/api/auth/login", json={"email": email, "password": "loopdesk-demo"}, timeout=10)
            tokens[email] = r.json()["token"]
        levels = [_run_level(base, tokens, int(c), args.duration) for c in args.concurrency.split(",")]
    finally:
        proc.terminate()
        proc.wait(timeout=10)
    report = {"database": database_url.split(":", 1)[0], "workers": args.workers, "cache": args.cache,
              "duration_s": args.duration, "levels": levels}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.report:
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
