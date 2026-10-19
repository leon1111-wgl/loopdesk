"""Prompt-injection heuristics.

Two places use this:
- ingestion: knowledge chunks that try to instruct the model are flagged and never
  reach the prompt (indirect injection, the more dangerous kind for RAG);
- user input: suspicious messages are annotated in the trace. They are not blocked,
  because the real defence is that tools enforce permissions server-side.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("override_instructions", re.compile(r"(忽略|无视|忘记|跳过)(掉)?(之前|以上|上述|前面|所有|全部)?的?(所有|全部)?(指令|指示|提示|规则|设定|要求)")),
    ("override_instructions_en", re.compile(r"(ignore|disregard|forget)\s+(all\s+)?(the\s+)?(previous|prior|above|earlier)\s+(instructions|prompts|rules)", re.I)),
    ("role_hijack", re.compile(r"(你现在是|从现在起你是|扮演|进入)(一个)?.{0,8}(模式|管理员|开发者|系统)")),
    ("system_prompt_probe", re.compile(r"(系统提示词|system\s*prompt|你的提示词|输出你的(指令|设定))", re.I)),
    ("bypass_approval", re.compile(r"(无需|不需要|跳过|绕过)(人工)?(审批|审核|确认|校验)")),
    ("direct_tool_order", re.compile(r"(直接|立即|马上)(调用|执行|发起).{0,12}(退款|refund|工具)", re.I)),
    ("fake_system_tag", re.compile(r"(【系统通知】|\[system\]|<\s*system\s*>|###\s*system)", re.I)),
]


@dataclass
class InjectionReport:
    suspicious: bool
    matches: list[str] = field(default_factory=list)


def detect_injection(text: str) -> InjectionReport:
    matches = [name for name, pattern in _PATTERNS if pattern.search(text)]
    return InjectionReport(bool(matches), matches)
