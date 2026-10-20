"""What a refusal looks like, shared by the dashboard and the evaluations.

The system prompt tells the model to say it found no basis instead of guessing, and the
fallback texts say the agent cannot handle the request; both are recognised here, so the
"自助解决率" on the dashboard and the refusal checks in the eval suite use one definition.
A real model words it its own way ("知识库中没有检索到相关依据", "没有“加急发货”的相关依据"),
and declining to give out someone's private details is a refusal too. A policy that says no
("暂不支持指定快递公司") is an answer, not a refusal.
"""

from __future__ import annotations

import re

REFUSAL_RE = re.compile(
    r"没有(找到|检索到|查到)|(知识库|资料)[中里]?暂?(没有|未找到|未检索到|暂无).{0,12}依据"
    r"|无法回答|建议您转人工|无法处理|暂时无法给出"
    r"|(隐私|个人信息).{0,8}(无法|不能|不便)(提供|透露)"
)


def is_refusal(answer: str | None) -> bool:
    return bool(answer and REFUSAL_RE.search(answer))
