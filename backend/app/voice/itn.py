"""Inverse text normalisation (ITN) for what a Chinese ASR model writes, and the entities the
agent depends on (order numbers, amounts, plan names).

A customer says an order number digit by digit, "S O 二零二六 一零一五 一零二", and an amount the
way it is spoken, "一百九十八块". The model's tools want SO20261015102 and 198. ITN here is
deliberately narrow: order numbers, money, long digit strings (so that a phone or card number
read aloud becomes digits, and PII masking catches it) and short letter codes (P1, MP3). "七天"
stays "七天": the agent reads it fine, and every extra rule is a chance to damage a sentence.

``canonical`` is a different, aggressive normalisation used only for scoring (CER): every Chinese
numeral becomes digits on both sides, so an engine that writes "7天" is not charged an error
against a reference that says "七天".
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

_DIGIT = {"零": "0", "〇": "0", "一": "1", "幺": "1", "二": "2", "三": "3", "四": "4", "五": "5",
          "六": "6", "七": "7", "八": "8", "九": "9"}
_CN_VALUE = {"零": 0, "〇": 0, "一": 1, "幺": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7,
             "八": 8, "九": 9}
_SMALL_UNIT = {"十": 10, "百": 100, "千": 1000}
_BIG_UNIT = {"万": 10_000, "亿": 100_000_000}
_DIGIT_CHARS = "零〇一二三四五六七八九幺"
_NUMERAL_CHARS = _DIGIT_CHARS + "两十百千万亿"

_CJK = r"一-鿿"
_SPACE_NEAR_CJK = re.compile(rf"(?<=[{_CJK}\d])\s+|\s+(?=[{_CJK}\d])")
_SINGLE_LETTERS = re.compile(r"(?<![A-Za-z])([A-Za-z])\s+(?=[A-Za-z](?![A-Za-z]))")
# Five or more digits in a row, spoken or written, with at most one separator between them.
_DIGIT_RUN = re.compile(rf"[{_DIGIT_CHARS}0-9](?:[ ,，、\-]?[{_DIGIT_CHARS}0-9]){{4,}}")
# An order number: up to six Latin letters (the ASR's attempt at "SO": "SO", "s o", "s wall")
# or the words 订单/单号 just before, then the digits. Applied after _DIGIT_RUN.
_ORDER = re.compile(r"(?<![A-Za-z0-9])(?P<prefix>[A-Za-z](?:\s?[A-Za-z]){0,5})?\s*[,，:：]?\s*(?P<digits>\d{8,14})(?!\d)")
# 订单号 2026…, or the second of two: "SO20261008002 和 20261012003".
_ORDER_CONTEXT = re.compile(r"(?:订单号?|单号|SO\d{11}\s*(?:和|跟|与|还有|、|，|,))(?:是|为)?[,，:：]?\s*$")
# Money: a numeral (spoken or written) and 块/元, optionally a trailing 毛/角 digit ("九块九", "三块五毛").
_MONEY = re.compile(rf"(?P<num>[{_NUMERAL_CHARS}点0-9.]+)(?P<unit>块钱|块|元)(?P<tail>[{_DIGIT_CHARS}](?:毛|角)?)?")
_PERCENT = re.compile(rf"百分之(?P<num>[{_NUMERAL_CHARS}点]+)")
# One or two letters and a single digit: "p一" -> P1, "mp三" -> MP3. Not before words that make
# the digit part of a word ("app一直", "vip一个月").
_LETTER_CODE = re.compile(rf"(?<![A-Za-z])(?P<letters>[A-Za-z]{{1,2}})(?P<digit>[{_DIGIT_CHARS}])"
                          rf"(?![{_NUMERAL_CHARS}点下直些样般起定共次个])")
AMOUNT_RE = re.compile(r"(?<![\d.])(\d+(?:\.\d+)?)\s*(?:块钱|块|元)")
ORDER_ID_FORMAT = re.compile(r"^SO(20\d{2})(\d{2})(\d{2})\d{3}$")


@dataclass
class Entity:
    kind: str  # order_id | amount | term
    value: str
    confidence: float = 1.0
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"kind": self.kind, "value": self.value, "confidence": round(self.confidence, 2), "notes": self.notes}


@dataclass
class ITNResult:
    text: str
    inferred_prefix: set[str] = field(default_factory=set)  # order numbers whose "SO" was not heard as such


def cn_number(text: str) -> float | None:
    """A Chinese numeral as a number: 一百九十八 -> 198, 一千二 -> 1200, 三万五 -> 35000,
    十八 -> 18, 一百零五 -> 105, 九点九 -> 9.9, 二零二六 -> 2026 (digits read one by one).
    None for anything that is not a well-formed numeral."""
    if not text:
        return None
    if "点" in text:
        whole, _, frac = text.partition("点")
        if not frac or any(c not in _CN_VALUE for c in frac):
            return None
        base = cn_number(whole) if whole else 0
        return None if base is None else float(f"{int(base)}.{''.join(str(_CN_VALUE[c]) for c in frac)}")
    if all(c in _DIGIT for c in text) and len(text) > 1:  # read digit by digit
        return float("".join(_DIGIT[c] for c in text))
    total, section, number, last_unit, seen_digit = 0, 0, 0, 1, False
    for i, ch in enumerate(text):
        if ch in _CN_VALUE:
            number, seen_digit = _CN_VALUE[ch], True
        elif ch in _SMALL_UNIT:
            unit = _SMALL_UNIT[ch]
            if not seen_digit and ch == "十":  # "十八"
                number = 1
            section += number * unit
            number, last_unit, seen_digit = 0, unit, False
        elif ch in _BIG_UNIT:
            section += number
            total = (total + section) * _BIG_UNIT[ch] if ch == "亿" else total + section * _BIG_UNIT[ch]
            section, number, last_unit, seen_digit = 0, 0, _BIG_UNIT[ch], False
        else:
            return None
    if number and len(text) >= 2 and text[-2] in "百千万" and last_unit >= 100:
        number *= last_unit // 10  # colloquial "一千二" = 1200, "三万五" = 35000
    return float(total + section + number)


def _number_text(value: float) -> str:
    return str(int(value)) if value == int(value) else f"{value:g}"


def _spaces(text: str) -> str:
    text = _SINGLE_LETTERS.sub(r"\1", text)
    return _SPACE_NEAR_CJK.sub("", text).strip()


def itn(raw: str) -> ITNResult:
    """The agent-facing transcript: order numbers, money and long digit strings as digits."""
    text = _spaces(unicodedata.normalize("NFKC", raw))
    text = _DIGIT_RUN.sub(lambda m: "".join(_DIGIT.get(c, c) for c in m.group(0) if c in _DIGIT or c.isdigit()), text)
    inferred: set[str] = set()

    def order(m: re.Match[str], before: str) -> str:
        prefix, digits = (m.group("prefix") or ""), m.group("digits")
        letters = prefix.replace(" ", "").lower()
        if letters == "so":
            return f"SO{digits}"
        plausible = len(digits) == 11 and digits.startswith("20")
        if plausible and (letters or _ORDER_CONTEXT.search(before)):
            inferred.add(f"SO{digits}")  # "s wall 2026…" or "订单号 2026…": the format says it is an order
            return f"SO{digits}"
        return m.group(0)

    pieces, last = [], 0
    for m in _ORDER.finditer(text):  # in order, so "订单号 A 和 B" sees A already as SO…
        pieces.append(text[last:m.start()])
        pieces.append(order(m, "".join(pieces)))
        last = m.end()
    text = "".join(pieces) + text[last:]

    def money(m: re.Match[str]) -> str:
        num = m.group("num")
        value = float(num) if re.fullmatch(r"\d+(?:\.\d+)?", num) else cn_number(num)
        if value is None:
            return m.group(0)
        tail = m.group("tail")
        if tail:
            value = float(f"{int(value)}.{_DIGIT[tail[0]]}")
        return f"{_number_text(value)}{m.group('unit')}"

    text = _MONEY.sub(money, text)
    text = _PERCENT.sub(lambda m: f"{_number_text(v)}%" if (v := cn_number(m.group('num'))) is not None else m.group(0), text)
    text = _LETTER_CODE.sub(lambda m: f"{m.group('letters').upper()}{_DIGIT[m.group('digit')]}", text)
    return ITNResult(text, inferred)


_NUMERAL_RUN = re.compile(rf"[{_NUMERAL_CHARS}](?:[{_NUMERAL_CHARS}]|点(?=[{_DIGIT_CHARS}]))*")
_NOT_WORD = re.compile(r"[^\w]|_")


def canonical(text: str) -> str:
    """For scoring only: NFKC, lower case, no punctuation or spaces, every numeral as digits."""
    text = unicodedata.normalize("NFKC", text).lower()
    text = itn(text).text.lower()

    def numeral(m: re.Match[str]) -> str:
        value = cn_number(m.group(0))
        return m.group(0) if value is None else _number_text(value)

    text = _NUMERAL_RUN.sub(numeral, text)
    return _NOT_WORD.sub("", text)


def valid_order_id(order_id: str) -> bool:
    m = ORDER_ID_FORMAT.match(order_id.upper())
    return bool(m) and 1 <= int(m.group(2)) <= 12 and 1 <= int(m.group(3)) <= 31


def extract_entities(text: str, terms: list[str] = ()) -> list[Entity]:
    """Order numbers, amounts and known plan or product names in an ITN'd transcript."""
    from ..textutil import ORDER_ID_RE

    entities = [Entity("order_id", m.group(0).upper()) for m in ORDER_ID_RE.finditer(text)]
    entities += [Entity("amount", _number_text(float(m.group(1)))) for m in AMOUNT_RE.finditer(text)]
    lowered = text.lower()
    entities += [Entity("term", t) for t in terms if t.lower() in lowered]
    return entities


