"""Evidence verifier: checks an answer sentence by sentence against the run's evidence.

For every factual sentence it asks two questions:
1. Do all numbers in the sentence appear in the evidence? (amounts, days, hours)
   A wrong number is the costliest support failure, so one unverified number makes
   the whole answer ``blocked``.
2. Is the wording grounded? Measured as token recall against the cited evidence
   (or all evidence if nothing is cited), for the sentence and for each of its clauses: a true
   clause must not carry an invented one past the check (N-183). Optionally an LLM judge replaces this check
   for paraphrasing models (LOOPDESK_VERIFIER=llm); the number rule always stays.
3. Does it contradict a tool result? A sentence can be well grounded in the refund
   policy ("arrives in 1-3 working days") and still be wrong because the refund was
   rejected or failed. Claiming money is coming when it is not, a ticket or a handoff no
   tool made, or deciding a requested refund without the refund tool blocks the answer
   (status_templates.py; the runtime renders the status sentences themselves from the tools).

Output: per-sentence results, ``coverage`` (= supported factual sentences / factual
sentences) and a verdict pass / warn / blocked.

LOOPDESK_VERIFIER=tiered (``_tiered``) checks claims only, in tiers, cheapest first:
- claim or not (claims.py: rules, or the fine-tuned encoder behind a flag); a number always is one;
- rules: numbers, order ids and the tool state, always, and never overruled by a model;
- grounding (grounding.py): the sentence, its weakest clause, and rule flags for wording that turns a
  grounded sentence wrong (polarity, party, time unit, promise, relative day);
- the LLM judge only for the claims where those tiers disagree (grounded but flagged, partly grounded),
  with a deadline; without a judge such a claim counts as unsupported.
``plan_repair`` then says what to do with a failed answer: FIX (drop the failed non-numeric claims when
what is left still answers), else one REASK with the reasons, else hand over to a person.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field

from ..config import Settings
from ..llm.base import LLMMessage, LLMProvider
from . import claims as claim_rules
from .grounding import best_sentence_score, gloss, ground
from ..textutil import (CITE_RE, ORDER_ID_RE, extract_numbers, normalize_width, overlap_recall, split_sentences,
                        strip_citations, token_set)
from .outcome import REFUSAL_RE
from .prompts import JUDGE_PROMPT
from .status_templates import case_state, contradiction, pieces
from .tools import Evidence

_NON_FACTUAL = re.compile(r"^(请|如需|如有|如果您|建议|还有什么|欢迎|感谢|为避免|麻烦|稍等)")
# Courtesy opening a sentence. What follows it is checked like any sentence: "您好，本店支持
# 终身保修" used to pass unchecked, because the whole sentence counted as a greeting.
_COURTESY = re.compile(r"^(您好|你好|抱歉|很抱歉|非常抱歉|不好意思|感谢您?的?(耐心|关注|咨询|反馈|支持)|另外|此外|同时|好的|收到|明白|了解)[，,！!、]?\s*")
# A list number opening a sentence ("1. ", "2、", "（3）") is not an amount.
_LIST_NUMBER = re.compile(r"^(\d{1,2}[.、．)）]|[（(]\d{1,2}[)）])\s*")
# Clauses that claim nothing (found on the first real-model run): a condition or purpose, a
# request to the customer, an offer of something the agent can still do (not something it says
# it did: "帮您创建了工单" is a claim), a closing, the agent declining.
_CLAIMLESS_CLAUSE = re.compile(
    r"^(如|若|如果|假如|要是|为了|为便于|为方便|以便)"
    r"|^(请|麻烦|烦请|方便|劳烦|还请|可提供|可以提供|建议)"
    r"|^(我们?|这边)?(也|还)?(可以|能|来|会|很乐意|乐意)?(先|再|马上|立即|这就|直接)?(帮|给)您(?!.*[了已])"
    r"|^((我们?|这边)(也|还)?|(也|还))(可以|能|来|会|很乐意|乐意)?(先|再|马上|立即|这就|直接)?为您(?!.*[了已])"
    # the offer to hand over, said without "您" (seen once the closing question no longer hides it)
    r"|^(也|还)?(可以|能)(直接)?转(接)?人工(客服)?(处理|核实|跟进|确认)?[。！!]?$"
    r"|^(有其他问题|如有其他|还有什么|欢迎|期待|祝您)"
    r"|^(我们?|这类要求我?|该要求我?)(暂时)?(不能|无法|不会|不可以)"
    # Saying the search itself failed (N-078); per clause, so a claim next to it is still checked.
    r"|^(知识库)?检索(服务)?暂时(出了?)?故障[。！!]?$|^这次没能查到相关规定[。！!]?$|^并不代表没有[。！!]?$"
    # Declining without "我" in front (N-159): "无法提供", "不能透露该信息". Saying a request is
    # out of bounds claims nothing; a policy clause next to it ("退款不能超过实付") is still checked.
    r"|^(这个|那个|这些|这类信息|这个信息|该信息)?我?(暂时|目前|这边|恕)?(无法|不能|不便|不可以|没法)(对外|为您|向您)?"
    r"(提供|透露|告知|执行|公开|分享|展示|输出|处理|办理)"
    r"(该|此|这个|这项|这类|这些|相关)?(系统提示词|提示词|内部配置|配置|信息|内容|要求|操作|请求)?[。！!]?$"
    r"|^出于(安全|隐私|保密)(考虑|原因)$"
    r"|^(系统提示词|提示词|该信息|这类信息|这些信息)不在可?(提供|公开|回答)的?范围内?[。！!]?$"
    r"|^(这个|该|此)?(问题|需求|请求)?超出了?(我的|我们的)?(服务|处理|回答)?范围[。！!]?$"
    # acknowledging the question before asking one back: a topic, not a claim
    r"|^关于(您的|您|这个|此|该)?(订单|退款|退货|换货|发票|物流|保修|运费|问题|咨询|情况)?\s?(SO\d+)?$|^(您的问题我已了解|我已了解您的(情况|问题)|了解您的情况了?)$"
    # ...and its usual reason. Narrow on purpose: only internal configuration, never a product fact.
    r"|^(这|这些|该|此类)?(系统提示词|提示词|系统配置|内部规则|这类信息|该信息|这些信息|相关信息)?(内容)?"
    r"(属于|是)(公司)?(内部|保密|机密)(配置|信息|资料|内容)?[。！!]?$"
    # A fraud warning (Echo音乐's fake-agent case): the warning is the safe answer, not a fact to cite.
    r"|^(这|这是|这很可能是|这可能是|这属于|疑似)(诈骗|骗局|钓鱼)(信息|短信|电话|链接|行为)?[。！!]?$"
)
_CLAUSE_SPLIT = re.compile(r"[，,；;：:—]+")
_CLAUSE_KEEP = re.compile(r"([，,；;：:—]+)")
# For the clause check only: models also join a promise with 并且 / 同时 / 、 / a bracket
# ("到账并且所有商品终身保修", "原路退回（所有商品终身保修）").
_CLAUSE_JOIN = re.compile(r"([，,；;：:—、（）()]+|并且|而且|同时|另外|以及|(?<=[\u4e00-\u9fff])且(?=[\u4e00-\u9fff]))")
# A clause that says what is not possible or not done, said with the negation in front ("无需重复
# 提交", "故未受理") or as the agent's own limit ("审核前无法承诺结果"). An invented limit costs the
# customer an answer; an invented promise costs the business money, and "不会收取手续费",
# "没有时间限制", "未拆封的随时可退" are promises, so the negation has to lead or name a limit.
_LIMIT = re.compile(
    r"^(也|还|但|且|故|均|都|暂时?|目前|因此|所以|系统)?"
    r"(无法|不能|不支持|不予|不要(?!求)|没能|未(能|受理|发起|提交|找到|查到|查询|发货|签收)"
    # a waiver ("无需原包装", "不必审核") loosens a condition, so only "no need to send it twice" counts
    r"|(无需|不必|不用)(重复|再次|再))"
    r"|(无法|不能)(承诺|保证|重复|再次|在线)|不符合|不满足|未找到|未查询到|不属于|已(被)?(拒绝|驳回)")
# A clause that only says where the next one comes from ("根据订单信息"), offers a person, or hedges
# the timing the clause before it gave ("具体到账时间以银行为准", "请耐心等待").
_LEAD_IN = re.compile(r"^(根据|按照|依据|查询到?|经查)[^，,]{0,8}(信息|记录|结果|规定|政策|显示)$"
                      r"|^(或|或者)?(也)?(可以|可)?(转|联系)人工(客服)?(核实|处理|确认|跟进)?[。！!]?$"
                      r"|^(具体)?(的)?(到账|退款|处理|审核|发货|配送)?(时间|金额|时效|进度|结果)?以[^，,]{1,8}为准[。！!]?$"
                      r"|^(请|还请)?(您)?耐心等待(即可)?[。！!]?$|^届时|^(请)?(您)?(注意)?留意")
# Something that costs the business: checked even when it is phrased as an offer ("我们会为您承担
# 运费") or is a single word ("免运费").
_PERK = re.compile(r"运费|补偿|赠|免费|免单|积分|券|换新|红包|返现|包邮|加急|保修|保价|退差价|不限|终身|任何时候"
                   r"|随时(都)?(可以?|能)?(退|换|取消|申请)")
# ...unless the clause asks, sets a condition or closes ("如需加急可转人工", "建议您保留好赠品").
_ASKS = re.compile(r"^(请|如需|如|若|建议|有问题|欢迎|麻烦|为便于|以便|您可以|可以联系)|^(我们)?(会)?随时为您")
# A clause that asks rather than states (N-159). Only trailing clauses that plainly ask are dropped:
# ending in a question particle or mark, opening like a question, or the first half of an either-or
# ("您是想退货，还是换货？"). "吧" is left out ("2小时就到账了吧" asserts), and so are openers that
# can tell as well as ask ("您需要支付运费15元", "或者退到余额").
_QUESTION_END = re.compile(r"(吗|呢|么|嘛)?[？?]$")
_QUESTION_OPENER = re.compile(r"^(请问|是否|能否|可否|要不要|需不需要|您看|方便的话)")
_EITHER_OR_TAIL = re.compile(r"^(还是|或是|或者)[^，,]{1,8}[？?]$")
_EITHER_OR_HEAD = re.compile(r"^(请问)?(您|你)?(是想|是要|想要|想|要)[^了已]{1,8}$")
# Asking with a number or an order id in it is rare; checking it costs nothing when it is right.
# A rhetorical "不都是…吗" asserts.
_ASSERTS = re.compile(r"\d|不都是|不就是|难道|不是.{0,12}吗")


@dataclass
class SentenceCheck:
    text: str
    factual: bool
    supported: bool
    score: float
    cited: list[int] = field(default_factory=list)
    best_ref: int | None = None
    unverified_numbers: list[float] = field(default_factory=list)
    unknown_ids: list[str] = field(default_factory=list)
    weak_clause: str | None = None  # the clause with no basis in an otherwise grounded sentence (N-183)
    contradicts: str | None = None  # request_refund | under_action | ticket | handoff (status_templates)
    source: str = "model"  # model | template (rendered from tool fields: supported by construction)
    # tiered mode: which tier decided, why a claim failed, the weakest clause and the rule flags
    tier: str = ""  # rules | grounding | judge | template | classifier
    reason: str = ""  # number | order_id | status | unsupported | flagged | unsure
    clause_score: float | None = None
    flags: list[str] = field(default_factory=list)
    dropped: bool = False  # FIX took it out of the answer: the customer never saw it


@dataclass
class Verification:
    verdict: str  # pass | warn | blocked
    coverage: float
    sentences: list[SentenceCheck]
    invalid_citations: list[int]
    method: str = "lexical"
    disputed: int = 0  # tiered: claims the cheap tiers disagreed on (what a judge is asked about)
    judge_calls: int = 0  # tiered: model calls made for this answer (0 when the cheap tiers agreed)
    judged: int = 0  # tiered: sentences the judge was asked about
    judge_ms: int = 0
    judge_failed: bool = False  # tiered: the judge gave no usable verdict in time (the claims stay unsupported)
    classifier: str = ""  # tiered: rules | model

    def to_dict(self) -> dict:
        data = asdict(self)
        data["coverage"] = round(self.coverage, 3)
        return data


def _claim(text: str) -> str:
    """The sentence without an opening courtesy or list number."""
    return _LIST_NUMBER.sub("", _COURTESY.sub("", text), count=1)


def _statement_part(claim: str) -> str:
    """What a question-ending sentence states before it asks (N-159).

    "本店终身保修，请问还有什么可以帮您？" used to skip the check because of its last character, so
    any claim could ride on a polite closing question. Only the trailing question clauses are
    dropped; the statement in front is checked like the same sentence without the question.
    """
    if not claim.endswith(("？", "?")):
        return claim
    parts = _CLAUSE_KEEP.split(claim)
    # (clause, separator after it); separators are kept so a label before a colon stays a label
    pairs = [(parts[i].strip(), parts[i + 1] if i + 1 < len(parts) else "") for i in range(0, len(parts), 2)]
    pairs = [(c, sep) for c, sep in pairs if c]
    if not pairs or _ASSERTS.search(pairs[-1][0]) or not _QUESTION_END.search(pairs[-1][0]):
        return _joined(pairs)
    # the last clause carries the question mark; a clause before it is dropped only when it opens
    # like a question or is the head of a short either-or ("本店终身保修呢，请问…" keeps its claim)
    either_or = bool(_EITHER_OR_TAIL.match(pairs.pop()[0]))
    while pairs and not _ASSERTS.search(pairs[-1][0]) and (
            _QUESTION_OPENER.match(pairs[-1][0]) or (either_or and _EITHER_OR_HEAD.match(pairs[-1][0]))):
        pairs.pop()
    return _joined(pairs)


def _joined(pairs: list[tuple[str, str]]) -> str:
    return "".join(c + (sep if i < len(pairs) - 1 else "") for i, (c, sep) in enumerate(pairs))


def _claims_nothing(text: str) -> bool:
    if _NON_FACTUAL.match(text) or REFUSAL_RE.search(text):  # saying there is no basis claims nothing
        return True
    if re.search(r"如下[:：]$", text):  # "物流信息如下：" introduces the facts that follow
        return True
    clauses = [c.strip() for c in _CLAUSE_SPLIT.split(text) if c.strip()]
    return bool(clauses) and all(_CLAIMLESS_CLAUSE.match(c) for c in clauses)


def _clauses_to_check(claim: str) -> list[str]:
    """The clauses of a several-clause claim that state something on their own.

    "退款1-3个工作日到账，所有商品终身保修" scored 0.6 as one sentence and passed: the true clause
    carried the invented one. Left to the sentence score: a clause that claims nothing, a label
    in front of a colon ("唯一例外："), a limit ("无需重复提交", "审核前无法承诺结果": an invented
    limit costs the customer an answer, an invented promise costs the business money), and a
    clause with fewer than two words besides its numbers ("金额 45 元": the number rule checks it).
    """
    parts = _CLAUSE_JOIN.split(claim)
    clauses = [(parts[i].strip(), parts[i + 1] if i + 1 < len(parts) else "") for i in range(0, len(parts), 2)]
    clauses = [(c, sep) for c, sep in clauses if c]
    if len(clauses) < 2:
        return []
    checked = []
    for c, sep in clauses:
        words = [t for t in token_set(c) if not re.fullmatch(r"[\d.]+", t)]
        if _PERK.search(c) and not _LIMIT.search(c) and not _ASKS.match(c):
            checked.append(c)
        elif not (_CLAIMLESS_CLAUSE.match(c) or _ASKS.match(c) or _LEAD_IN.search(c) or re.search(r"[：:]", sep)
                  or _LIMIT.search(c) or len(words) < 2):
            checked.append(c)
    return checked


def _numbers_match(value: float, pool: list[float]) -> bool:
    return any(abs(value - other) < 1e-6 for other in pool)


def verify_answer(
    answer: str,
    evidence: list[Evidence],
    user_text: str,
    settings: Settings,
    judge: LLMProvider | None = None,
    judge_model: str | None = None,
    trusted: list[str] | None = None,
) -> Verification:
    """``trusted``: the status sentences the runtime rendered from tool fields (status_templates)."""
    if settings.verifier == "tiered":
        return _tiered(answer, evidence, user_text, settings, judge, judge_model, trusted)
    by_ref = {e.ref: e for e in evidence}
    evidence_text = "\n".join(e.text for e in evidence)
    number_pool = extract_numbers(evidence_text) + extract_numbers(user_text)
    known_ids = {m.upper() for m in ORDER_ID_RE.findall(evidence_text + " " + user_text)}
    state = case_state(evidence, user_text)
    rendered = {strip_citations(p)[0] for t in trusted or [] for p in split_sentences(t)}

    checks: list[SentenceCheck] = []
    invalid: set[int] = set()
    evidence_tokens: list[set[str]] | None = None
    for raw in split_sentences(answer):
        text, cited = strip_citations(raw)
        if not text:
            continue
        invalid.update(r for r in cited if r not in by_ref)
        if text in rendered:
            checks.append(SentenceCheck(text=text, factual=True, supported=True, score=1.0, source="template"))
            continue
        claim = _statement_part(_claim(text))
        numbers = extract_numbers(claim)
        factual = not (
            len(claim) < 6
            or (not numbers and _claims_nothing(claim))
        )
        # A status claim counts even when phrased politely ("请放心，钱会到账").
        contradicts = contradiction(text, state)
        check = SentenceCheck(text=text, factual=factual or bool(contradicts), supported=True, score=1.0,
                              cited=cited, contradicts=contradicts)
        if check.factual:
            check.unverified_numbers = [n for n in numbers if not _numbers_match(n, number_pool)]
            check.unknown_ids = [i.upper() for i in ORDER_ID_RE.findall(claim) if i.upper() not in known_ids]
            candidates = [by_ref[r] for r in cited if r in by_ref] or evidence
            best_score, best_ref = 0.0, None
            for item in candidates:
                score = overlap_recall(claim, item.text + " " + str(item.meta.get("heading", "")))
                if score > best_score:
                    best_score, best_ref = score, item.ref
            check.score, check.best_ref = round(best_score, 3), best_ref
            threshold = 0.3 if numbers else settings.sentence_support_threshold
            grounded = best_score >= threshold
            # Each clause clears its own bar against any evidence (a sentence often cites one chunk
            # and paraphrases two); the lower bar for numbers applies only to the clause that has
            # them: a checked number vouches for its own clause, not for its neighbours.
            for clause in _clauses_to_check(claim) if grounded else []:
                bar = 0.3 if extract_numbers(clause) else settings.sentence_support_threshold
                if evidence_tokens is None:  # tool payloads can be long: tokenize them once per answer
                    evidence_tokens = [token_set(i.text + " " + str(i.meta.get("heading", ""))) for i in evidence]
                words = token_set(clause)
                if words and max((len(words & t) / len(words) for t in evidence_tokens), default=0.0) < bar:
                    check.weak_clause, grounded = clause, False
                    break
            check.supported = grounded and not check.unverified_numbers and not check.unknown_ids \
                and not check.contradicts
        checks.append(check)

    method = "lexical"
    if judge is not None and evidence:
        labels = _judge(judge, judge_model or "", checks, evidence)
        if labels is not None:
            method = "llm"
            for check, label in zip(_judged(checks), labels):
                check.weak_clause = None  # the judge read the whole sentence
                check.supported = (bool(label) and not check.unverified_numbers and not check.unknown_ids
                                   and not check.contradicts)

    factual_checks = [c for c in checks if c.factual]
    supported = sum(1 for c in factual_checks if c.supported)
    coverage = supported / len(factual_checks) if factual_checks else 1.0
    hard_problem = any(c.unverified_numbers or c.unknown_ids or c.contradicts for c in factual_checks)
    if hard_problem or coverage < 0.5:
        verdict = "blocked"
    elif coverage >= settings.evidence_pass_coverage and not invalid:
        verdict = "pass"
    else:
        verdict = "warn"
    return Verification(verdict, coverage, checks, sorted(invalid), method)


def _judged(checks: list[SentenceCheck]) -> list[SentenceCheck]:
    """The sentences a judge reads: claims the model wrote (a rendered status sentence is the tool's)."""
    return [c for c in checks if c.factual and c.source != "template"]


def _judge(provider: LLMProvider, model: str, checks: list[SentenceCheck], evidence: list[Evidence]) -> list[bool] | None:
    factual = _judged(checks)
    if not factual:
        return []
    prompt = JUDGE_PROMPT.format(
        evidence="\n".join(f"[{e.ref}] {e.text}" for e in evidence),
        sentences="\n".join(f"{i + 1}. {c.text}" for i, c in enumerate(factual)),
    )
    try:
        result = provider.chat([LLMMessage(role="user", content=prompt)], [], model, {"task": "judge", "temperature": 0})
        match = re.search(r"\{.*\}", result.content, re.S)
        labels = json.loads(match.group(0))["supported"] if match else None
    except Exception:  # judge failures fall back to the lexical result
        return None
    if not isinstance(labels, list) or len(labels) != len(factual):
        return None
    return [bool(x) for x in labels]


# ---------------------------------------------------------------- tiered (LOOPDESK_VERIFIER=tiered)
TIERED_LOW = 0.2  # below this nothing in the evidence supports the claim (reported as "unsupported")
CLAUSE_MIN = 0.5  # a claim clause with fewer of its words in the evidence is not grounded
SPLICE_MIN = 0.5  # grounded overall but no single evidence sentence holds half the claim: facts spliced


def _tiered(answer: str, evidence: list[Evidence], user_text: str, settings: Settings,
            judge: LLMProvider | None, judge_model: str | None, trusted: list[str] | None) -> Verification:
    by_ref = {e.ref: e for e in evidence}
    evidence_text = "\n".join(e.text for e in evidence)
    number_pool = extract_numbers(evidence_text) + extract_numbers(user_text)
    known_ids = {m.upper() for m in ORDER_ID_RE.findall(evidence_text + " " + user_text)}
    state = case_state(evidence, user_text)
    rendered = {strip_citations(p)[0] for t in trusted or [] for p in split_sentences(t)}
    worded = {e.ref: f"{e.text} {e.meta.get('heading', '')} {gloss(e.title, e.text) if e.kind == 'tool' else ''}"
              for e in evidence}  # tool fields in words: "refundable": true is "可申请退款"
    corpus = "\n".join(worded.values())
    classify = claim_rules.classifier(settings)
    raw = [(r, *strip_citations(r)) for r in split_sentences(answer)]
    raw = [(r, t, c) for r, t, c in raw if t]
    probs = classify.claim_probabilities([t for _, t, _ in raw])
    threshold = classify.threshold

    checks: list[SentenceCheck] = []
    invalid: set[int] = set()
    unsure: list[SentenceCheck] = []
    earlier_ids: list[str] = []  # order numbers named so far: "- 状态：已签收" in a listing is about the last one
    for (_, text, cited), prob in zip(raw, probs):
        contradicts = contradiction(text, state, earlier_ids)
        earlier_ids += ORDER_ID_RE.findall(normalize_width(text))
        invalid.update(r for r in cited if r not in by_ref)
        if text in rendered:
            checks.append(SentenceCheck(text=text, factual=True, supported=True, score=1.0, source="template",
                                        tier="template"))
            continue
        claim = claim_rules.claim_text(text)
        numbers = extract_numbers(claim)
        # numbers and order ids are always checked by rule, whatever the classifier says
        factual = bool(numbers) or bool(ORDER_ID_RE.search(claim)) or bool(contradicts) or prob >= threshold
        check = SentenceCheck(text=text, factual=factual, supported=True, score=1.0, cited=cited,
                              contradicts=contradicts, tier="classifier")
        checks.append(check)
        if not factual:
            continue
        check.unverified_numbers = [n for n in numbers if not _numbers_match(n, number_pool)]
        # an order number given as an example of the format ("例如 SO2026…") names no order
        check.unknown_ids = [m.group(0).upper() for m in ORDER_ID_RE.finditer(claim)
                             if m.group(0).upper() not in known_ids
                             and not re.search(r"(例如|比如|格式如|形如|如)\s*$", claim[max(0, m.start() - 4):m.start()])]
        if check.unverified_numbers or check.unknown_ids or contradicts:
            check.supported, check.tier = False, "rules"
            check.reason = "number" if check.unverified_numbers else "order_id" if check.unknown_ids else "status"
            continue
        if prob < threshold and not numbers:
            check.tier = "rules"  # framing that names a known order ("回复“SO… 申请退款”即可"): only the id is a claim
            continue
        items = [by_ref[r] for r in cited if r in by_ref] or evidence
        g = ground(claim, [(e.ref, worded[e.ref]) for e in items], user_text, corpus)
        check.score, check.best_ref, check.clause_score, check.flags = g.sentence_score, g.best_ref, g.clause_score, g.flags
        check.tier = "grounding"
        grounded = g.sentence_score >= (0.3 if numbers else settings.sentence_support_threshold)
        spliced = grounded and best_sentence_score(claim, corpus) < SPLICE_MIN
        if spliced:
            check.flags.append("spliced")
        if grounded and g.clause_score >= CLAUSE_MIN and not check.flags:
            continue  # the rules find nothing wrong and the evidence grounds it: supported
        # The rules find nothing wrong but the grounding fails, or it is grounded but a flag fires: the tiers
        # disagree, and only here may a judge decide. Without one the claim counts as unsupported.
        check.supported = False
        check.reason = "flagged" if check.flags else "unsupported" if g.sentence_score < TIERED_LOW else "unsure"
        unsure.append(check)

    verification = Verification("pass", 1.0, checks, sorted(invalid), "tiered", classifier=classify.name,
                                disputed=len(unsure))
    if unsure and judge is not None and evidence:
        started = time.monotonic()
        labels = _judge_some(judge, judge_model or "", unsure, evidence,
                             deadline=started + getattr(settings, "verifier_judge_seconds", 8.0))
        verification.judge_calls, verification.judged = 1, len(unsure)
        verification.judge_ms = int((time.monotonic() - started) * 1000)
        verification.judge_failed = labels is None
        if labels is not None:
            for check, label in zip(unsure, labels):
                check.supported, check.tier = bool(label), "judge"
                check.reason = "" if label else "unsupported"
    _tiered_verdict(verification, settings)
    return verification


def _tiered_verdict(v: Verification, settings: Settings) -> None:
    factual = [c for c in v.sentences if c.factual]
    supported = sum(1 for c in factual if c.supported)
    v.coverage = supported / len(factual) if factual else 1.0
    if any(c.unverified_numbers or c.unknown_ids or c.contradicts for c in factual):
        v.verdict = "blocked"
    elif supported == len(factual) and not v.invalid_citations:
        v.verdict = "pass"
    else:
        v.verdict = "warn" if v.coverage >= 0.5 else "blocked"


def _judge_some(provider: LLMProvider, model: str, checks: list[SentenceCheck], evidence: list[Evidence],
                deadline: float) -> list[bool] | None:
    prompt = JUDGE_PROMPT.format(
        evidence="\n".join(f"[{e.ref}] {e.text}" for e in evidence),
        sentences="\n".join(f"{i + 1}. {c.text}" for i, c in enumerate(checks)),
    )
    try:
        result = provider.chat([LLMMessage(role="user", content=prompt)], [], model,
                               {"task": "judge", "temperature": 0, "deadline": deadline})
        match = re.search(r"\{.*\}", result.content, re.S)
        labels = json.loads(match.group(0))["supported"] if match else None
    except Exception:  # a failed or slow judge leaves the claims unsupported: the answer is repaired, not shown
        return None
    if not isinstance(labels, list) or len(labels) != len(checks):
        return None
    return [bool(x) for x in labels]


# ---------------------------------------------------------------- repair: FIX, then one REASK, then handoff
@dataclass
class Repair:
    action: str  # pass | fix | reask | handoff
    text: str
    dropped: list[str] = field(default_factory=list)
    problems: list[SentenceCheck] = field(default_factory=list)  # what a re-ask is told about


def plan_repair(v: Verification, answer: str, reasked: bool = False) -> Repair:
    """What to do with a checked answer. Only a non-numeric claim that failed may be dropped, and only when
    what is left still answers: a supported claim remains and at most half the claims go. A wrong number, an
    unknown order id or a status the tools contradict is never dropped silently: the model is asked once
    to correct it, then a person takes over."""
    factual = [c for c in v.sentences if c.factual and c.source != "template"]
    failed = [c for c in factual if not c.supported]
    if not failed and not v.invalid_citations:
        return Repair("pass", answer)
    hard = [c for c in failed if c.unverified_numbers or c.unknown_ids or c.contradicts]
    supported = [c for c in v.sentences if c.factual and c.supported]
    if failed and not hard and supported and len(failed) * 2 <= len(factual):
        return Repair("fix", _drop(answer, {c.text for c in failed}), [c.text for c in failed], failed)
    if not failed:  # only a citation to nothing: drop the marker
        return Repair("fix", CITE_RE.sub(lambda m: "" if int(m.group(1)) in v.invalid_citations else m.group(0), answer))
    return Repair("handoff" if reasked else "reask", answer, [], failed)


def apply_fix(v: Verification, repair: Repair, settings: Settings) -> None:
    """The verdict of what is shown after FIX: the dropped claims are marked, the rest decide."""
    for check in v.sentences:
        if check.text in repair.dropped:
            check.dropped = True
    kept = Verification(v.verdict, v.coverage, [c for c in v.sentences if not c.dropped], [], v.method)
    _tiered_verdict(kept, settings)
    v.verdict, v.coverage = kept.verdict, kept.coverage
    v.invalid_citations = []


def _drop(answer: str, texts: set[str]) -> str:
    kept = []
    for piece in pieces(answer):
        text = strip_citations(piece.strip())[0]
        if text and text in texts:
            continue
        kept.append(piece)
    return "".join(kept).strip()


def reask_message(repair: Repair) -> str:
    """What the model is told when its answer could not be fixed by dropping sentences (sent once)."""
    lines = []
    for i, c in enumerate(repair.problems[:6], 1):
        if c.unverified_numbers:
            why = f"数字 {'、'.join(f'{n:g}' for n in c.unverified_numbers)} 在工具结果和知识库中都没有"
        elif c.unknown_ids:
            why = f"订单号 {'、'.join(c.unknown_ids)} 不在工具结果中"
        elif c.contradicts == "under_action":
            why = "顾客明确要求退款，能不能退、退多少由 request_refund 判断，请调用它，不要自己下结论"
        elif c.contradicts:
            why = "与工具返回的状态不符"
        else:
            why = "在工具结果和知识库中找不到依据"
        lines.append(f"{i}. “{c.text}”：{why}")
    return (REASK_MARKER + "上一版回答没有发给顾客，因为以下句子没有通过核对：\n" + "\n".join(lines)
            + "\n请只依据工具结果和知识库，重新回答顾客刚才的问题：改正或删去这些内容，需要时调用工具。不要提到这次核对。")


REASK_MARKER = "（系统核对，不是顾客的话）"
