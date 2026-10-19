"""Publishing two knowledge patches at once must not drop either of them."""

import threading

from sqlalchemy import select

from app.db import new_session
from app.flywheel import service
from app.models import Document, KBPatch, User
from app.services import get_services
from tests.conftest import by_type


def _propose(client, ask, login, question, correction):
    _, events = ask(login("customer@leon.demo"), question)
    message_id = by_type(events, "final")[0]["message_id"]
    r = client.post(f"/api/messages/{message_id}/feedback", headers=login("agent@leon.demo"),
                    json={"rating": "correction", "correction": correction, "reason_category": "knowledge_gap"})
    return r.json()["patch_id"]


def test_concurrent_patch_publishing_keeps_every_applied_patch(client, ask, login, monkeypatch):
    first = _propose(client, ask, login, "以旧换新活动怎么参加？", "以旧换新仅限手机和平板，审核通过后可抵扣50-800元。")
    second = _propose(client, ask, login, "门店在哪里？", "Leon优选是纯线上商城，没有线下门店。")

    # Worst interleaving: both publishers have read the patch document before either writes.
    # Once publishing is serialised the second publisher cannot reach the barrier while the
    # first holds the lock, so the barrier times out; the old code reached it from both sides.
    barrier = threading.Barrier(2, timeout=1)
    real_ingest = service.ingest_document

    def ingest_after_both_read(*args, **kwargs):
        try:
            barrier.wait()
        except threading.BrokenBarrierError:
            pass
        return real_ingest(*args, **kwargs)

    monkeypatch.setattr(service, "ingest_document", ingest_after_both_read)
    errors = []

    def publish(patch_id):
        with new_session() as db:
            try:
                admin = db.execute(select(User).where(User.email == "admin@leon.demo")).scalar_one()
                service.apply_patch(db, db.get(KBPatch, patch_id), admin, get_services().embedder)
            except Exception as exc:  # a loser may fail; it must not be marked applied
                errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=publish, args=(p,)) for p in (first, second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    with new_session() as db:
        document = db.execute(select(Document).where(Document.source == service.PATCH_DOC_SOURCE)).scalar_one()
        for patch in db.execute(select(KBPatch).where(KBPatch.id.in_([first, second]))).scalars():
            if patch.status == "applied":
                assert patch.content.strip() in document.content, f"patch {patch.id} is applied but missing: {errors}"
        applied = db.execute(select(KBPatch).where(KBPatch.id.in_([first, second]), KBPatch.status == "applied")).all()
        assert len(applied) == 2 and not errors, errors
        assert len(db.execute(select(Document).where(Document.source == service.PATCH_DOC_SOURCE)).all()) == 1
