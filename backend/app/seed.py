"""Demo data: two tenants, users, knowledge documents, orders and eval cases.

Everything here is SYNTHETIC. Run ``python -m app.seed --reset`` to rebuild the
local database from scratch.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from .llm.base import Embedder
from .models import EvalCase, Order, Tenant, User
from .rag.ingest import ingest_document
from .security import hash_password

DEMO_PASSWORD = "loopdesk-demo"
BASE_DIR = Path(__file__).resolve().parent.parent
SEED_DIR = BASE_DIR / "seed_data"
EVAL_DIR = BASE_DIR / "evals"

TENANTS = [
    {
        "slug": "leon", "name": "Leon优选", "industry": "ecommerce", "auto_refund_limit": 50.0, "ticket_sla_hours": 24,
        "users": [
            ("admin@leon.demo", "Leon", "admin"),
            ("agent@leon.demo", "Mia", "agent"),
            ("customer@leon.demo", "Emma", "customer"),
            ("customer2@leon.demo", "Jack", "customer"),
        ],
        "documents": [
            ("refund_policy.md", "售后与退款政策", "public"),
            ("shipping.md", "配送与物流说明", "public"),
            ("membership.md", "会员与积分规则", "public"),
            ("invoice.md", "发票开具指南", "public"),
            ("internal_handbook.md", "客服内部操作手册", "internal"),
            ("supplier_faq.md", "供应商合作 FAQ", "public"),
        ],
    },
    {
        "slug": "iris", "name": "Iris云文档", "industry": "saas", "auto_refund_limit": 0.0, "ticket_sla_hours": 8,
        "users": [
            ("admin@iris.demo", "Iris", "admin"),
            ("customer@iris.demo", "Noah", "customer"),
        ],
        "documents": [
            ("product_faq.md", "产品常见问题", "public"),
            ("sla.md", "SLA 与工单说明", "public"),
        ],
    },
]


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value).replace(tzinfo=timezone.utc)


def _orders(customer: User, other: User) -> list[Order]:
    t = customer.tenant_id
    return [
        Order(id="SO20260918001", tenant_id=t, customer_id=customer.id, amount=39.9, status="delivered",
              items=[{"name": "云朵抱枕", "qty": 1, "price": 39.9}],
              paid_at=_dt("2026-09-18T10:05:00"), delivered_at=_dt("2026-09-20T15:30:00"),
              shipment={"carrier": "Orion快递", "tracking_no": "OR7788123401", "events": [
                  {"time": "2026-09-18 19:02", "desc": "已揽收（杭州）"},
                  {"time": "2026-09-20 15:30", "desc": "已签收，签收人：本人"}]}),
        Order(id="SO20261008002", tenant_id=t, customer_id=customer.id, amount=1299.0, status="shipped",
              items=[{"name": "扫地机器人 X1", "qty": 1, "price": 1299.0}],
              paid_at=_dt("2026-10-08T09:12:00"),
              shipment={"carrier": "Atlas快运", "tracking_no": "AT1402938475", "events": [
                  {"time": "2026-10-08 18:20", "desc": "已揽收（深圳）"},
                  {"time": "2026-10-09 06:45", "desc": "已到达杭州转运中心"},
                  {"time": "2026-10-09 14:10", "desc": "派送中，快递员正在派件"}]}),
        Order(id="SO20261012003", tenant_id=t, customer_id=customer.id, amount=268.0, status="paid",
              items=[{"name": "手冲咖啡套装", "qty": 1, "price": 268.0}], paid_at=_dt("2026-10-12T21:40:00")),
        Order(id="SO20260801004", tenant_id=t, customer_id=customer.id, amount=159.0, status="refunded",
              items=[{"name": "运动水壶", "qty": 2, "price": 79.5}], paid_at=_dt("2026-08-01T08:00:00"),
              delivered_at=_dt("2026-08-03T12:00:00")),
        Order(id="SO20261003005", tenant_id=t, customer_id=other.id, amount=899.0, status="delivered",
              items=[{"name": "降噪耳机 Pro", "qty": 1, "price": 899.0}],
              paid_at=_dt("2026-10-03T11:11:00"), delivered_at=_dt("2026-10-05T10:00:00"),
              shipment={"carrier": "Atlas快运", "tracking_no": "AT1400011122", "events": [
                  {"time": "2026-10-05 10:00", "desc": "已签收"}]}),
    ]


def fixture_orders(tenant_slug: str, users_by_email: dict[str, User]) -> list[Order]:
    """Deterministic business data. Also used by the eval sandbox, so evaluations never
    depend on whatever state the live orders are in."""
    if tenant_slug != "leon":
        return []
    return _orders(users_by_email["customer@leon.demo"], users_by_email["customer2@leon.demo"])


def load_eval_cases(db: Session, tenant: Tenant, path: Path, suite: str = "default") -> int:
    if not path.exists():
        return 0
    count = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        data = json.loads(line)
        db.add(EvalCase(
            tenant_id=tenant.id, key=data["key"], suite=suite, question=data["question"],
            role=data.get("role", "customer"), as_user=data.get("as_user"),
            expected_tools=data.get("expected_tools", []), forbidden_tools=data.get("forbidden_tools", []),
            must_include=data.get("must_include", []), must_not_include=data.get("must_not_include", []),
            expect_refusal=data.get("expect_refusal"), expect_approval=data.get("expect_approval"),
            expected_doc=data.get("expected_doc"), tags=data.get("tags", []), source="seed", status="active",
        ))
        count += 1
    db.commit()
    return count


def seed_demo(db: Session, embedder: Embedder) -> bool:
    if db.execute(select(Tenant.id).limit(1)).first():
        return False
    for spec in TENANTS:
        tenant = Tenant(slug=spec["slug"], name=spec["name"], industry=spec["industry"],
                        auto_refund_limit=spec["auto_refund_limit"], ticket_sla_hours=spec["ticket_sla_hours"])
        db.add(tenant)
        db.flush()
        users = {}
        password_hash = hash_password(DEMO_PASSWORD)
        for email, name, role in spec["users"]:
            user = User(tenant_id=tenant.id, email=email, name=name, role=role, password_hash=password_hash)
            db.add(user)
            users[email] = user
        db.flush()
        for filename, title, visibility in spec["documents"]:
            content = (SEED_DIR / spec["slug"] / filename).read_text(encoding="utf-8")
            ingest_document(db, tenant_id=tenant.id, title=title, content=content, embedder=embedder,
                            visibility=visibility, source=filename)
        for order in fixture_orders(spec["slug"], users):
            db.add(order)
        db.commit()
        load_eval_cases(db, tenant, EVAL_DIR / f"{spec['slug']}_default.jsonl")
    return True


DEMO_TRAFFIC = [
    ("customer@leon.demo", ["七天无理由退货有什么条件？", "那退款多久能到账？"]),
    ("customer@leon.demo", ["SO20261008002 的快递到哪了？"]),
    ("customer@leon.demo", ["SO20261008002 我要退款"]),
    ("customer2@leon.demo", ["寄到新疆要额外加运费吗？", "积分可以怎么抵扣？"]),
    ("customer2@leon.demo", ["以旧换新活动怎么参加？"]),
    ("customer2@leon.demo", ["帮我查一下订单 SO20261008002"]),
    ("customer2@leon.demo", ["你们这服务太差了，我要投诉，给我转人工！"]),
    ("agent@leon.demo", ["一线客服最多可以发放多少钱的补偿券？"]),
    ("customer@iris.demo", ["P1故障多久响应？"]),
]


def seed_demo_traffic(db: Session, services) -> None:
    """A handful of real conversations so a fresh demo already has traces, a pending
    approval and a knowledge-gap answer to correct."""
    from .agent.runtime import collect
    from .models import Conversation

    for email, questions in DEMO_TRAFFIC:
        user = db.execute(select(User).where(User.email == email)).scalar_one()
        tenant = db.get(Tenant, user.tenant_id)
        conversation = Conversation(tenant_id=tenant.id, user_id=user.id)
        db.add(conversation)
        db.commit()
        for question in questions:
            collect(services.runtime(db).start(tenant=tenant, user=user, text=question, conversation=conversation))


def main() -> None:  # pragma: no cover - CLI
    from .config import get_settings
    from .db import create_all, get_engine, init_engine, new_session
    from .db import Base
    from .rag.embeddings import build_embedder

    parser = argparse.ArgumentParser(description="Seed LoopDesk demo data")
    parser.add_argument("--reset", action="store_true", help="drop all tables first")
    args = parser.parse_args()
    settings = get_settings()
    init_engine(settings.database_url)
    if args.reset:
        Base.metadata.drop_all(get_engine())
    create_all()
    with new_session() as db:
        created = seed_demo(db, build_embedder(settings))
    print("seeded demo data" if created else "database already has data; use --reset to rebuild")


if __name__ == "__main__":  # pragma: no cover
    main()
