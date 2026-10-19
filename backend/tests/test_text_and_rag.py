from app.rag.chunking import chunk_markdown, parse_upload
from app.rag.embeddings import HashingEmbedder
from app.textutil import extract_numbers, strip_citations, tokenize


def test_tokenizer_drops_question_words_and_keeps_domain_terms():
    tokens = tokenize("退款多久能到账？")
    assert "退款" in tokens and "到账" in tokens
    assert "多久" not in tokens


def test_numbers_ignore_identifiers_and_thousand_separators():
    assert extract_numbers("订单 SO20261008002 金额 ¥1,299.00，预计1-3个工作日") == [1299.0, 1.0, 3.0]


def test_strip_citations():
    assert strip_citations("退款原路退回[2]。") == ("退款原路退回。", [2])


def test_chunks_keep_heading_path_and_never_cross_sections():
    doc = "# 政策\n\n## 退货\n- 7天内可退货。\n\n## 到账\n- 1-3个工作日到账。\n"
    chunks = chunk_markdown(doc)
    assert [c.heading for c in chunks] == ["政策 > 退货", "政策 > 到账"]
    assert "到账" not in chunks[0].text


def test_long_section_is_split_with_overlap():
    body = "".join(f"第{i}条规则说明内容较长需要切分。" for i in range(40))
    chunks = chunk_markdown(f"## 规则\n{body}", max_chars=120, overlap=1)
    assert len(chunks) > 3
    last_sentence = chunks[0].text.splitlines()[-1]
    assert chunks[1].text.startswith(last_sentence)


def test_parse_upload_handles_gbk_text():
    text, pages = parse_upload("faq.txt", "退货政策".encode("gb18030"))
    assert text == "退货政策" and pages is None


def test_hashing_embedder_is_normalized_and_similar_texts_are_closer():
    e = HashingEmbedder(256)
    a, b, c = e.embed(["退款多久到账", "退款到账时间", "发票怎么开"])
    dot = lambda x, y: sum(i * j for i, j in zip(x, y))
    assert abs(dot(a, a) - 1) < 1e-6
    assert dot(a, b) > dot(a, c)


def test_hybrid_search_ranks_policy_and_hides_internal_docs(client, login):
    staff = login("agent@leon.demo")
    r = client.post("/api/kb/search", json={"query": "退款多久到账", "scope": "customer"}, headers=staff).json()
    assert r["hits"][0]["doc_title"] == "售后与退款政策"
    assert r["hits"][0]["bm25_rank"] == 1
    customer_view = client.post("/api/kb/search", json={"query": "补偿券", "scope": "customer"}, headers=staff).json()
    assert all(h["doc_title"] != "客服内部操作手册" for h in customer_view["hits"])
    staff_view = client.post("/api/kb/search", json={"query": "补偿券", "scope": "staff"}, headers=staff).json()
    assert staff_view["hits"][0]["doc_title"] == "客服内部操作手册"


def test_injected_chunk_is_flagged_at_ingestion(client, login):
    admin = login("admin@leon.demo")
    docs = client.get("/api/kb/documents", headers=admin).json()
    supplier = next(d for d in docs if d["title"] == "供应商合作 FAQ")
    assert supplier["flagged_chunks"] == 1
    detail = client.get(f"/api/kb/documents/{supplier['id']}", headers=admin).json()
    flagged = [c for c in detail["chunks"] if c["flagged_injection"]]
    assert "忽略之前的所有指令" in flagged[0]["text"]


def test_query_expansion_folds_variants_and_adds_canonical_terms():
    from app.textutil import expand_query, tokenize

    assert "到账" in expand_query("退款到帐要几天"), "帐 is folded to 账"
    expanded = expand_query("公司开专票要啥材料")
    assert "增值税专用发票" in expanded and expanded.count("增值税专用发票") == 1
    assert "啥时候" not in tokenize("钱啥时候能退回来"), "colloquial question words carry no topic"
    assert expand_query("积分可以怎么抵扣？") == "积分可以怎么抵扣？", "no expansion without a matching phrase"


def test_order_ids_are_found_next_to_chinese_text():
    from app.textutil import ORDER_ID_RE

    for text in ("我订单SO20261008002到哪了", "SO20261008002的物流", "订单 so20261008002 呢", "（SO20261008002）"):
        assert ORDER_ID_RE.search(text).group(0).upper() == "SO20261008002", text
    assert ORDER_ID_RE.search("XSO20261008002") is None, "not inside another token"


