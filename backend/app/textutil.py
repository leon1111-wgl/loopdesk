"""Text helpers shared by retrieval, the evidence verifier and the offline model.

Chinese is segmented with jieba (search mode) plus a small customer-service domain
dictionary. If jieba is not installed the tokenizer falls back to overlapping character
bigrams, a dependency-free baseline that still works for BM25.
"""

from __future__ import annotations

import logging
import math
import re

try:
    import jieba

    jieba.setLogLevel(logging.WARNING)
    for _word in (
        "到账", "原路退回", "无理由退货", "七天无理由", "运单号", "工作日", "运费险", "电子发票",
        "增值税专用发票", "优惠券", "积分", "签收", "质量问题", "换货", "退款", "退货", "转人工",
        "工单", "补偿券", "定制商品", "生鲜", "偏远地区", "发货", "物流", "会员等级", "海外",
    ):
        jieba.add_word(_word)
except ImportError:  # pragma: no cover - exercised only without jieba
    jieba = None

_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]+|[A-Za-z][A-Za-z0-9_\-]*|\d+(?:\.\d+)?")
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_WORD_RE = re.compile(r"[\u4e00-\u9fffA-Za-z0-9]")
_SENT_RE = re.compile(r"(?<=[。！？!?；;])|\n+")
_NUM_RE = re.compile(r"(?<![A-Za-z\d.])\d+(?:\.\d+)?(?![\d])")
_THOUSANDS_RE = re.compile(r"(?<=\d),(?=\d{3})")
CITE_RE = re.compile(r"\[(\d{1,2})\]")
ORDER_ID_RE = re.compile(r"\bSO\d{8,14}\b", re.IGNORECASE)

# Question words and function words that carry no topical signal.
STOP_TOKENS = {
    "可以", "什么", "怎么", "如何", "请问", "我的", "你们", "是否", "一下", "这个", "那个", "多久",
    "多少", "哪些", "为什么", "怎样", "能否", "能不能", "是不是", "有没有", "没有", "知道", "告诉",
    "现在", "目前", "已经", "我们", "你好", "您好", "帮我", "一个", "需要", "时候", "情况", "问题",
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
    return tokens


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
