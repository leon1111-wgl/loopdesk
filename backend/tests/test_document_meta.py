"""A document's title and visibility change without its text (N-165, N-115): a PDF published by
mistake can be made internal at once instead of deleted, and an administrator who only flips
visibility does not have to send the whole text back."""

from sqlalchemy import select

from app.db import new_session
from app.models import Chunk, Document, Tenant
from app.rag.ingest import ingest_document
from app.services import get_services

from tests.conftest import by_type

OVERRIDE = "Supervisor override code for VIP refunds is 7731."


def _pdf(title: str, pages: list[str], visibility: str = "public") -> dict:
    """What an uploaded PDF leaves in the database: chunks with page numbers."""
    with new_session() as db:
        tenant_id = db.execute(select(Tenant.id).where(Tenant.slug == "leon")).scalar_one()
        document = ingest_document(db, tenant_id=tenant_id, title=title, content="\n\n".join(pages),
                                   embedder=get_services().embedder, visibility=visibility, source=f"{title}.pdf",
                                   pages=pages)
        return {"id": document.id, "version": document.version}


def _final(ask, headers, text) -> str:
    _, events = ask(headers, text)
    return by_type(events, "final")[0]["text"]


def test_a_pdf_published_by_mistake_is_made_internal_and_customers_stop_seeing_it(client, login, ask):
    admin, emma = login("admin@leon.demo"), login("customer@leon.demo")
    doc = _pdf("playbook", ["VIP refunds\n" + OVERRIDE])
    assert "7731" in _final(ask, emma, "supervisor override code for VIP refunds")

    r = client.put(f"/api/kb/documents/{doc['id']}", headers=admin,
                   json={"visibility": "internal", "version": doc["version"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["visibility"] == "internal" and body["version"] == doc["version"] + 1
    assert body["title"] == "playbook" and body["chunk_count"] == 1

    assert "7731" not in _final(ask, emma, "supervisor override code for VIP refunds")
    assert "7731" in _final(ask, admin, "supervisor override code for VIP refunds")  # staff still find it
    with new_session() as db:  # the page numbers and text are kept
        chunk = db.execute(select(Chunk).where(Chunk.document_id == doc["id"])).scalars().one()
        assert chunk.page == 1 and "7731" in chunk.text
    entry = client.get("/api/audit?category=kb", headers=admin).json()[0]
    assert entry["action"] == "kb.document.update"
    assert entry["detail"]["changes"] == {"visibility": ["public", "internal"]}
    assert entry["detail"]["diff"] == ""


def test_the_cached_answer_from_a_document_made_internal_is_not_replayed(client, login, ask):
    admin, emma, jack = login("admin@leon.demo"), login("customer@leon.demo"), login("customer2@leon.demo")
    doc = _pdf("playbook", ["VIP refunds\n" + OVERRIDE])
    question = "supervisor override code for VIP refunds"
    assert "7731" in _final(ask, emma, question)
    _, events = ask(jack, question)
    assert by_type(events, "final")[0]["meta"].get("cached")  # stored and replayed before the change
    client.put(f"/api/kb/documents/{doc['id']}", headers=admin, json={"visibility": "internal", "version": 1})
    _, events = ask(jack, question)
    final = by_type(events, "final")[0]
    assert "7731" not in final["text"] and not final["meta"].get("cached")


def test_the_text_of_a_pdf_still_cannot_be_edited_in_place(client, login):
    admin = login("admin@leon.demo")
    doc = _pdf("playbook", ["VIP refunds\n" + OVERRIDE])
    r = client.put(f"/api/kb/documents/{doc['id']}", headers=admin,
                   json={"content": "VIP refunds\nno code", "version": doc["version"]})
    assert r.status_code == 400
    assert "上传新版本" in r.json()["detail"] and "可见范围" in r.json()["detail"]
    # the stored text sent back unchanged (an editor that always sends every field) is a metadata edit
    with new_session() as db:
        content = db.get(Document, doc["id"]).content
    r = client.put(f"/api/kb/documents/{doc['id']}", headers=admin,
                   json={"title": "VIP 退款手册", "content": content, "visibility": "internal", "version": 1})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "VIP 退款手册" and r.json()["visibility"] == "internal"


def test_only_the_visibility_of_a_text_document_changes_without_its_text(client, login):
    admin = login("admin@leon.demo")
    created = client.post("/api/kb/documents/text", headers=admin, json={
        "title": "坐席手册", "content": "## 退款\n- 退款预计1-3个工作日到账。", "visibility": "public"}).json()
    r = client.put(f"/api/kb/documents/{created['id']}", headers=admin,
                   json={"visibility": "internal", "version": created["version"]})
    assert r.status_code == 200, r.text
    detail = client.get(f"/api/kb/documents/{created['id']}", headers=admin).json()
    assert detail["visibility"] == "internal" and detail["content"] == "## 退款\n- 退款预计1-3个工作日到账。"


def test_an_edit_that_changes_nothing_says_so(client, login):
    admin = login("admin@leon.demo")
    doc = _pdf("playbook", ["VIP refunds\n" + OVERRIDE])
    r = client.put(f"/api/kb/documents/{doc['id']}", headers=admin, json={"version": doc["version"]})
    assert r.status_code == 422
    assert r.json()["detail"] == "请至少修改标题、内容或可见范围中的一项"


def test_a_stale_version_is_refused_for_a_metadata_edit_too(client, login):
    admin = login("admin@leon.demo")
    doc = _pdf("playbook", ["VIP refunds\n" + OVERRIDE])
    assert client.put(f"/api/kb/documents/{doc['id']}", headers=admin,
                      json={"visibility": "internal", "version": 1}).status_code == 200
    r = client.put(f"/api/kb/documents/{doc['id']}", headers=admin, json={"visibility": "public", "version": 1})
    assert r.status_code == 409
    assert client.get(f"/api/kb/documents/{doc['id']}", headers=admin).json()["visibility"] == "internal"


def test_making_a_document_public_scans_it_again_under_the_public_rules(client, login):
    """Instructions to staff are fine in an internal handbook and held back in public knowledge."""
    admin = login("admin@leon.demo")
    doc = _pdf("坐席手册", ["坐席话术\n坐席应告知顾客预计到账时间。"], visibility="internal")
    with new_session() as db:
        assert not db.execute(select(Chunk.flagged_injection).where(Chunk.document_id == doc["id"])).scalar_one()
    r = client.put(f"/api/kb/documents/{doc['id']}", headers=admin, json={"visibility": "public", "version": 1})
    assert r.status_code == 200, r.text
    assert r.json()["flagged_chunks"] == 1
    held = client.get(f"/api/kb/documents/{doc['id']}", headers=admin).json()["chunks"][0]
    assert held["flagged_injection"] and "agent_directive" in {x["code"] for x in held["flag_reasons"]}
    # and back to internal: indexed again
    r = client.put(f"/api/kb/documents/{doc['id']}", headers=admin, json={"visibility": "internal", "version": 2})
    assert r.json()["flagged_chunks"] == 0


def test_agents_cannot_change_visibility(client, login):
    doc = _pdf("playbook", ["VIP refunds\n" + OVERRIDE])
    r = client.put(f"/api/kb/documents/{doc['id']}", headers=login("agent@leon.demo"),
                   json={"visibility": "internal", "version": 1})
    assert r.status_code == 403


def test_a_released_chunk_stays_released_after_a_round_trip_through_internal(client, login):
    admin = login("admin@leon.demo")
    doc = _pdf("坐席手册", ["坐席话术\n坐席应告知顾客预计到账时间。"])
    chunk = client.get(f"/api/kb/documents/{doc['id']}", headers=admin).json()["chunks"][0]
    assert chunk["flagged_injection"]
    assert client.post(f"/api/kb/chunks/{chunk['id']}/release", headers=admin).status_code == 200
    for version, visibility in ((1, "internal"), (2, "public")):
        r = client.put(f"/api/kb/documents/{doc['id']}", headers=admin, json={"visibility": visibility, "version": version})
        assert r.status_code == 200, r.text
        assert r.json()["flagged_chunks"] == 0  # the review still covers what the public scan finds


def test_a_save_without_changes_takes_no_version(client, login):
    admin = login("admin@leon.demo")
    created = client.post("/api/kb/documents/text", headers=admin, json={
        "title": "坐席手册", "content": "## 退款\n- 退款预计1-3个工作日到账。", "visibility": "public"}).json()
    r = client.put(f"/api/kb/documents/{created['id']}", headers=admin, json={
        "title": "坐席手册", "content": "## 退款\n- 退款预计1-3个工作日到账。", "visibility": "public", "version": 1})
    assert r.status_code == 422 and r.json()["detail"] == "请至少修改标题、内容或可见范围中的一项"
    assert client.get(f"/api/kb/documents/{created['id']}", headers=admin).json()["version"] == 1


def test_a_title_change_is_audited(client, login):
    admin = login("admin@leon.demo")
    doc = _pdf("playbook", ["VIP refunds\n" + OVERRIDE], visibility="internal")
    r = client.put(f"/api/kb/documents/{doc['id']}", headers=admin, json={"title": "VIP 退款手册", "version": 1})
    assert r.status_code == 200 and r.json()["visibility"] == "internal"
    entry = client.get("/api/audit?category=kb", headers=admin).json()[0]
    assert entry["detail"]["changes"] == {"title": ["playbook", "VIP 退款手册"]}
    for bad in ({"title": "", "version": 2}, {"content": "", "version": 2}, {"visibility": "secret", "version": 2}):
        assert client.put(f"/api/kb/documents/{doc['id']}", headers=admin, json=bad).status_code == 422
