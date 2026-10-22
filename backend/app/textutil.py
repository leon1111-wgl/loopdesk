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

# Customer-service domain words: jieba keeps them whole, and the voice channel uses those a
# tenant's knowledge base mentions as ASR hotwords (app/voice/lexicon.py).
DOMAIN_WORDS = (
    "到账", "原路退回", "无理由退货", "七天无理由", "运单号", "工作日", "运费险", "电子发票",
    "增值税专用发票", "优惠券", "积分", "签收", "质量问题", "换货", "退款", "退货", "转人工",
    "工单", "补偿券", "定制商品", "生鲜", "偏远地区", "发货", "物流", "会员等级", "海外",
    # the music-streaming tenant (Echo音乐)
    "自动续费", "连续包月", "豪华会员", "普通会员", "数字专辑", "Echo币", "无损音质", "杜比全景声", "变灰",
    "监护人", "未成年人", "青少年模式", "兑换券", "扣费",
    # the SaaS tenant (Iris云文档): plan names, also the voice channel's hotwords there
    "免费版", "专业版", "企业版",
)

try:
    import jieba

    jieba.setLogLevel(logging.WARNING)
    for _word in DOMAIN_WORDS:
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
CITE_RE = re.compile(r"[\[［](\d{1,2})[\]］]")  # models also write full-width ［1］
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


# A refund is filed only when the customer asks for one (N-091). Declines ("先别退款", "我不想退款", "不退款，我要换货",
# "退款就算了", "No refund") are cut out first. Then an explicit request ("帮我退", "申请退款", "如果不能换货就退款",
# "please refund") files one, unless the message says it is only asking ("就问问", "假如", "what if"); any other
# question about refunds ("如果退款的话…", "退款是原路退回吗", "refund rules?") is answered, not filed; a bare
# statement ("SO… 退货退款") is still a request. A conditional followed by a question is not a request
# (N-095, `_asks_what_if`): "如果我申请退款，优惠券会退吗" asks what would happen.
REFUND_RE = re.compile(r"退款|退钱|退货|退掉|退我|refund", re.IGNORECASE)
REFUND_APPLY_RE = re.compile(
    r"我要退|申请退|帮我退|给我退|想退|要退|退一下|退掉|请(?:帮我|直接)?退|直接退|退款吧|退了吧|退款给我|把钱退|就(?:直接)?退"
    r"|退我(?!的)|(?:给|帮|为)(?:客户|顾客|他|她|这位顾客)退"
    r"|(?:want|need|request|issue|process|like|get)\s+(?:a\s+|my\s+|the\s+)?refund|refund\s+(?:me|it|my|this|the|order)"
    r"|please\s+refund",
    re.IGNORECASE)
NEGATED_REFUND_RE = re.compile(
    r"(?:不是要?|不是想|不想要?|不要|不需要|不用|不打算|没打算|没说要|没想|并不想|先别|别|暂时不|暂不|先不)(?:给我|帮我)?"
    r"(?:申请|开|发起)?(?:退款|退钱|退费|退货|退掉|退了|退我的钱)"
    r"|(?<!为什么)(?<!怎么)(?<!凭什么)(?<!还)不退(?:款|货|钱)(?!.{0,2}就)了?|(?:先|就|还是)?不退了"
    r"|(?:退款|退货)(?:就|先|还是)?(?:算了|不必了|不用了|不要了|放一放)|取消(?:这笔|这个)?退款(?:申请)?"
    r"|(?:don'?t|do\s+not|didn'?t|did\s+not|haven'?t|have\s+not|never)\s+(?:want(?:ed)?|need(?:ed)?|ask(?:ed)?\s+for"
    r"|request(?:ed)?)\s+(?:a\s+|any\s+|the\s+)?refunds?"
    r"|not\s+(?:asking\s+for|requesting|wanting|looking\s+for|a)\s+(?:a\s+)?refunds?|(?<![A-Za-z])no\s+refunds?",
    re.IGNORECASE)
