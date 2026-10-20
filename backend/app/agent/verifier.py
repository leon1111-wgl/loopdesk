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
   rejected or failed. Claiming money is coming when it is not blocks the answer.

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
from .tools import Evidence

# Wording that tells the customer money is on its way.
_PAYOUT = re.compile(r"到账|已退款|退款成功|已受理|原支付方式退回|原路退回")
_NON_FACTUAL = re.compile(r"^(请|如需|如有|如果您|建议|还有什么|欢迎|感谢|为避免|麻烦|稍等)")
# Courtesy opening a sentence. What follows it is checked like any sentence: "您好，本店支持
# 终身保修" used to pass unchecked, because the whole sentence counted as a greeting.
_COURTESY = re.compile(r"^(您好|你好|抱歉|很抱歉|非常抱歉|不好意思|感谢您?的?(耐心|关注|咨询|反馈|支持)|另外|此外|同时)[，,！!、]?\s*")
# A list number opening a sentence ("1. ", "2、", "（3）") is not an amount.
_LIST_NUMBER = re.compile(r"^(\d{1,2}[.、．)）]|[（(]\d{1,2}[)）])\s*")
# Clauses that claim nothing (found on the first real-model run): a condition or purpose, a
# request to the customer, an offer of something the agent can still do (not something it says
# it did: "帮您创建了工单" is a claim), a closing, the agent declining.
_CLAIMLESS_CLAUSE = re.compile(
    r"^(如|若|如果|假如|要是|为了|为便于|为方便|以便)"
    r"|^(请|麻烦|烦请|方便|劳烦|还请|可提供|可以提供|建议您)"
    r"|^(我们?|这边)?(可以|能|来|会|很乐意|乐意)?(帮|给)您(?!.*[了已])|^(我们?|这边)(可以|能|来|会|很乐意|乐意)?为您(?!.*[了已])"
    r"|^(有其他问题|如有其他|还有什么|欢迎|期待|祝您)"
    r"|^(我们?|这类要求我?|该要求我?)(暂时)?(不能|无法|不会|不可以)"
)
_CLAUSE_SPLIT = re.compile(r"[，,；;：:—]+")


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
    contradicts: str | None = None  # the tool result this sentence contradicts


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


def _claims_nothing(text: str) -> bool:
    if _NON_FACTUAL.match(text) or REFUSAL_RE.search(text):  # saying there is no basis claims nothing
        return True
    clauses = [c.strip() for c in _CLAUSE_SPLIT.split(text) if c.strip()]
    return bool(clauses) and all(_CLAIMLESS_CLAUSE.match(c) for c in clauses)


def _numbers_match(value: float, pool: list[float]) -> bool:
    return any(abs(value - other) < 1e-6 for other in pool)


def _failed_refund(evidence: list[Evidence]) -> str | None:
    """The message of the last refund call if it was rejected or failed, or the payment system has
    not confirmed it (pending: it may never be paid), else None."""
    for item in reversed(evidence):
        if item.kind != "tool" or item.title != "request_refund":
            continue
        try:
            output = json.loads(item.text)
        except ValueError:
            return None
        if isinstance(output, dict) and (output.get("error") or output.get("status") == "rejected"
                                         or output.get("refund_status") in ("pending", "failed")):
            return str(output.get("message") or output.get("error") or "")
        return None
    return None


def _claims_payout(text: str, failure: str) -> bool:
    """Whether a sentence says money is coming, beyond quoting why it is not.

    Quoting the failure ("未能提交：该订单当前状态为“已退款”…", "审核意见：该订单已退款")
    or a quoted status word is not a claim.
    """
    for piece in [failure, *split_sentences(failure)]:
        piece = piece.strip().rstrip("。")
        if piece:
            text = text.replace(piece, "")
    text = re.sub(r"“[^”]*”", "", text)
    return bool(_PAYOUT.search(text))


def verify_answer(
    answer: str,
    evidence: list[Evidence],
    user_text: str,
    settings: Settings,
    judge: LLMProvider | None = None,
    judge_model: str | None = None,
) -> Verification:
    by_ref = {e.ref: e for e in evidence}
    evidence_text = "\n".join(e.text for e in evidence)
    number_pool = extract_numbers(evidence_text) + extract_numbers(user_text)
    known_ids = {m.upper() for m in ORDER_ID_RE.findall(evidence_text + " " + user_text)}
    failed_refund = _failed_refund(evidence)

    checks: list[SentenceCheck] = []
    invalid: set[int] = set()
    for raw in split_sentences(answer):
        text, cited = strip_citations(raw)
        if not text:
            continue
        invalid.update(r for r in cited if r not in by_ref)
        claim = _claim(text)
        numbers = extract_numbers(claim)
        factual = not (
            len(claim) < 6
            or claim.endswith(("？", "?"))
            or (not numbers and _claims_nothing(claim))
        )
        # A payout claim counts even when phrased politely ("请放心，钱会到账").
        contradicts = "request_refund" if failed_refund is not None and _claims_payout(text, failed_refund) else None
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
            factual_checks = [c for c in checks if c.factual]
            for check, label in zip(factual_checks, labels):
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


def _judge(provider: LLMProvider, model: str, checks: list[SentenceCheck], evidence: list[Evidence]) -> list[bool] | None:
    factual = [c for c in checks if c.factual]
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
