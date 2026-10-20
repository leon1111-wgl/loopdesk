"""What TTS may say: citations and markdown removed, order numbers spelled out, and sensitive
data (phone, card, ID, e-mail, codes) never read aloud, only named."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..textutil import CITE_RE, ORDER_ID_RE

_SPELL = dict(zip("0123456789", "零幺二三四五六七八九"))
_SPELL_PLAIN = dict(zip("0123456789", "零一二三四五六七八九"))
_SENTENCE = re.compile(r"(?<=[。！？!?；;])|\n+")
_CODE = re.compile(r"(验证码|动态码|校验码|取件码|提货码|口令|密码)(?:是|为)?[:：]?\s*(?:\d[\s\-]?){3,7}\d")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Seven or more digits (or masking asterisks), possibly in groups: "138 1234 5678", "138-1234-5678",
# "1 3 8 …", "138 **** 5678". Matched with the separators removed, so the grouping does not matter;
# a run that is none of these (a date, a tracking number) is left as it was.
_RUN = re.compile(r"(?<![\dA-Za-z])[\d*](?:[\s\-]{0,2}[\d*Xx]){6,}(?![\dA-Za-z])")
_RUN_KINDS = [
    ("id_card", re.compile(r"\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])\d{5}[\dXx]"), lambda d: "您的身份证号"),
    ("phone", re.compile(r"(?:\+?86)?1[3-9]\d[\d*]{4}\d{4}"), lambda d: f"尾号{_digits(d[-4:])}的手机号"),
    ("bank_card", re.compile(r"\d{4}[\d*]{4,11}\d{4}"), lambda d: f"尾号{_digits(d[-4:])}的银行卡"),
]


def _sensitive_run(m: re.Match[str], redacted: list[str]) -> str:
    collapsed = re.sub(r"[\s\-]", "", m.group(0))
    for kind, pattern, say in _RUN_KINDS:
        if pattern.fullmatch(collapsed):
            redacted.append(kind)
            return say(collapsed)
    return m.group(0)


@dataclass
class Speech:
    text: str
    redacted: list[str] = field(default_factory=list)


def _digits(s: str) -> str:
    return "".join(_SPELL_PLAIN.get(c, c) for c in s)


def _spell_order(order_id: str) -> str:
    digits = order_id[2:]
    groups = [digits[i:i + 4] for i in range(0, len(digits), 4)]
    return "S O，" + "，".join("".join(_SPELL.get(c, c) for c in g) for g in groups)


def speakable(sentence: str) -> Speech:
    # Sensitive data first: a masked "138****5678" is still a phone number, and the markdown
    # clean-up below would turn it into digits.
    redacted: list[str] = []
    text, n = _CODE.subn(lambda m: f"{m.group(1)}（不播报）", sentence)
    if n:
        redacted.append("code")
    text, n = _EMAIL.subn("您的邮箱", text)
    if n:
        redacted.append("email")
    text = _RUN.sub(lambda m: _sensitive_run(m, redacted), text)
    text = CITE_RE.sub("", text)
    text = re.sub(r"[*#`>]+", "", text)
    text = re.sub(r"https?://\S+", "（链接请在页面上查看）", text)
    text = ORDER_ID_RE.sub(lambda m: _spell_order(m.group(0).upper()), text)
    text = re.sub(r"[¥￥]\s*(\d+(?:\.\d+)?)", lambda m: f"{float(m.group(1)):g}元", text)
    return Speech(text.strip(), redacted)


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text) if s and s.strip()]
