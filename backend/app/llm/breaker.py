"""Circuit breaker per model, per process (review round 4, N-031).

Closed: calls go through and failures are counted in a sliding window. ``llm_breaker_failures``
failures within ``llm_breaker_window_seconds`` open it: the model is not called for
``llm_breaker_cooldown_seconds``, so while a model is down each customer gets the degraded answer
at once instead of waiting out retries and timeouts. After the cool-down one trial call goes
through (half-open): success closes the breaker, failure opens it again.

Each worker process learns on its own; with N workers a dead model costs up to N times the
threshold before every worker stops calling it, which is cheap compared with sharing state.
"""

from __future__ import annotations

import threading
import time


class CircuitBreaker:
    def __init__(self, settings, clock=time.monotonic):
        self.settings = settings  # read on every call, so thresholds can be changed at run time
        self.clock = clock
        self._lock = threading.Lock()
        self._failures: dict[str, list[float]] = {}
        self._opened: dict[str, float] = {}
        self._trial: set[str] = set()

    def allow(self, key: str) -> bool:
        if self.settings.llm_breaker_failures <= 0:
            return True
        with self._lock:
            opened = self._opened.get(key)
            if opened is None:
                return True
            if self.clock() - opened < self.settings.llm_breaker_cooldown_seconds or key in self._trial:
                return False
            self._trial.add(key)  # half-open: this caller makes the one trial call
            return True

    def record(self, key: str, ok: bool) -> None:
        with self._lock:
            now = self.clock()
            if ok:
                self._failures.pop(key, None)
                self._opened.pop(key, None)
                self._trial.discard(key)
                return
            if key in self._trial:  # the trial failed: open for another cool-down
                self._trial.discard(key)
                self._opened[key] = now
                return
            window = self.settings.llm_breaker_window_seconds
            recent = [t for t in self._failures.get(key, []) if now - t < window] + [now]
            if 0 < self.settings.llm_breaker_failures <= len(recent):
                self._opened[key] = now
                recent = []
            self._failures[key] = recent

    def state(self, key: str) -> str:
        with self._lock:
            if key not in self._opened:
                return "closed"
            return "half_open" if key in self._trial else "open"
