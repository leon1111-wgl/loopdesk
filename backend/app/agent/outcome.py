"""What a refusal looks like, shared by the dashboard and the evaluations.

The system prompt tells the model to say it found no basis instead of guessing, and the
fallback texts say the agent cannot handle the request; both are recognised here, so the
"自助解决率" on the dashboard and the refusal checks in the eval suite use one definition.
"""

from __future__ import annotations

import re

REFUSAL_RE = re.compile(r"没有找到|无法回答|建议您转人工|无法处理|暂时无法给出")


def is_refusal(answer: str | None) -> bool:
    return bool(answer and REFUSAL_RE.search(answer))
