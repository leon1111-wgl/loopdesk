"""What TTS may say: citations and markdown removed, order numbers spelled out, and sensitive
data (phone, card, ID, e-mail, codes) never read aloud, only named."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..textutil import CITE_RE, ORDER_ID_RE

_SPELL = dict(zip("0123456789", "零幺二三四五六七八九"))
_SPELL_PLAIN = dict(zip("0123456789", "零一二三四五六七八九"))
_SENTENCE = re.compile(r"(?<=[。！？!?；;])|\n+")
_SENSITIVE = [
    ("code", re.compile(r"验证码(?:是|为)?[:：]?\s*\d{4,8}"), lambda m: "验证码（不播报）"),
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), lambda m: "您的邮箱"),
    ("id_card", re.compile(r"(?<![\dA-Za-z])\d{6}(?:19|20)\d{2}\d{4}\d{3}[\dXx](?![\dA-Za-z])"), lambda m: "您的身份证号"),
    ("bank_card", re.compile(r"(?<![\dA-Za-z])\d{4}[\d* -]{8,19}\d{4}(?![\dA-Za-z])"),
     lambda m: f"尾号{_digits(m.group(0)[-4:])}的银行卡"),
    ("phone", re.compile(r"(?<![\dA-Za-z])1[3-9]\d[\d*]{4}\d{4}(?![\dA-Za-z])"),
     lambda m: f"尾号{_digits(m.group(0)[-4:])}的手机号"),
]


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
    text, redacted = sentence, []
    for kind, pattern, replace in _SENSITIVE:
        text, n = pattern.subn(replace, text)
        if n:
            redacted.append(kind)
    text = CITE_RE.sub("", text)
    text = re.sub(r"[*#`>]+", "", text)
    text = re.sub(r"https?://\S+", "（链接请在页面上查看）", text)
    text = ORDER_ID_RE.sub(lambda m: _spell_order(m.group(0).upper()), text)
    text = re.sub(r"[¥￥]\s*(\d+(?:\.\d+)?)", lambda m: f"{float(m.group(1)):g}元", text)
    return Speech(text.strip(), redacted)


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text) if s and s.strip()]