def assess(entities: list[Entity], *, inferred_prefix: set[str] = frozenset(),
           other_passes: list[str] = (), char_probs: list[tuple[str, float]] | None = None) -> list[Entity]:
    """Confidence for the entities that a wrong value would turn into a wrong action. Signals, all
    cheap: the order number's format (SO + date + 3 digits), a prefix that was inferred rather than
    heard, a second decoding pass that disagrees (the streaming pass versus the final pass), and the
    engine's own token probabilities when it reports them (Whisper does, Paraformer does not)."""
    # Compared entity by entity ("198" is a substring of "1980块", and that pass disagrees). A pass that
    # has no entity of the kind is no evidence either way: streaming often drops the unit ("一百九十八").
    others = []
    for p in other_passes:
        if p:
            kinds: dict[str, set[str]] = {}
            for o in extract_entities(itn(p).text):
                kinds.setdefault(o.kind, set()).add(o.value.upper())
            others.append(kinds)
    for e in entities:
        if e.kind == "term":
            continue
        score = 0.95
        if e.kind == "order_id":
            if not valid_order_id(e.value):
                score = min(score, 0.2)
                e.notes.append("format")
            if e.value in inferred_prefix:
                score = min(score, 0.5)
                e.notes.append("prefix_inferred")
        if any(e.kind in o and e.value.upper() not in o[e.kind] for o in others):
            score = min(score, 0.4)
            e.notes.append("passes_disagree")
        if char_probs:
            probs = [p for token, p in char_probs if token and token.strip() and token.strip() in key]
            if probs and min(probs) < 0.5:
                score = min(score, 0.4)
                e.notes.append("low_token_probability")
        e.confidence = score
    return entities
