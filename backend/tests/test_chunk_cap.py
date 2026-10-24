"""N-109: a chunk has a hard length cap, whatever the punctuation of the source text."""

from app.rag.chunking import chunk_markdown, chunk_pages

# The nitpick reproduction: 9,903 characters, commas only, one full stop at the very end.
LONG = ("本店售后规则如下" + "，".join(f"第{i}类商品的退换货按照对应品类的通用规则执行" for i in range(1, 400))
        + "，其中智能手表的电池保修期为18个月，屏幕不在保修范围内。")


def test_a_comma_only_paragraph_is_cut_at_clauses_within_the_cap():
    chunks = chunk_markdown(LONG)
    assert len(LONG) == 9903 and len(chunks) > 30
    assert max(len(c.text) for c in chunks) <= 300
    assert "".join(c.text.replace("\n", "") for c in chunks).count("智能手表的电池保修期为18个月") >= 1
    # Clause cuts keep words whole: every chunk ends at a clause mark or the final full stop.
    assert all(c.text[-1] in "，。" for c in chunks)


def test_text_without_any_punctuation_is_cut_by_length():
    pdf_text = "页面抽取文本没有任何标点" * 100  # 1,200 characters, like OCR output
    chunks = chunk_pages([pdf_text], max_chars=300)
    assert [len(c.text) for c in chunks] == [300, 300, 300, 300]
    assert all(c.page == 1 for c in chunks)


def test_a_long_list_item_or_table_row_is_cut_too():
    row = "| " + " | ".join(f"第{i}列说明" for i in range(120)) + " |"
    chunks = chunk_markdown(f"## 表\n{row}\n- 短条目。")
    assert max(len(c.text) for c in chunks) <= 300


def test_overlap_never_pushes_a_chunk_past_the_cap():
    # Two 109-character sentences in a row: carrying the first into the next chunk made 219.
    body = "".join(f"第{i}句" + "这是一句很长的说明" * 12 + "。" for i in range(10))
    chunks = chunk_markdown(f"## 规则\n{body}", max_chars=120, overlap=1)
    assert len(chunks) == 10 and max(len(c.text) for c in chunks) <= 120
    short = "".join(f"第{i}条短规则。" for i in range(30))
    chunks = chunk_markdown(f"## 规则\n{short}", max_chars=60, overlap=1)
    assert chunks[1].text.startswith(chunks[0].text.splitlines()[-1])  # short ones still overlap


def test_the_long_paragraph_no_longer_comes_back_as_a_9906_character_answer(client, login):
    admin = login("admin@leon.demo")
    doc = client.post("/api/kb/documents/text", headers=admin, json={
        "title": "超长单段", "content": LONG, "visibility": "public"}).json()
    detail = client.get(f"/api/kb/documents/{doc['id']}", headers=admin).json()
    assert len(detail["chunks"]) > 30
    assert max(len(c["text"]) for c in detail["chunks"]) <= 300
    body = client.post("/api/chat", json={"content": "智能手表电池保修多久"},
                       headers=login("customer2@leon.demo")).json()
    assert "18个月" in body["answer"]
    assert len(body["answer"]) < 600, len(body["answer"])