# Only asking, even next to request words ("我没说要退款，就问问", "What if I want a refund…").
_ONLY_ASKING_RE = re.compile(r"只是问问|就问问|问问而已|随便问问|先问问|假如|假设|万一|just\s+asking|just\s+wondering|what\s+if",
                             re.IGNORECASE)
# Asking about refunds, unless a request is made too ("如果不能换货就退款").
REFUND_INQUIRY_RE = re.compile(
    _ONLY_ASKING_RE.pattern + r"|如果|要是|(?:退款|退货|退)的话|咨询一下|想?问一?下|规则|政策|流程|吗|[？?]|多久|多长时间|怎么|如何"
    r"|是否|能不能|可不可以|会不会|有没有|(?<![A-Za-z])(?:if|how|what|when|does|do|is|can|will)(?![A-Za-z])",
    re.IGNORECASE)


# A conditional ("如果 / 要是 / 若 / 一旦 / if …", "申请退款的话 / 后 / 的时候", "想问一下申请退款…") followed by a question
# asks what would happen (N-095). Request words still count when they come before the condition ("我要退款，如果可以的话
# 今天能到账吗"), right after its consequence ("的话 / 那就 / 就帮我退 / please": "如果可以的话帮我退款，多久到账"), or
# when no question follows ("如果不能换货就退款", "如果坏了帮我退款", "if it's broken please refund"). A bare "就退款" does
# not end the condition: "如果我今天就退款，优惠券还能退吗" is a question, so "如果不能换货就退款，多久到账" is answered too.
_CONDITION_RE = re.compile(
    r"如果|(?<![需主只想])要是|倘若|若(?!干)|一旦|(?<![A-Za-z])if(?![A-Za-z])|想?问一?下|咨询一下"
    r"|(?:" + REFUND_APPLY_RE.pattern + r")[款货钱]?了?(?:的话|之?后|以后|的时候|时|会不会|需要|要不要)",
    re.IGNORECASE)
_QUESTION_RE = re.compile(
    r"吗|[？?]|是不是|会不会|能不能|可不可以|还能|还在|多久|多长时间|几天|多少|哪|谁|什么|怎么|如何|是否|有没有"
    r"|呢(?=[\s，,。！!]|$)|(?:应该|会|是)[^，,。！!？?]{0,10}吧"
    r"|(?:^|[,，.;；]\s*)(?:how|what|when|where|will|would|does|do|is|are|can|could)(?![A-Za-z])"
    r"|(?<![A-Za-z])(?:will|would|does|do|is|are|can|could)\s+(?:i|my|it|the|you|we|there|shipping|this|that)(?![A-Za-z])",
    re.IGNORECASE)
_CLAUSE_RE = re.compile(r"[^，,。.！!？?；;\n]+")
_CONSEQUENCE_RE = re.compile(r"的话|那么|那就|就(?=(?:帮我|给我|直接|请)退)|(?<![A-Za-z])(?:then|please)(?![A-Za-z])",
                             re.IGNORECASE)


def _asks_what_if(text: str) -> bool:
    """A conditional followed by a question, with no request outside the condition."""
    condition = _CONDITION_RE.search(text)
    if not condition or not _QUESTION_RE.search(text, condition.start()):
        return False
    for clause in _CLAUSE_RE.finditer(text):
        part = clause.group(0)
        for request in REFUND_APPLY_RE.finditer(part):
            if _QUESTION_RE.search(part, request.end()):
                continue  # "…，钱会退我原来的卡吗" asks
            starts = [m.start() for m in _CONDITION_RE.finditer(part) if m.start() <= request.start()]
            if not starts:
                if not _QUESTION_RE.search(part[:request.start()]):
                    return False  # "我要退款，如果…", "refund me if it has not shipped, how long…"
                continue
            consequence = _CONSEQUENCE_RE.search(part, starts[-1])
            if starts[-1] < request.start() and consequence and consequence.start() <= request.start():
                return False  # "如果可以的话帮我退款，多久到账", "要是今天不发货就给我退款，可以吗"
    return True


