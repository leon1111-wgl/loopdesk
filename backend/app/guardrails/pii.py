"""PII masking for prompts and traces (mainland-China formats).

Digits glued to letters are skipped, so order numbers such as SO20261008002 survive.
People type numbers the way they read them: "138-1234-5678", "138 1234 5678",
"+86 138 1234 5678", "6222 0212 3456 7890 123", so a single space or hyphen is allowed
between digits. Over-masking a long digit run is the cheaper mistake.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("id_card", re.compile(r"(?<![A-Za-z\d])\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])\d{3}[\dXx](?![A-Za-z\d])")),
    ("bank_card", re.compile(r"(?<![A-Za-z\d])\d(?:[ -]?\d){15,18}(?![A-Za-z\d])")),
    ("phone", re.compile(r"(?<![A-Za-z\d+])(?:(?:\+|00)?86[ -]?)?1[3-9]\d[ -]?\d{4}[ -]?\d{4}(?![A-Za-z\d])")),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
]


@dataclass
class PIIReport:
    masked: str
    found: list[str] = field(default_factory=list)


def _mask(kind: str, value: str) -> str:
    if kind == "email":
        name, _, domain = value.partition("@")
        return f"{name[:1]}***@{domain}"
    digits = re.sub(r"\D", "", value)
    if kind == "phone":
        digits = digits[-11:]  # drop the country code
        return f"{digits[:3]}****{digits[-4:]}"
    if kind == "id_card":
        digits = value  # keeps a trailing X
    return f"{digits[:4]}{'*' * (len(digits) - 8)}{digits[-4:]}"


def mask_pii(text: str) -> PIIReport:
    found: list[str] = []
    for kind, pattern in _RULES:
        def replace(match: re.Match[str], kind: str = kind) -> str:
            found.append(kind)
            return _mask(kind, match.group(0))

        text = pattern.sub(replace, text)
    return PIIReport(text, found)
