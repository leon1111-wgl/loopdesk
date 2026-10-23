"""A stand-in for a slow OpenAI-compatible model, for load tests (N-158).

Every POST waits ``latency`` seconds, then streams one short sentence with a finish_reason
and usage. The offline mock answers in microseconds, so it cannot show how many answers a
process holds open at once; a real model is slow to answer and cheap to wait for, and that
is what this imitates.

    python -m app.evals.fake_model --port 8799 --latency 6
"""

from __future__ import annotations

import argparse
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ANSWER = "您好，这个问题我已经记录，稍后为您处理。"


def _handler(latency: float) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self) -> None:  # noqa: N802 - http.server's naming
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            time.sleep(latency)
            chunks = [
                {"choices": [{"index": 0, "delta": {"content": ANSWER}}]},
                {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                {"choices": [], "usage": {"prompt_tokens": 400, "completion_tokens": 20}},
            ]
            body = "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks) + "data: [DONE]\n\n"
            data = body.encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args) -> None:
            pass

    return Handler


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 1024  # read in the constructor: a burst of hundreds of connections must not be refused


def start(latency: float, port: int = 0) -> tuple[ThreadingHTTPServer, str]:
    """Serve in a daemon thread; returns the server and its /v1 base URL."""
    server = _Server(("127.0.0.1", port), _handler(latency))
    threading.Thread(target=server.serve_forever, name="fake-model", daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}/v1"


def main() -> None:
    parser = argparse.ArgumentParser(description="slow OpenAI-compatible model for load tests")
    parser.add_argument("--port", type=int, default=8799)
    parser.add_argument("--latency", type=float, default=6.0)
    args = parser.parse_args()
    server, base = start(args.latency, args.port)
    print(f"fake model at {base}, {args.latency}s per call")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()


if __name__ == "__main__":
    main()