# A complaint followed by a bare demand ("还不发货？？？ 不要了 退钱！！！", "怎么还不发货 退款", "等了这么久还不发？退款！",
# "SO… 退了吧") is a request (N-126): the question belongs to the complaint. Only a clause that is nothing but the demand
# counts, and only when no question follows it: "怎么还不发货？退款已经申请了", "咋回事？退款金额不对", "应该退了吧？怎么还没到账",
# "退款，多久到账" and "退款 咋弄" are not requests. Clauses end at punctuation, or at a space next to a Chinese character
# ("还不发货 退款", "还不发货 refund"; "how do I get a refund" stays one clause).
_DEMAND_SEP_RE = re.compile(r"[，,。.！!？?；;、～~…\n]+|(?<=[\u4e00-\u9fff])\s+|\s+(?=[\u4e00-\u9fff])")
# Possessive quantifiers and a length cap: "那就" and "退货退款" can also be read as two pieces, and a failing fullmatch
# would otherwise try every split ("那就" * 26 + "退了吧z" took over 15 s). Whitespace is gone by the time this runs.
# No "了 / 啦" ending: "怎么回事？都退款了" says it was refunded. "退了吧" after a question is still read as a demand.
_BARE_DEMAND_RE = re.compile(
    r"(?:那|就|不要了|不等了|麻烦|请|赶紧|赶快|快点?|马上|立刻|直接|全额|全部)*+"
    r"(?:(?:退款|退钱|退货|退掉)++(?:吧|啊|呀|哈|呗|好了|行了|谢谢)*+|(?:都|全)?退了吧(?:谢谢)?)"
    r"|(?:please)?(?:full)?refund(?:now|please|me|it|asap)*+", re.IGNORECASE)
_BARE_DEMAND_MAX = 16
_DEMAND_FILLER_RE = re.compile(r"\s+|(?:SO\d+(?:和|跟|与|及|and)?)+|[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F]+",
                               re.IGNORECASE)
# Colloquial questions the inquiry patterns miss: 咋 / 啥, 行不行 / 快不快, a trailing 不 or 没 ("要手续费不", "到账没").
_COLLOQUIAL_QUESTION_RE = re.compile(r"咋|啥|([\u4e00-\u9fff])不\1|[不没](?=[\s，,。.！!？?～~]|$)")


def _demands_refund(text: str) -> bool:
    questions = [m.start() for pattern in (REFUND_INQUIRY_RE, _QUESTION_RE, _COLLOQUIAL_QUESTION_RE)
                 for m in pattern.finditer(text)]
    last_question = max(questions, default=-1)
    start = 0
    for sep in [*_DEMAND_SEP_RE.finditer(text), None]:
        end = sep.start() if sep else len(text)
        bare = _DEMAND_FILLER_RE.sub("", text[start:end])
        if start > last_question and 0 < len(bare) <= _BARE_DEMAND_MAX and _BARE_DEMAND_RE.fullmatch(bare):
            return True
        start = sep.end() if sep else end
    return False


def refund_requested(text: str) -> bool:
    """True when the message asks for a refund, not when it asks about one or declines one."""
    kept = NEGATED_REFUND_RE.sub("，", normalize_width(text or ""))
    if _ONLY_ASKING_RE.search(kept):
        return False
    if not REFUND_RE.search(kept):
        return _demands_refund(kept)  # "SO… 退了吧": not one of the refund words, but a bare demand
    if _asks_what_if(kept):
        return False
    return bool(REFUND_APPLY_RE.search(kept)) or not REFUND_INQUIRY_RE.search(kept) or _demands_refund(kept)


def refund_declined(text: str) -> bool:
    """The message mentions a refund but does not ask for one: filing one would go against it."""
    text = normalize_width(text or "")
    return bool(REFUND_RE.search(text) or NEGATED_REFUND_RE.search(text)) and not refund_requested(text)

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
