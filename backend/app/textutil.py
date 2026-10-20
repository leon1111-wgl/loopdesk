"""Text helpers shared by retrieval, the evidence verifier and the offline model.

Chinese is segmented with jieba (search mode) plus a small customer-service domain
dictionary. If jieba is not installed the tokenizer falls back to overlapping character
bigrams, a dependency-free baseline that still works for BM25.
"""

from __future__ import annotations

import logging
import math
import re
from functools import lru_cache

try:
    import jieba

    jieba.setLogLevel(logging.WARNING)
    for _word in (
        "到账", "原路退回", "无理由退货", "七天无理由", "运单号", "工作日", "运费险", "电子发票",
        "增值税专用发票", "优惠券", "积分", "签收", "质量问题", "换货", "退款", "退货", "转人工",
        "工单", "补偿券", "定制商品", "生鲜", "偏远地区", "发货", "物流", "会员等级", "海外",
        # the music-streaming tenant (Echo音乐)
        "自动续费", "连续包月", "豪华会员", "普通会员", "数字专辑", "Echo币", "无损音质", "杜比全景声", "变灰",
        "监护人", "未成年人", "青少年模式", "兑换券", "扣费",
    ):
        jieba.add_word(_word)
    # The general dictionary segments "退货运费" as 退 / 货运费. The knowledge base never says
    # 货运费, and the wrong split (made on both sides) sent "退货运费谁出" to the membership
    # rules instead of the return policy. Without the word: 退货 / 运费, 换货 / 运费.
    jieba.del_word("货运费")
except ImportError:  # pragma: no cover - exercised only without jieba
    jieba = None

_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]+|[A-Za-z][A-Za-z0-9_\-]*|\d+(?:\.\d+)?")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_WORD_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")
_SENT_RE = re.compile(r"(?<=[。！？!?；;])|\n+")
_NUM_RE = re.compile(r"(?<![A-Za-z\d.])\d+(?:\.\d+)?(?![\d])")
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3})")
CITE_RE = re.compile(r"\[(\d{1,2})\]")
# No \b: Python treats CJK characters as word characters, so "订单SO2026…" has no word
# boundary before "S" and \b silently fails. Explicit look-arounds work next to Chinese.
ORDER_ID_RE = re.compile(r"(?<![A-Za-z0-9])SO\d{8,14}(?!\d)", re.IGNORECASE)

# "不用转人工，帮我查下物流" mentions 人工 but asks for the opposite: negated mentions are cut out
# before any matching (the offline model and the explicit-request rule below share this).
_NEG = r"(?:不用|不要|别|无需|不需要|不必|甭)(?:帮我|给我)?"
NEGATED_PERSON_RE = re.compile(rf"{_NEG}(?:转接|转到|转|找|接)?(?:人工客服|人工|真人)|{_NEG}转接|不是要?投诉")
# An explicit request for a person: a verb aimed at 人工 / 真人 ("转人工", "我要人工客服", "能帮我转接人工吗"),
# the bare words as the whole message ("人工！"), or "我要投诉". A question about the service
# ("人工客服几点上班？") is not a request and goes to the model.
_PERSON = r"(?:人工(?!智能)|真人)"
_EXPLICIT_PERSON_RE = re.compile(
    rf"(?:转|接|找|要|换|叫|联系)(?:到|个|一个|一下)?{_PERSON}"
    rf"|^(?:请|麻烦|我要|我想|给我|帮我)?{_PERSON}(?:客服|服务)?(?:吧|啊|呢|谢谢)?$"
    r"|(?:我要|我想|要)投诉"
)


def asks_for_person(text: str) -> bool:
    """True for an explicit request to talk to a person, which is honoured without the model
    (it cannot be talked out of it, and it still works when the model service is down)."""
    stripped = re.sub(r"[\s，,。.！!？?~～]+", "", NEGATED_PERSON_RE.sub("，", normalize_width(text)))
    return bool(stripped) and bool(_EXPLICIT_PERSON_RE.search(stripped))

# Full-width digits and Latin letters (and ＋ －, which appear in phone numbers) become ASCII:
# "ＳＯ２０２６…" is the same order number. Chinese full-width punctuation (，？！) is left alone,
# so what the customer typed still reads the same in the transcript.
_HALF_WIDTH = str.maketrans({
    **{chr(c): chr(c - 0xFEE0) for c in range(0xFF10, 0xFF1A)},  # ０-９
    **{chr(c): chr(c - 0xFEE0) for c in range(0xFF21, 0xFF3B)},  # Ａ-Ｚ
    **{chr(c): chr(c - 0xFEE0) for c in range(0xFF41, 0xFF5B)},  # ａ-ｚ
    "＋": "+", "－": "-",
})


def normalize_width(text: str) -> str:
    return text.translate(_HALF_WIDTH)


# Question words and function words that carry no topical signal.
STOP_TOKENS = {
    "可以", "什么", "怎么", "如何", "请问", "我的", "你们", "是否", "一下", "这个", "那个", "多久",
    "多少", "哪些", "为什么", "怎样", "能否", "能不能", "是不是", "有没有", "没有", "知道", "告诉",
    "现在", "目前", "已经", "我们", "你好", "您好", "帮我", "一个", "需要", "时候", "情况", "问题",
    "啥时候", "什么时候", "几时", "多会儿", "啥", "咋", "咋样", "哪里", "哪儿", "怎么办", "咋办", "东西",
    "支持",  # "支持X吗" asks about X; the knowledge base says 支持 / 不支持 about everything
    "the", "a", "an", "is", "are", "to", "of", "and", "or", "for", "my", "i", "you", "can", "how", "what",
}


