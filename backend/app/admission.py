"""How many agent runs one process holds open at once (N-158).

A run spends most of its time waiting on the model, so CPU says nothing about load: a
process at 3% CPU can be out of worker threads and database connections. Each chat request
takes a slot before it starts a run; when every slot is taken it is turned away at once with
503 and Retry-After, instead of queueing for 30 s and failing with a pool timeout. The slot
count is also the autoscaling signal (``loopdesk_inflight_runs`` in /metrics).

The thread pool is sized from the same number (``main.py``), so an admitted request never
queues for a thread. Queueing was what broke before: a request waiting for a thread had
already run its dependencies, and the user lookup left a database connection checked out;
once more than fifteen waited, the pool (5 + 10) ran dry and the rest failed after 30 s.
"""

from __future__ import annotations

import logging
import threading

from fastapi import HTTPException, status

log = logging.getLogger("loopdesk.admission")
OVERLOADED = "当前咨询人数较多，请稍后再试。"


class Admission:
    def __init__(self, limit: int, retry_after: int = 5):
        self.limit = limit
        self.retry_after = retry_after
        self.inflight = 0
        self.rejected = 0  # since the process started
        self._lock = threading.Lock()

    def enter(self) -> None:
        """Take a slot, or raise 503. Every successful call needs exactly one ``leave()``."""
        with self._lock:
            if self.inflight >= self.limit:
                self.rejected += 1
                raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, OVERLOADED,
                                    headers={"Retry-After": str(self.retry_after)})
            self.inflight += 1

    def leave(self) -> None:
        with self._lock:
            if self.inflight == 0:  # a second leave() for one enter(): a bug, not a reason to go negative
                log.error("admission slot released twice")
                return
            self.inflight -= 1


_admission: Admission | None = None


def configure(limit: int, retry_after: int = 5) -> Admission:
    global _admission
    _admission = Admission(limit, retry_after)
    return _admission


def get_admission() -> Admission:
    global _admission
    if _admission is None:  # code paths that never built the app (scripts, some tests)
        from .config import get_settings

        _admission = Admission(get_settings().max_inflight_runs)
    return _admission
