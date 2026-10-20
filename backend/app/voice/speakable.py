"""What TTS may say: citations and markdown removed, order numbers spelled out, and sensitive
data (phone, card, ID, e-mail, codes) never read aloud, only named."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from ..textutil import CITE_RE, ORDER_ID_RE

_SPELL = dict(zip("0123456789", "零幺二三四五六七八九"))
_SPELL_PLAIN = dict(zip("0123456789", "零一二三四五六七八九"))
_SENTENCE = re.compile(r"(?<=[。！？!?；;])|\n+")
_CODE_WORDS = r"验证码|动态码|校验码|短信码|取件码|提货码|激活码|兑换码|口令|密码|PIN|pin"
_CODE = re.compile(rf"({_CODE_WORDS})(?:\([^)]{{0,8}}\))?(?:是|为)?\s*[:：]?\s*[A-Za-z0-9](?:[A-Za-z0-9\s\-]{{2,22}}[A-Za-z0-9])?")
_PASSPORT = re.compile(r"护照(?:号码?)?\s*[:：]?\s*[A-Za-z]\d{8}")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Seven or more digits (or masking asterisks), possibly in groups: "138 1234 5678", "138-1234-5678",
# "1 3 8 …", "138 **** 5678", or as Chinese numerals. Matched with the separators removed, so the
# grouping does not matter; a run that is none of these (a date, a tracking number) is left as it was.
_RUN = re.compile(r"(?<![\dA-Za-z])[\d*](?:[\s\-]{0,2}[\d*Xx]){6,}(?![\dA-Za-z])")
_HAN_RUN = re.compile(r"[零〇一二三四五六七八九幺]{7,}")
_HAN_DIGIT = dict(zip("零〇一二三四五六七八九幺", "001234567891"))
# Numbers the customer asked for and may need to hear: a tracking or ticket number is not personal data.
_REFERENCE = re.compile(r"(?:单号|运单号|快递单号|流水号|工单号|订单号|编号|批次号|申请号)\s*[:：]?\s*(?:[A-Za-z]{1,4}\s*)?$")
_RUN_KINDS = [
    ("id_card", re.compile(r"\d{6}(?:19|20)\d{2}(?:0[1-9]|1[0-2])\d{5}[\dXx]"), lambda d: "您的身份证号"),
    ("phone", re.compile(r"(?:86)?1[3-9]\d[\d*]{4}\d{4}"), lambda d: f"尾号{_digits(d[-4:])}的手机号"),
    ("phone", re.compile(r"0\d{2,3}\d{7,8}"), lambda d: f"尾号{_digits(d[-4:])}的电话"),
    ("bank_card", re.compile(r"\d{4}[\d*]{4,11}\d{4}"), lambda d: f"尾号{_digits(d[-4:])}的银行卡"),
]


def _sensitive(digits: str, redacted: list[str]) -> str | None:
    for kind, pattern, say in _RUN_KINDS:
        if pattern.fullmatch(digits):
            redacted.append(kind)
            return say(digits)
    return None


def _sensitive_run(m: re.Match[str], redacted: list[str]) -> str:
    if _REFERENCE.search(m.string[max(0, m.start() - 12):m.start()]):
        return m.group(0)
    return _sensitive(re.sub(r"[\s\-]", "", m.group(0)), redacted) or m.group(0)


def _amount(m: re.Match[str]) -> str:
    value = float(m.group(1).replace(",", "") + (f".{m.group(2)}" if m.group(2) else ""))
    return f"{int(value) if value == int(value) else value}元"


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
    text = unicodedata.normalize("NFKC", sentence)  # full-width digits are digits
    for kind, pattern, replace in (("code", _CODE, lambda m: f"{m.group(1)}（不播报）"),
                                   ("passport", _PASSPORT, lambda m: "护照号（不播报）"),
                                   ("email", _EMAIL, lambda m: "您的邮箱")):
        text, n = pattern.subn(replace, text)
        if n:
            redacted.append(kind)
    text = _RUN.sub(lambda m: _sensitive_run(m, redacted), text)
    text = _HAN_RUN.sub(lambda m: _sensitive("".join(_HAN_DIGIT[c] for c in m.group(0)), redacted) or m.group(0), text)
    text = CITE_RE.sub("", text)
    text = re.sub(r"[*#`>]+", "", text)
    text = re.sub(r"https?://\S+", "（链接请在页面上查看）", text)
    text = ORDER_ID_RE.sub(lambda m: _spell_order(m.group(0).upper()), text)
    text = re.sub(r"[¥￥]\s*(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?", _amount, text)
    return Speech(text.strip(), redacted)


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE.split(text) if s and s.strip()]
