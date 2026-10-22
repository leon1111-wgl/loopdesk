"""N-102: a member of staff who leaves is disabled, not erased: no sign-in, their open work goes
back to the queue, and their name stays on what they handled."""

from app.seed import DEMO_PASSWORD


def _id(client, headers):
    return client.get("/api/auth/me", headers=headers).json()["id"]


def _sign_in(client, email):
    return client.post("/api/auth/login", json={"email": email, "password": DEMO_PASSWORD})


def test_a_disabled_agent_cannot_sign_in_and_their_open_work_is_requeued(client, ask, login):
    admin, mia, emma = login("admin@leon.demo"), login("agent@leon.demo"), login("customer@leon.demo")
    mia_id = _id(client, mia)
    # Mia closes one handoff, is serving another, and has claimed an issue ticket.
    done, _ = ask(emma, "转人工")
    done_ticket = client.get("/api/handoffs", headers=mia).json()[0]["ticket_id"]
    client.post(f"/api/handoffs/{done_ticket}/reply", json={"content": "您好，我是 Mia"}, headers=mia)
    assert client.post(f"/api/handoffs/{done_ticket}/close", headers=mia).status_code == 200
    live, _ = ask(login("customer2@leon.demo"), "转人工")
    live_ticket = client.get("/api/handoffs", headers=mia).json()[0]["ticket_id"]
    client.post(f"/api/handoffs/{live_ticket}/reply", json={"content": "您好，我是 Mia"}, headers=mia)
    ask(emma, "我买的扫地机器人坏了开不了机，帮我报修")
    issue = client.get("/api/tickets", headers=mia).json()[0]["id"]
    assert client.post(f"/api/tickets/{issue}/claim", headers=mia).status_code == 200

    r = client.post(f"/api/admin/users/{mia_id}/disable", headers=admin)
    assert r.status_code == 200, r.text
    assert r.json()["requeued"] == 2 and r.json()["disabled_at"]

    assert client.get("/api/approvals", headers=mia).status_code == 401, "her tokens stop working"
    again = _sign_in(client, "agent@leon.demo")
    assert again.status_code == 403 and "停用" in again.json()["detail"], "the password no longer works"
    assert _sign_in(client, "agent@leon.demo").status_code == 403

    queue = client.get("/api/handoffs", headers=admin).json()
    assert [(q["ticket_id"], q["assignee"]) for q in queue] == [(live_ticket, None)], "back in the queue"
    ticket = next(t for t in client.get("/api/tickets", headers=admin).json() if t["id"] == issue)
    assert ticket["status"] == "open" and ticket["assignee"] is None
    assert client.post(f"/api/tickets/{issue}/claim", headers=admin).status_code == 200, "someone else takes it"

    # What she already handled still says she did it (erasure would have said "已注销用户 2").
    export = client.get("/api/export/handoffs.csv?days=1", headers=admin).text
    assert "Mia" in export and "已注销" not in export
    entry = client.get("/api/audit?action=user.disable", headers=admin).json()[0]
    assert entry["action"] == "user.disable" and entry["target"] == f"user:{mia_id}"
    assert entry["detail"]["requeued"] == 2
    staff = {u["id"]: u for u in client.get("/api/admin/users", headers=admin).json()}
    assert staff[mia_id]["disabled_at"] and staff[mia_id]["name"] == "Mia"
    assert all(u["role"] != "customer" for u in staff.values())


def test_enable_restores_the_password_and_disabling_twice_changes_nothing(client, login):
    admin = login("admin@leon.demo")
    mia_id = _id(client, login("agent@leon.demo"))
    client.post(f"/api/admin/users/{mia_id}/disable", headers=admin)
    second = client.post(f"/api/admin/users/{mia_id}/disable", headers=admin).json()
    assert second["requeued"] == 0
    assert [e["action"] for e in client.get("/api/audit", headers=admin).json()].count("user.disable") == 1
    assert client.post(f"/api/admin/users/{mia_id}/enable", headers=admin).json()["disabled_at"] is None
    assert _sign_in(client, "agent@leon.demo").status_code == 200
    assert client.get("/api/audit", headers=admin).json()[0]["action"] == "user.enable"


def test_who_can_be_disabled_and_by_whom(client, login):
    admin, mia, emma = login("admin@leon.demo"), login("agent@leon.demo"), login("customer@leon.demo")
    admin_id, mia_id, emma_id = _id(client, admin), _id(client, mia), _id(client, emma)
    assert client.post(f"/api/admin/users/{mia_id}/disable", headers=mia).status_code == 403, "admins only"
    assert client.post(f"/api/admin/users/{admin_id}/disable", headers=admin).status_code == 400, "not yourself"
    assert client.post(f"/api/admin/users/{emma_id}/disable", headers=admin).status_code == 400, "customers are erased"
    assert client.post(f"/api/admin/users/{mia_id}/disable", headers=login("admin@iris.demo")).status_code == 404
    assert client.get("/api/admin/users", headers=mia).status_code == 403
    assert _sign_in(client, "agent@leon.demo").status_code == 200, "nothing was disabled"


