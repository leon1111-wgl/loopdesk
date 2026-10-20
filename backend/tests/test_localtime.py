"""Dates shown to people are the tenant's local dates (review round 3, N-024)."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from tests.conftest import by_type

BEIJING = ZoneInfo("Asia/Shanghai")


# Review round 3, N-024: the dashboard's days were UTC dates, so the traffic at 06:14 Beijing time on
# 10-20 was counted under 10-19 while every other time on the page is local. Days are now the tenant's
# (Asia/Shanghai unless changed), and "the last 7 days" means 7 local calendar days, today included.

def _beijing_today_at(hour: int, minute: int = 0, days_ago: int = 0) -> datetime:
    local = datetime.now(BEIJING).replace(hour=hour, minute=minute, second=0, microsecond=0) - timedelta(days=days_ago)
    return local.astimezone(timezone.utc)


def test_the_dashboard_counts_days_in_the_tenants_time_zone(client, ask, login):
    from app.db import new_session
    from app.models import Run

    customer, admin = login("customer@leon.demo"), login("admin@leon.demo")
    early, oldest, too_old = (by_type(ask(customer, q)[1], "done")[0]["run_id"]
                              for q in ("付款以后多久发货？", "积分可以怎么抵扣？", "退款多久能到账？"))
    with new_session() as db:
        db.get(Run, early).created_at = _beijing_today_at(0, 1)          # the previous UTC date
        db.get(Run, oldest).created_at = _beijing_today_at(0, 1, days_ago=6)
        db.get(Run, too_old).created_at = _beijing_today_at(23, 59, days_ago=7)
        db.commit()
    overview = client.get("/api/metrics/overview?days=7", headers=admin).json()
    today = datetime.now(BEIJING).date()
    days = [d["day"] for d in overview["daily"]]
    assert days == [(today - timedelta(days=n)).strftime("%m-%d") for n in range(6, -1, -1)], "7 local days, in order"
    assert overview["daily"][-1]["runs"] == 1 and overview["daily"][0]["runs"] == 1
    assert overview["runs"] == 2, "the 7 days start at local midnight six days ago"
    assert overview["timezone"] == "Asia/Shanghai"

    assert client.patch("/api/tenant", json={"timezone": "Mars/Olympus"}, headers=admin).status_code == 400
    assert client.patch("/api/tenant", json={"timezone": "UTC"}, headers=admin).status_code == 200
    utc = client.get("/api/metrics/overview?days=7", headers=admin).json()
    assert utc["timezone"] == "UTC" and len(utc["daily"]) == 7


def test_order_dates_shown_to_customers_are_local_dates(client, ask, login):
    from app.db import new_session
    from app.models import Order

    delivered = _beijing_today_at(6, 30, days_ago=2)  # 22:30 UTC the day before
    with new_session() as db:
        db.get(Order, "SO20260918001").delivered_at = delivered
        db.commit()
    _, events = ask(login("customer@leon.demo"), "SO20260918001 这个订单现在什么状态？")
    order = next(e["output"] for e in by_type(events, "tool_result") if e["name"] == "get_order")
    assert order["delivered_at"] == delivered.astimezone(BEIJING).strftime("%Y-%m-%d")
    assert order["delivered_at"] != delivered.strftime("%Y-%m-%d"), "the UTC date is the day before"