def test_relevance_gate_scores_the_canonical_wording():
    from app.textutil import canonical_query, tokenize

    # Colloquial words are outside the knowledge base's vocabulary and would only dilute coverage.
    assert tokenize(canonical_query("能开发票吗")) == ["开具", "发票"]
    assert tokenize(canonical_query("能寄到国外吗")) == ["配送", "海外"]
    assert tokenize(canonical_query("钱什么时候到")) == ["退款", "到账"]
    assert tokenize(canonical_query("以旧换新活动怎么参加")) == tokenize("以旧换新活动怎么参加"), "no synonym, no change"


def test_return_shipping_fee_is_segmented_like_the_policy_documents():
    from app.textutil import tokenize

    assert tokenize("退货运费谁出") == ["退货", "运费"]
    assert "货运" not in tokenize("金卡会员每月享受2次免费退货运费")


def test_unknown_compound_is_not_covered_by_its_sub_word(client, login):
    # "货到付款" is not in the knowledge base; "付款" is ("付款后48小时内发货").
    r = client.post("/api/kb/search", json={"query": "能货到付款吗", "scope": "customer"}, headers=login("admin@leon.demo"))
    assert not any(h["passes_threshold"] for h in r.json()["hits"]), "a sub-word alone must not carry an unknown compound"
    r = client.post("/api/kb/search", json={"query": "付款后多久发货", "scope": "customer"}, headers=login("admin@leon.demo"))
    assert any(h["passes_threshold"] for h in r.json()["hits"]), "the same sub-word on its own still counts"


def test_tokenize_cache_hands_out_copies():
    from app.textutil import tokenize

    first = tokenize("退款多久能到账")
    first.append("被调用方改掉")
    assert tokenize("退款多久能到账") == ["退款", "到账"], "a caller mutating its list must not poison the cache"
    long_text = "退款到账" * 600  # longer than the cache limit: still tokenized, just not cached
    assert tokenize(long_text)[:2] == ["退款", "到账"]


def _scan_bm25(index, q_tokens, allowed, k, k1=1.5, b=0.75):
    """The original full scan, kept as the reference the inverted index must match."""
    scores, unique = [], set(q_tokens)
    for i, c in enumerate(index.chunks):
        if c.visibility not in allowed:
            continue
        score = 0.0
        for t in unique:
            tf = c.tf.get(t)
            if tf:
                score += index.idf(t) * tf * (k1 + 1) / (tf + k1 * (1 - b + b * c.length / (index.avgdl or 1)))
        if score > 0:
            scores.append((i, score))
    scores.sort(key=lambda x: -x[1])
    return scores[:k]


def _scan_vector(index, qvec, allowed, k):
    scores = [(i, sum(a * b for a, b in zip(qvec, index.matrix[i].tolist())))
              for i, c in enumerate(index.chunks) if c.visibility in allowed]
    scores.sort(key=lambda x: -x[1])
    return [s for s in scores[:k] if s[1] > 1e-6]


def test_the_index_ranks_exactly_like_a_full_scan(client):
    # BM25 now walks an inverted index and vector scores are one matrix product (20,000 chunks:
    # 391 ms -> see docs/PERFORMANCE.md). Rankings must not move: same chunks, same order.
    from sqlalchemy import select

    from app.db import new_session
    from app.models import Tenant
    from app.rag.retrieval import index_manager
    from app.services import get_services
    from app.textutil import expand_query, tokenize

    questions = ["退款多久能到账", "退货运费谁出", "积分可以怎么抵扣", "发票怎么开", "能货到付款吗",
                 "海外订单多久送到", "银行卡退款要几天", "客服内部怎么处理投诉", "SO20261008002 到哪了", "会员等级"]
    embedder = get_services().embedder
    with new_session() as db:
        tenant = db.execute(select(Tenant).where(Tenant.slug == "leon")).scalar_one()
        index = index_manager.get(db, tenant.id)
        for question in questions:
            query = expand_query(question)
            q_tokens, qvec = tokenize(query), embedder.embed([query])[0]
            for allowed in ({"public"}, {"public", "internal"}):
                assert index.bm25(q_tokens, allowed, 12) == _scan_bm25(index, q_tokens, allowed, 12), question
                got, expected = index.vector(qvec, allowed, 12), _scan_vector(index, qvec, allowed, 12)
                assert [i for i, _ in got] == [i for i, _ in expected], question
                assert all(abs(a - b) < 1e-5 for (_, a), (_, b) in zip(got, expected))