def test_staff_accounts_are_not_erased(client, login):
    # Erasure renames the account, so every approval, ticket and audit entry they handled would
    # be signed "已注销用户": staff are disabled instead.
    admin = login("admin@leon.demo")
    mia_id = _id(client, login("agent@leon.demo"))
    r = client.delete(f"/api/admin/users/{mia_id}/personal-data", headers=admin)
    assert r.status_code == 400 and "停用" in r.json()["detail"]
    assert _sign_in(client, "agent@leon.demo").status_code == 200
    staff = {u["id"]: u for u in client.get("/api/admin/users", headers=admin).json()}
    assert staff[mia_id]["name"] == "Mia"


def test_a_token_of_a_disabled_account_is_refused_even_if_not_revoked(client, login):
    # Disabling revokes the tokens; the check in the auth dependency covers a disable written some
    # other way (a script, a restored backup) that left token_version alone.
    from datetime import datetime, timezone

    from app.db import new_session
    from app.models import User

    mia = login("agent@leon.demo")
    with new_session() as db:
        db.get(User, _id(client, mia)).disabled_at = datetime.now(timezone.utc)
        db.commit()
    r = client.get("/api/auth/me", headers=mia)
    assert r.status_code == 401 and "停用" in r.json()["detail"]


def _principal(db, email):
    from sqlalchemy import select

    from app.models import Tenant, User
    from app.security import Principal

    user = db.execute(select(User).where(User.email == email)).scalar_one()
    return Principal(user, db.get(Tenant, user.tenant_id))


def test_two_admins_disabling_each_other_leave_one_admin(client, login):
    # Review (SQLite, the default): both requests passed sign-in, each disabled the other, and the
    # tenant was left with no admin who could enable anyone. The write re-checks the admin.
    import pytest
    from fastapi import HTTPException

    from app.api.auth import disable_user
    from app.db import new_session
    from app.models import User
    from app.security import hash_password

    with new_session() as db:
        db.add(User(tenant_id=_principal(db, "admin@leon.demo").tenant.id, email="noah@leon.demo", name="Noah",
                    role="admin", password_hash=hash_password(DEMO_PASSWORD)))
        db.commit()
    with new_session() as a, new_session() as b:
        leon, noah = _principal(a, "admin@leon.demo"), _principal(b, "noah@leon.demo")
        disable_user(leon.user.id, noah, b)  # Noah's request lands first ...
        with pytest.raises(HTTPException) as refused:
            disable_user(noah.user.id, leon, a)  # ... Leon's had already passed sign-in
        assert refused.value.status_code == 401
    assert _sign_in(client, "noah@leon.demo").status_code == 200, "one admin is left"


def test_a_claim_that_passed_sign_in_before_the_disable_is_refused(client, ask, login):
    # Review: Mia's claim passed get_principal, the disable committed (her ticket was not hers yet,
    # so nothing was requeued), then her claim assigned the ticket to a disabled account.
    import pytest
    from fastapi import HTTPException

    from app.api import handoffs, tickets
    from app.db import new_session

    admin, mia = login("admin@leon.demo"), login("agent@leon.demo")
    ask(login("customer@leon.demo"), "我买的扫地机器人坏了开不了机，帮我报修")
    issue = client.get("/api/tickets", headers=mia).json()[0]["id"]
    ask(login("customer2@leon.demo"), "转人工")
    waiting = client.get("/api/handoffs", headers=mia).json()[0]["ticket_id"]
    with new_session() as db:
        stale = _principal(db, "agent@leon.demo")
        assert client.post(f"/api/admin/users/{stale.user.id}/disable", headers=admin).json()["requeued"] == 0
        with pytest.raises(HTTPException) as claim:
            tickets.claim(issue, stale, db)
        assert claim.value.status_code == 401
        with pytest.raises(HTTPException) as reply:
            handoffs.reply(waiting, handoffs.ReplyIn(content="您好"), stale, db)
        assert reply.value.status_code == 401
    ticket = next(t for t in client.get("/api/tickets", headers=admin).json() if t["id"] == issue)
    assert ticket["status"] == "open" and ticket["assignee"] is None
    assert client.get("/api/handoffs", headers=admin).json()[0]["assignee"] is None
