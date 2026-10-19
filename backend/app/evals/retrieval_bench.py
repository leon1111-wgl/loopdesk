"""Retrieval latency as the knowledge base grows.

    python -m app.evals.retrieval_bench --sizes 1000,5000,20000 --queries 40 --report retrieval.json

Builds a throwaway SQLite database with the demo tenant plus N synthetic chunks (sentences of
the demo knowledge base recombined with made-up product names, so vocabulary and lengths are
realistic), then times building the tenant's index and hybrid_search per question. Offline
hashing embeddings; numbers are for comparing implementations on one machine, not capacity.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import tempfile
import time
from pathlib import Path

QUESTIONS = ["退款多久能到账", "退货运费谁出", "积分可以怎么抵扣", "发票怎么开", "会员有什么权益",
             "能货到付款吗", "海外订单多久送到", "怎么修改收货地址", "银行卡退款要几天", "快递丢了怎么办"]
PRODUCTS = ["Atlas 空气炸锅", "Orion 扫地机", "Leon 保温杯", "Mia 护手霜", "Noah 跑步鞋", "Iris 台灯", "Emma 收纳盒",
            "Jack 背包", "Atlas 电热水壶", "Orion 蓝牙耳机"]


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))]


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - benchmark CLI
    parser = argparse.ArgumentParser(description="LoopDesk retrieval benchmark")
    parser.add_argument("--sizes", default="1000,5000,20000")
    parser.add_argument("--queries", type=int, default=40)
    parser.add_argument("--report")
    args = parser.parse_args(argv)

    from sqlalchemy import select

    from ..config import Settings
    from ..db import init_engine, new_session, prepare_database
    from ..models import Chunk, Document, Tenant
    from ..rag.embeddings import pack
    from ..rag.retrieval import hybrid_search, index_manager
    from ..seed import seed_demo
    from ..services import Services
    from ..textutil import split_sentences

    settings = Settings()
    settings.database_url = f"sqlite:///{Path(tempfile.mkdtemp()) / 'retrieval.db'}"
    init_engine(settings.database_url)
    prepare_database(settings.database_url, True)
    services = Services(settings)
    rng = random.Random(42)
    results = []
    with new_session() as db:
        seed_demo(db, services.embedder)
        tenant = db.execute(select(Tenant).where(Tenant.slug == "leon")).scalar_one()
        sentences = [s for c in db.execute(select(Chunk).where(Chunk.tenant_id == tenant.id)).scalars()
                     for s in split_sentences(c.text) if len(s) > 6]
        current = db.execute(select(Chunk).where(Chunk.tenant_id == tenant.id)).scalars().all().__len__()
        for size in [int(s) for s in args.sizes.split(",")]:
            while current < size:
                batch = min(500, size - current)
                document = Document(tenant_id=tenant.id, title=f"合成文档 {current}", content="", visibility="public",
                                    source="bench", version=1, chunk_count=batch)
                db.add(document)
                db.flush()
                texts = [f"{rng.choice(PRODUCTS)} {current + i}：" + "".join(rng.sample(sentences, 3)) for i in range(batch)]
                vectors = services.embedder.embed([f"合成章节\n{t}" for t in texts])
                db.add_all(Chunk(tenant_id=tenant.id, document_id=document.id, ord=i, heading="合成章节", text=text,
                                 embedding=pack(vector)) for i, (text, vector) in enumerate(zip(texts, vectors)))
                db.commit()
                current += batch
            index_manager.invalidate(tenant.id)
            started = time.perf_counter()
            index_manager.get(db, tenant.id)
            build_s = time.perf_counter() - started
            latencies = []
            for n in range(args.queries):
                started = time.perf_counter()
                hybrid_search(db, tenant.id, QUESTIONS[n % len(QUESTIONS)], services.embedder, {"public"},
                              top_k=4, min_relevance=settings.min_relevance)
                latencies.append((time.perf_counter() - started) * 1000)
            row = {"chunks": current, "index_build_s": round(build_s, 2), "p50_ms": round(statistics.median(latencies), 1),
                   "p95_ms": round(_percentile(latencies, 0.95), 1)}
            results.append(row)
            print(json.dumps(row, ensure_ascii=False), flush=True)
    if args.report:
        Path(args.report).write_text(json.dumps({"levels": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
