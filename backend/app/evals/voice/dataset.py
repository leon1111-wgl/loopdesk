"""The voice test set: every gate question of every tenant, as a customer would say it.

A TTS front end reads "SO20261015102" as one number ("两百零二亿……"), which no customer does,
so each question gets a spoken script: order numbers digit by digit in groups, amounts and
counts as spoken numerals, codes letter by letter. The written question stays the reference:
what the agent should receive. For engines whose Chinese front end cannot read Latin letters
(Kokoro zh), letters and the few brand words become Chinese characters that sound alike; a
question that is mostly English is skipped for them.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ...textutil import ORDER_ID_RE, normalize_width

EVAL_DIR = Path(__file__).resolve().parents[3] / "evals"
TENANTS = ("leon", "iris", "echo")
_DIGIT = dict(zip("0123456789", "零一二三四五六七八九"))
_LETTER = {"A": "诶", "B": "比", "C": "西", "D": "迪", "E": "伊", "F": "艾弗", "G": "吉", "H": "艾尺", "I": "艾",
           "J": "杰", "K": "开", "L": "艾勒", "M": "艾姆", "N": "恩", "O": "欧", "P": "批", "Q": "丘", "R": "阿尔",
           "S": "艾斯", "T": "踢", "U": "优", "V": "维", "W": "达布溜", "X": "艾克斯", "Y": "歪", "Z": "贼德"}
_WORDS = {"echo": "艾扣", "apple": "爱破", "app": "爱普", "iphone": "爱疯", "store": "斯多尔", "id": "艾迪",
          "hi-res": "嗨瑞斯"}
_UNIT = r"块钱|块|元|天|小时|分钟|首|积分|张|个工作日|工作日|个月|年|月|号|倍|折|G"


@dataclass
class Utterance:
    id: str  # tenant/key
    tenant: str
    key: str
    question: str  # the written form: the reference
    spoken: str  # what the speaker says (Latin kept)
    spoken_hanzi: str | None  # for front ends without Latin; None when the question is mostly English
    history: list[str] = field(default_factory=list)


def cardinal(n: int) -> str:
    """Chinese reading of a whole number below 10^8: 198 -> 一百九十八, 10500 -> 一万零五百."""
    if n == 0:
        return "零"
    if n >= 10_000:
        high, low = divmod(n, 10_000)
        return cardinal(high) + "万" + (("零" if low < 1000 else "") + cardinal(low) if low else "")
    out, zero = "", False
    for value, unit in ((1000, "千"), (100, "百"), (10, "十"), (1, "")):
        digit, n = divmod(n, value)
        if digit:
            if zero:
                out += "零"
            out += _DIGIT[str(digit)] + unit
            zero = False
        elif out:
            zero = True
    return out[1:] if out.startswith("一十") else out


def digits(s: str) -> str:
    return "".join(_DIGIT.get(c, c) for c in s)


def _number(m: re.Match[str]) -> str:
    whole, _, frac = m.group(1).partition(".")
    text = cardinal(int(whole)) + ("点" + digits(frac) if frac else "")
    return text + m.group(2)


def spoken_script(question: str) -> str:
    # Typed full-width characters ("ＳＯ２０２６…") are said like any others; unnormalised, the TTS read
    # them as one large number (found in voice-eval run 39475750196).
    text = normalize_width(question)
    text = ORDER_ID_RE.sub(lambda m: "S O，" + "，".join(digits(m.group(0)[2:][i:i + 4])
                                                        for i in range(0, len(m.group(0)) - 2, 4)), text)
    text = re.sub(r"(\d+(?:\.\d+)?)\s*(" + _UNIT + ")", _number, text)
    text = re.sub(r"\d(?:[ -]?\d){3,}", lambda m: digits(re.sub(r"[ -]", "", m.group(0))), text)  # card numbers
    text = re.sub(r"(?<=[A-Za-z])-?(\d)", lambda m: " " + digits(m.group(1)), text)  # P1, MP3, AP-0001
    text = re.sub(r"\d+(?:\.\d+)?", lambda m: _number(re.match(r"(.+)()", m.group(0))), text)
    text = re.sub(r"(?<![A-Za-z])([A-Z]{2,4})(?![A-Za-z])", lambda m: " ".join(m.group(1)), text)  # MP -> M P
    text = re.sub(r"(?<=[A-Za-z])-(?=[零一二三四五六七八九])", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def to_hanzi(spoken: str) -> str | None:
    words = re.findall(r"[A-Za-z][A-Za-z\-]*", spoken)
    known = [w for w in words if w.lower() in _WORDS or len(w) == 1 or w.isupper()]
    if len(words) - len(known) > 1:
        return None  # a sentence in English: not for a Chinese-only front end

    def word(m: re.Match[str]) -> str:
        w = m.group(0)
        if w.lower() in _WORDS:
            return _WORDS[w.lower()]
        return "".join(_LETTER.get(c.upper(), "") for c in w)

    return re.sub(r"\s+", "", re.sub(r"[A-Za-z][A-Za-z\-]*", word, spoken).replace("，S", "，"))


def load(tenants: tuple[str, ...] = TENANTS, limit: int = 0) -> list[Utterance]:
    items: list[Utterance] = []
    for tenant in tenants:
        rows = [json.loads(line) for line in (EVAL_DIR / f"{tenant}_default.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()]
        rows = [r for r in rows if r.get("status", "active") == "active"][: limit or None]
        for r in rows:
            spoken = spoken_script(r["question"])
            items.append(Utterance(f"{tenant}/{r['key']}", tenant, r["key"], r["question"], spoken, to_hanzi(spoken),
                                   r.get("history") or []))
    return items


def write_manifest(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def as_dict(u: Utterance) -> dict:
    return asdict(u)
