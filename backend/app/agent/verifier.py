"""Evidence verifier: checks an answer sentence by sentence against the run's evidence.

For every factual sentence it asks two questions:
1. Do all numbers in the sentence appear in the evidence? (amounts, days, hours)
   A wrong number is the costliest support failure, so one unverified number makes
   the whole answer ``blocked``.
2. Is the wording grounded? Measured as token recall against the cited evidence
   (or all evidence if nothing is cited). Optionally an LLM judge replaces this check
   for paraphrasing models (LOOPDESK_VERIFIER=llm); the number rule always stays.
3. Does it contradict a tool result? A sentence can be well grounded in the refund
   policy ("arrives in 1-3 working days") and still be wrong because the refund was
   rejected or failed. Claiming money is coming when it is not, a ticket or a handoff no
   tool made, or deciding a requested refund without the refund tool blocks the answer
   (status_templates.py; the runtime renders the status sentences themselves from the tools).

Output: per-sentence results, ``coverage`` (= supported factual sentences / factual
sentences) and a verdict pass / warn / blocked.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field

from ..config import Settings
from ..llm.base import LLMMessage, LLMProvider
from ..textutil import ORDER_ID_RE, extract_numbers, overlap_recall, split_sentences, strip_citations
from .outcome import REFUSAL_RE
from .prompts import JUDGE_PROMPT
from .status_templates import case_state, contradiction
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
    contradicts: str | None = None  # request_refund | under_action | ticket | handoff (status_templates)
    source: str = "model"  # model | template (rendered from tool fields: supported by construction)


@dataclass
class Verification:
    verdict: str  # pass | warn | blocked
    coverage: float
    sentences: list[SentenceCheck]
    invalid_citations: list[int]
    method: str = "lexical"

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
    clauses = [c.strip() for c in _CLAUSE_SPLIT.split(claim) if c.strip()]
    if not clauses or _ASSERTS.search(clauses[-1]) or not _QUESTION_END.search(clauses[-1]):
        return "，".join(clauses)
    # the last clause carries the question mark; a clause before it is dropped only when it opens
    # like a question or is the head of a short either-or ("本店终身保修呢，请问…" keeps its claim)
    either_or = bool(_EITHER_OR_TAIL.match(clauses.pop()))
    while clauses and not _ASSERTS.search(clauses[-1]) and (
            _QUESTION_OPENER.match(clauses[-1]) or (either_or and _EITHER_OR_HEAD.match(clauses[-1]))):
        clauses.pop()
    return "，".join(clauses)


def _claims_nothing(text: str) -> bool:
    if _NON_FACTUAL.match(text) or REFUSAL_RE.search(text):  # saying there is no basis claims nothing
        return True
    if re.search(r"如下[:：]$", text):  # "物流信息如下：" introduces the facts that follow
        return True
    clauses = [c.strip() for c in _CLAUSE_SPLIT.split(text) if c.strip()]
    return bool(clauses) and all(_CLAIMLESS_CLAUSE.match(c) for c in clauses)


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
    by_ref = {e.ref: e for e in evidence}
    evidence_text = "\n".join(e.text for e in evidence)
    number_pool = extract_numbers(evidence_text) + extract_numbers(user_text)
    known_ids = {m.upper() for m in ORDER_ID_RE.findall(evidence_text + " " + user_text)}
    state = case_state(evidence, user_text)
    rendered = {strip_citations(p)[0] for t in trusted or [] for p in split_sentences(t)}

    checks: list[SentenceCheck] = []
    invalid: set[int] = set()
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
            check.supported = grounded and not check.unverified_numbers and not check.unknown_ids \
                and not check.contradicts
        checks.append(check)

    method = "lexical"
    if judge is not None and evidence:
        labels = _judge(judge, judge_model or "", checks, evidence)
        if labels is not None:
            method = "llm"
            for check, label in zip(_judged(checks), labels):
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
