import os
from datetime import date

from app.agent.prompts import build_system_prompt
from app.models import Tenant, User


def test_system_prompt_keeps_per_request_facts_after_the_shared_rules():
    leon, iris = Tenant(name="Leon优选"), Tenant(name="Iris云文档")
    customer, agent = User(role="customer"), User(role="agent")
    a = build_system_prompt(leon, customer, today=date(2026, 10, 19))
    b = build_system_prompt(iris, agent, today=date(2026, 10, 20))

    shared = os.path.commonprefix([a, b])
    # Everything up to the rules is byte-identical across tenants, roles and days, so a
    # provider's prefix cache can serve it; the variable lines sit in the short tail.
    assert "8. 用简体中文回答" in shared
    assert "Leon优选" not in shared and "2026-10-19" not in shared
    assert a.endswith("当前会话：企业「Leon优选」，对话对象是顾客，今天是 2026-10-19。")
