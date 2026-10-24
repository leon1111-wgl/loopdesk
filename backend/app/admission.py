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

The slots are shared fairly (N-181). One tenant may hold at most ``tenant_share`` of them, so
a tenant whose customers flood the chat fills its own share and the rest stay open for every
other tenant; it gets 503 like a full process would. One user may hold ``per_user`` at once:
a person waits for an answer before asking the next question, so more than a few at once is a
script, a stuck client or someone pressing send in a loop, and it gets 429 (their own doing,
not the service being busy). A single-tenant deployment sets the share to 1.
"""

from __future__ import annotations

import logging
import math
import threading

from fastapi import HTTPException, status

log = logging.getLogger("loopdesk.admission")
OVERLOADED = "当前咨询人数较多，请稍后再试。"
USER_BUSY = "您还有回答正在生成，请等它完成后再发送。"

Slot = tuple  # (tenant_id, user_id) as given to enter(); hand it back to leave()


class Admission:
    def __init__(self, limit: int, retry_after: int = 5, tenant_share: float = 1.0, per_user: int | None = None):
        self.limit = limit
        self.retry_after = retry_after
        self.tenant_limit = max(1, math.floor(limit * tenant_share + 1e-9))  # 0.7 * 90 is 62.999…
        self.user_limit = max(1, per_user or limit)
        self.inflight = 0
        self.by_tenant: dict = {}
        self.by_user: dict = {}
        self.rejections: dict[tuple[str, str], int] = {}  # (reason, tenant) -> count since the process started
        self._lock = threading.Lock()

    @property
    def rejected(self) -> int:
        return sum(self.rejections.values())

    def enter(self, tenant_id=None, user_id=None) -> Slot:
        """Take a slot, or raise 429 (this user) / 503 (process or tenant full). Every successful
        call needs exactly one ``leave()`` with the slot it returned."""
        with self._lock:
            if user_id is not None and self.by_user.get(user_id, 0) >= self.user_limit:
                self._reject("user", tenant_id, status.HTTP_429_TOO_MANY_REQUESTS, USER_BUSY)
            if self.inflight >= self.limit:
                self._reject("process", tenant_id)
            if tenant_id is not None and self.by_tenant.get(tenant_id, 0) >= self.tenant_limit:
                self._reject("tenant", tenant_id)
            self.inflight += 1
            if tenant_id is not None:
                self.by_tenant[tenant_id] = self.by_tenant.get(tenant_id, 0) + 1
            if user_id is not None:
                self.by_user[user_id] = self.by_user.get(user_id, 0) + 1
            return (tenant_id, user_id)

    def _reject(self, reason: str, tenant_id, code: int = status.HTTP_503_SERVICE_UNAVAILABLE,
                detail: str = OVERLOADED) -> None:
        key = (reason, "" if tenant_id is None else str(tenant_id))
        self.rejections[key] = self.rejections.get(key, 0) + 1
        raise HTTPException(code, detail, headers={"Retry-After": str(self.retry_after)})

    def leave(self, slot: Slot) -> None:
        tenant_id, user_id = slot
        with self._lock:
            if self.inflight == 0:  # a second leave() for one enter(): a bug, not a reason to go negative
                log.error("admission slot released twice")
                return
            self.inflight -= 1
            for counts, key in ((self.by_tenant, tenant_id), (self.by_user, user_id)):
                if key is None:
                    continue
                if counts.get(key, 0) == 0:  # a slot that was not this one: the shares would drift
                    log.error("admission slot %r released without being held", slot)
                    continue
                counts[key] -= 1
                if not counts[key]:
                    del counts[key]  # keep the maps as small as what is running now

    def snapshot(self) -> tuple[dict, dict]:
        """Copies of the per-tenant counts and the rejections, for /metrics."""
        with self._lock:
            return dict(self.by_tenant), dict(self.rejections)


_admission: Admission | None = None


def configure(limit: int, retry_after: int = 5, tenant_share: float = 1.0, per_user: int | None = None) -> Admission:
    global _admission
    _admission = Admission(limit, retry_after, tenant_share, per_user)
    return _admission


def get_admission() -> Admission:
    global _admission
    if _admission is None:  # code paths that never built the app (scripts, some tests)
        from .config import get_settings

        s = get_settings()
        _admission = Admission(s.max_inflight_runs, tenant_share=s.tenant_inflight_share,
                               per_user=s.user_inflight_runs)
    return _admission
