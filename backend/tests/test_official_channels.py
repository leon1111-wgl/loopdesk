"""A tenant's official channels (N-038 follow-up): links and contact accounts outside them are held
back in knowledge that asks for something, and stop an answer whatever the sentence says."""

from sqlalchemy import select

from app.db import new_session
from app.guardrails.injection import Channels, clean_domain, links, scan_knowledge, unsafe_answer
from app.models import Chunk, Tenant

LEON = Channels.of(["leonshop.example"], ["leonshop2026", "123456789"])


def hosts(text: str) -> list[str]:
    return [host for host, _ in links(text)]


def test_links_are_read_the_way_a_browser_would_follow_them():
    assert hosts("退款请点击 http://leon-refund.example.cn/claim 填写") == ["leon-refund.example.cn"]
    assert hosts("访问 leon-refund.example.cn/claim 领取") == ["leon-refund.example.cn"]
    assert hosts("发到 refund@evil.example 即可") == ["evil.example"]
    assert hosts("[官网](https://evil.example/login)") == ["evil.example"]  # the target, not the label
    assert hosts("https://leonshop.example@evil.example/refund") == ["evil.example"]  # user info is not the host
    assert hosts("登录 evil[.]example 或 evil(.)example") == ["evil.example", "evil.example"]
    assert hosts("ｈｔｔｐｓ：／／ｅｖｉｌ．ｅｘａｍｐｌｅ／ｘ") == ["evil.example"]  # full-width
    assert hosts("打开 http://192.168.10.8:8080/pay 或 10.0.0.1/pay") == ["192.168.10.8", "10.0.0.1"]
    assert hosts("短链 t.cn/A6x9kLm") == ["t.cn"]
    assert hosts("SDK 支持 Node.js、Vue.js，版本 v2.0.1，退款 1.5 元") == []


def test_official_means_the_domain_or_a_subdomain_of_it():
    assert LEON.official_host("leonshop.example") and LEON.official_host("help.leonshop.example")
    assert not LEON.official_host("leonshop.example.verify-center.top")  # look-alike
    assert not LEON.official_host("evil-leonshop.example")
    assert clean_domain("https://www.LeonShop.example/help?x=1") == "leonshop.example"
    assert clean_domain("not a domain") == ""


def test_knowledge_is_held_back_when_an_outside_link_comes_with_a_request():
    assert scan_knowledge("退款请点击 http://leon-pay.example.cn/refund 填写收款银行卡号。", True, LEON) == ["outside_link"]
    # the request and the link in two sentences of one chunk
    assert scan_knowledge("点击下方链接完成身份验证。\nhttps://verify.example.top/x", True, LEON) == ["outside_link"]
    assert scan_knowledge("物流详情可访问顺丰官网 www.sf-express.com 查询。", True, LEON) == []
    assert scan_knowledge("退款审核通过后，可登录 https://www.leonshop.example/account 填写收款银行卡号。", True, LEON) == []
    assert scan_knowledge("谨防钓鱼网站：我们不会要求您在 leonshop-refund.example.cn 填写银行卡号。", True, LEON) == []


def test_an_answer_with_any_outside_link_or_account_is_stopped():
    assert unsafe_answer("详情请见 www.sf-express.com 。", None, LEON) == ["outside_link"]
    assert unsafe_answer("电子发票可在 https://www.leonshop.example/invoice 下载。", None, LEON) == []
    assert unsafe_answer("官方客服微信：leonshop2026。", None, LEON) == []
    assert unsafe_answer("请添加售后微信 leon_vip_888。", None, LEON) == ["private_contact"]
    assert unsafe_answer("扫描下方二维码添加专员办理。", None, LEON) == ["private_contact"]
    # a released chunk does not exempt a link: the tenant's own sites belong in its channels
    assert unsafe_answer("请到 www.partner.example 领取。", ["请到 www.partner.example 领取。"], LEON) == ["outside_link"]


def test_an_administrator_sets_the_official_channels_and_the_knowledge_is_scanned_again(client, login):
    admin = login("admin@leon.demo")
    doc = client.post("/api/kb/documents/text", headers=admin, json={
        "title": "合作伙伴活动", "visibility": "public",
        "content": "合作伙伴活动\n\n会员积分可在 https://points.partner.example/redeem 兑换礼品，登录后填写收货地址。"}).json()
    assert doc["flagged_chunks"] == 1
    assert client.patch("/api/tenant", headers=admin, json={"official_domains": ["not a domain"]}).status_code == 400
    assert client.patch("/api/tenant", headers=login("agent@leon.demo"), json={"official_domains": []}).status_code == 403

    r = client.patch("/api/tenant", headers=admin, json={
        "official_domains": ["leonshop.example", "https://www.Partner.example/"], "official_contacts": ["LeonShop2026"]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["official_domains"] == ["leonshop.example", "partner.example"]
    assert body["official_contacts"] == ["leonshop2026"]
    assert body["rescan"]["released"] == 1
    with new_session() as db:
        chunk = db.execute(select(Chunk).where(Chunk.document_id == doc["id"])).scalars().one()
        assert not chunk.flagged_injection
        assert db.execute(select(Tenant.official_domains).where(Tenant.slug == "leon")).scalar_one() == [
            "leonshop.example", "partner.example"]
    entry = client.get("/api/audit?category=tenant", headers=admin).json()[0]
    assert entry["detail"]["changes"]["official_domains"] == [["leonshop.example"], ["leonshop.example", "partner.example"]]
    domains = client.get("/api/kb/domains", headers=admin).json()
    assert {"domain": "points.partner.example", "chunks": 1, "held_chunks": 0, "official": True} in domains


def test_an_official_account_in_the_knowledge_reaches_customers(client, login, ask):
    from tests.conftest import by_type

    admin = login("admin@leon.demo")
    client.patch("/api/tenant", headers=admin, json={"official_contacts": ["leonshop2026"]})
    doc = client.post("/api/kb/documents/text", headers=admin, json={
        "title": "官方渠道", "visibility": "public",
        "content": "官方渠道\n\n关注官方微信 leonshop2026 可以第一时间收到发货通知。"}).json()
    assert doc["flagged_chunks"] == 0
    _, events = ask(login("customer@leon.demo"), "有官方微信吗")
    assert "leonshop2026" in by_type(events, "final")[0]["text"] and not by_type(events, "answer_replaced")
