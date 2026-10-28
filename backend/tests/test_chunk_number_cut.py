"""N-211: a length cut never lands inside a number, an amount, a Latin word or an order id."""

import re

from app.rag.chunking import chunk_markdown

# The nitpick reproduction: 290 characters of padding with no punctuation, then the price
# sentence, so a cut at exactly 300 fell between "每台19" and "9元".
PAD = ("本店支持多种售后服务方式包括上门取件到店退换以及快递寄回等多种渠道顾客可以根据自己的实际情况进行选择" * 8)[:290]
PRICE = "空调安装费为每台199元含两米铜管超出部分每米收取60元由安装师傅现场收取并开具收据"
GLUED = re.compile(r"[0-9A-Za-z.%¥]")


def _doc(pad: str = PAD) -> str:
    return "# 大家电安装费用\n\n## 安装费用\n" + pad + PRICE + "\n"


def _no_glued_edge(chunks):
    for a, b in zip(chunks, chunks[1:]):
        assert not (GLUED.match(a.text[-1]) and GLUED.match(b.text[0])), (a.text[-10:], b.text[:10])
    return all(not GLUED.match(c.text[-1]) for c in chunks[:-1])


def test_the_cut_backs_off_to_a_position_outside_the_number():
    chunks = chunk_markdown(_doc())
    assert len(chunks) == 2 and max(len(c.text) for c in chunks) <= 300
    assert _no_glued_edge(chunks)
    assert not chunks[1].text[0].isdigit()
    assert not any(c.text.endswith("每台19") for c in chunks)
    # The windows overlap, so the sentence the cut went through is whole in one of them.
    assert any(PRICE in c.text for c in chunks)


def test_no_cut_position_splits_a_number_word_or_order_id():
    # Slide the glued runs across the cap one character at a time.
    for tail in ("199元", "1,299元", "SO20261008002", "iPhone15", "12.5%", "10:30"):
        for shift in range(-12, 4):
            chunks = chunk_markdown("## 规则\n" + "很" * (300 - len(tail) + shift) + tail + "后面还有很长的说明" * 10)
            assert max(len(c.text) for c in chunks) <= 300
            assert any(tail in c.text for c in chunks), (tail, shift)
            assert _no_glued_edge(chunks), (tail, shift)


def test_a_glued_run_longer_than_the_cap_still_respects_the_cap():
    chunks = chunk_markdown("## 日志\n" + "a1" * 400)
    assert max(len(c.text) for c in chunks) <= 300 and len("".join(c.text for c in chunks)) >= 800


def test_the_answer_is_not_a_half_number_and_no_false_conflict(client, login):
    admin = login("admin@leon.demo")
    first = client.post("/api/kb/documents/text", headers=admin, json={
        "title": "大家电安装费用（短）", "content": _doc(PAD[:150]), "visibility": "public"}).json()
    assert first["conflicts"] == 0
    second = client.post("/api/kb/documents/text", headers=admin, json={
        "title": "大家电安装费用", "content": _doc(), "visibility": "public"}).json()
    assert second["conflicts"] == 0  # was {"元": {"this": ["60元", "9元"], "other": ["199元", "60元"]}}
    assert client.get("/api/kb/conflicts", headers=admin).json() == []
    body = client.post("/api/chat", json={"content": "空调安装费多少钱"},
                       headers=login("customer2@leon.demo")).json()
    assert "199元" in body["answer"] and "每台19[" not in body["answer"], body["answer"]


def test_a_comma_separated_number_list_is_cut_after_a_comma():
    row = "型号" + ",".join(str(12345 + i * 7) for i in range(80))  # one glued run, 478 characters
    chunks = chunk_markdown("## 型号\n" + row)
    assert max(len(c.text) for c in chunks) <= 300
    assert all(c.text[-1] == "," for c in chunks[:-1]), [c.text[-6:] for c in chunks]
    assert set(re.findall(r"\d+", "".join(c.text for c in chunks))) == set(re.findall(r"\d+", row))


def test_a_glued_run_after_a_cut_does_not_repeat_the_overlap():
    chunks = chunk_markdown("## 日志\n" + "很" * 100 + "a" * 300 + "很" * 100)
    lines = [line for c in chunks for line in c.text.splitlines()]
    assert not any(a.endswith(b) or b.endswith(a) for a, b in zip(lines, lines[1:])), [len(x) for x in lines]