def _bigram_tokens(text: str) -> list[str]:
    tokens: list[str] = []
    for match in _TOKEN_RE.finditer(text):
        piece = match.group(0)
        if _CJK_RE.match(piece):
            tokens.extend([piece] if len(piece) == 1 else [piece[i : i + 2] for i in range(len(piece) - 1)])
        else:
            tokens.append(piece.lower())
    return tokens


def tokenize(text: str) -> list[str]:
    """Tokens for BM25 and overlap scoring. Single CJK characters and stop words are dropped."""
    if len(text) > _CACHE_MAX_CHARS:
        return list(_tokenize(text))
    return list(_tokenize_cached(text))


# One answer tokenizes the same knowledge-base sentences several times (retrieval, answer
# extraction, the verifier), and jieba is the largest CPU cost per request. Segmentation
# is a pure function of the text once the dictionary is loaded at import, so it is
# memoised; long texts (whole documents at ingest) are not, to bound memory.
_CACHE_MAX_CHARS = 2000


def _tokenize(text: str) -> tuple[str, ...]:
    if jieba is None:
        raw = _bigram_tokens(text)
    else:
        raw = [t.strip().lower() for t in jieba.lcut_for_search(text)]
    tokens = []
    for t in raw:
        if not t or not _WORD_RE.search(t) or t in STOP_TOKENS:
            continue
        if len(t) == 1 and _CJK_RE.match(t):
            continue
        tokens.append(t)
    return tuple(tokens)


_tokenize_cached = lru_cache(maxsize=4096)(_tokenize)


# Query normalisation for how customers actually type. Variant characters are folded, and
# colloquial phrases are *expanded* with the knowledge base's own wording (never replaced,
# so recall can only grow). Kept small and domain-specific on purpose: every entry should
# be justified by an eval case ("colloquial" tag) or a real bad case from the flywheel.
_VARIANT_CHARS = str.maketrans({"帐": "账"})
QUERY_SYNONYMS: dict[str, str] = {
    "退回来": "退款 到账",
    "退钱": "退款",
    "钱啥时候": "退款 到账",
    "不想要": "七天无理由退货",
    "能退吗": "退货 退款",
    "能不能退": "退货 退款",
    "专票": "增值税专用发票",
    "开票": "发票",
    "开发票": "开具 发票",  # jieba keeps 开发票 as one word and search mode adds 开发
    "多掏钱": "额外支付 运费",
    "加钱": "额外支付",
    "快递费": "运费",
    "钱什么时候": "退款 到账",
    "钱多久": "退款 到账",
    "退到哪": "原路退回",
    "坏了": "质量问题",
    "能换": "换货",
    "换一个": "换货",
    "国外": "海外",
    "境外": "海外",
    "寄到": "配送",
    "抵钱": "抵扣",
    # generic support phrasing (found with the SaaS tenant's eval cases)
    "用不了": "异常",
    "不能用": "异常",
    "有人管": "响应",
    "有人处理": "响应",
    # found with the music-streaming tenant's eval cases
    "灰了": "变灰",
    "放不了": "无法播放",
    "听不了": "无法播放",
    "块钱": "元",
    "孩子": "未成年人",
    "小孩": "未成年人",
    "扣钱": "扣费",
    "关掉续费": "取消自动续费",
    "不想续了": "取消自动续费",
}
_SYNONYM_RE = re.compile("|".join(sorted(map(re.escape, QUERY_SYNONYMS), key=len, reverse=True)))


def expand_query(text: str) -> str:
    """"钱啥时候能退回来？" -> "钱啥时候能退回来？ 退款 到账"."""
    folded = text.translate(_VARIANT_CHARS)
    extras = list(dict.fromkeys(canonical for phrase, canonical in QUERY_SYNONYMS.items() if phrase in folded))
    return f"{folded} {' '.join(extras)}" if extras else folded


def canonical_query(text: str) -> str:
    """The question in the knowledge base's own words: colloquial phrases are *replaced*.

    Used by the relevance gate. There, the colloquial words themselves ("寄到", "国外") are
    out of the knowledge base's vocabulary, get the highest IDF, and would drag coverage
    under the threshold even though the canonical words match: "能寄到国外吗" -> "能 配送 海外 吗"."""
    return _SYNONYM_RE.sub(lambda m: f" {QUERY_SYNONYMS[m.group(0)]} ", text.translate(_VARIANT_CHARS))


def token_set(text: str) -> set[str]:
    return set(tokenize(text))


def split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENT_RE.split(text)]
    return [p for p in parts if p]


def normalize_numbers(text: str) -> str:
    return _THOUSANDS_RE.sub("", text)


def extract_numbers(text: str) -> list[float]:
    """Numbers that make a factual claim (amounts, days, hours, dates).
    Digits glued to letters (order IDs such as SO20261008002) are identifiers, not numbers."""
    return [float(m.group(0)) for m in _NUM_RE.finditer(normalize_numbers(text))]


def strip_citations(text: str) -> tuple[str, list[int]]:
    refs = [int(r) for r in CITE_RE.findall(text)]
    return CITE_RE.sub("", text).strip(), refs


def overlap_recall(claim: str, evidence: str) -> float:
    """Share of the claim's tokens that also appear in the evidence."""
    claim_tokens = token_set(claim)
    if not claim_tokens:
        return 1.0
    return len(claim_tokens & token_set(evidence)) / len(claim_tokens)


def estimate_tokens(text: str) -> int:
    """Rough token estimate used when a provider does not report usage."""
    if not text:
        return 0
    cjk = len(_CJK_RE.findall(text))
    other = len(text) - cjk
    return cjk + math.ceil(other / 4)
