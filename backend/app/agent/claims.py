"""Claim or not: which sentences of an answer the tiered verifier checks.

An answer is mostly framing: greetings, offers ("我帮您查一下"), requests ("请提供订单号"), lead-ins
("可以这样处理："), saying nothing was found, closings. None of it can be wrong about the policy or the
order, and checking it against the evidence only produces false blocks: on the recorded real-model
answers, 97 of the 290 sentences the lexical verifier failed on the dev split were sentences like these
(docs/EVALUATION.md). A sentence with a number is always a claim: numbers are checked by rule, whatever
a classifier says.

Two classifiers share one interface, ``claim_probabilities(sentences) -> list[float]``:
- ``RuleClaims``: the patterns below, tuned on the dev split of the recorded answers only;
- ``ModelClaims``: a fine-tuned Chinese encoder exported to ONNX (trained and measured on GitHub Actions,
  .github/workflows/claim-model.yml), used when LOOPDESK_CLAIM_CLASSIFIER=model and
  LOOPDESK_CLAIM_MODEL_DIR point at it. It needs the optional packages in requirements-claims.txt.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from functools import lru_cache
from pathlib import Path

from ..textutil import extract_numbers, normalize_width
from .outcome import REFUSAL_RE

logger = logging.getLogger(__name__)

_YOU = r"[您你]"
# Courtesy opening a sentence. What follows it is checked like any sentence: "您好，本店支持终身保修"
# used to pass unchecked, because the whole sentence counted as a greeting.
COURTESY = re.compile(r"^(您好|你好|抱歉|很抱歉|非常抱歉|不好意思|感谢您?的?(耐心|关注|咨询|反馈|支持)|另外|此外|同时|好的|明白|收到)"
                      r"[，,！!、]?\s*")
# A list marker opening a sentence ("1. ", "2、", "（3）", "- ") is not an amount.
LIST_NUMBER = re.compile(r"^(\d{1,2}[.、．)）]|[（(]\d{1,2}[)）]|[-*•·]\s)\s*")
_SENTENCE_NON_CLAIM = re.compile(
    # (a condition opening a sentence, "如需加急，专票可当天寄出", leaves the decision to its clauses)
    r"^(请|建议|还有什么|欢迎|感谢|为避免|麻烦|稍等|方便的话)"
    # a lead-in: "收到破损商品可以这样处理：", "已为您查到两个订单：", "物流信息如下："
    r"|^[^，,。]{0,24}(如下|以下|这样|几点|几种|两种情况|两件事|几件事|两个订单|几个订单)[^，,。]{0,6}[:：]$"
    r"|^\[[A-Za-z_]+\]$"  # a tool name the model echoed
    r"|^(问题|您的问题|你的问题|故障|现象|诉求)[:：]"  # restating what the customer reported
)
# What says there is no basis or that the agent cannot answer, beyond outcome.REFUSAL_RE.
_NO_BASIS = re.compile(r"(未|没)(能)?(在知识库|在资料)?[中里]?(查到|找到|检索到).{0,30}(依据|规定|政策|规则|信息|说明)"
                       r"|暂未(查到|找到|检索到)|(知识库|资料)[中里]?(没有|未|暂无).{0,30}依据|无法确认|不是系统指令"
                       r"|^(也|并|但)?(未|没有)(提供|提到|涉及|包含|说明).{0,10}(说明|信息|依据|规定)$")
# Clauses that claim nothing: a condition or purpose, a request to the customer, an offer of something
# the agent can still do (not something it says it did: "帮您创建了工单" is a claim), a closing, the agent
# declining, an uncertainty, a deferral to someone who will decide.
_CLAIMLESS_CLAUSE = re.compile(
    r"^(如|若|如果|假如|要是|为了|为便于|为方便|以便|便于)"
    r"|^(请|麻烦|烦请|方便|劳烦|还请|可提供|可以提供|建议|不要|请勿|切勿|勿)"
    rf"|^(我们?|这边)?(也)?(可以|能|来|会|很乐意|乐意)?(先|再|马上|立即|这就|直接)?(帮|给){_YOU}(?!.*(?<!是否)[了已])"
    rf"|^(我们?|这边|我也)?(也)?(可以|能|来|会|很乐意|乐意)?(先|再|马上|立即|这就|直接)?为{_YOU}(?!.*(?<!是否)[了已])"
    rf"|^(并|再|同时|也)?(协助|帮{_YOU}|为{_YOU})(?!.*(?<!是否)[了已])"
    r"|^(也|还|或|或者|另外也)?(可以|可|能)(直接|再|随时)?(告诉我|联系|回复|提供|发给我|转接|转人工|转至人工|找人工|联系人工)"
    r"|(可以|可|需要|建议|可能需要)(您|你)?(再)?(转接?|联系)人工(客服)?(协助|处理|核实|确认|跟进|核查)?(确认|处理|跟进)?$"
    r"|^(直接)?(提供|告诉我|回复|发送)(“[^”]*”|[^，。]){0,12}(即可|就行|就可以)?$"
    r"|^(这次|本次)?(只|仅)(解答|回答)(问题|疑问)|不会为[您你](发起|提交|申请)"
    r"|^(有其他问题|如有其他|还有什么|欢迎|期待|祝您|祝你)"
    r"|^(因此|所以)?(我们?|这类\S{0,4}|该\S{0,4}|此类\S{0,4})?我?(暂时)?(不能|无法|不会|不可以|没法)(为[您你](再次)?)?"
    r"(执行|处理|操作|提供|透露|回答|确认|判断|发起|办理)"
    r"|^(因此|所以)?(目前|暂时|现在)?(还|暂)?(无法|没法|不能)(确定|确认|判断|预估|保证)"
    r"|^具体.{0,16}(以|需|要由|由).{0,12}(为准|确认|判断|决定)$|以.{0,12}为准$"
    r"|没有更(精确|具体|详细)的"
    r"|^(我们|这边)?不(需要|会)(您|你)提供"
    # what the agent itself is or cannot do: "我是Leon优选的客服助手", "我这边没有修改退款额度的权限"
    r"|^(不过)?(作为[^，。]{0,12})?(我|我这边|我这里|我这儿)(是|只是|只|只能|仅|仅能|没有|无法|不能|不会|也不会|无从|的权限)"
    r"|^(此项|该项|这类|这项)?[^，。]{0,10}(请|需)(通过|联系|走)[^，。]{0,16}(处理|办理|操作|咨询)$"
    r"|(内部信息|内部配置|系统提示词)[^，。]{0,10}(无法|不能|不会|不便)?(提供|披露|对外|输出)?"
    # Saying the search itself failed (N-078); per clause, so a claim next to it is still checked.
    r"|^(知识库)?检索(服务)?暂时(出了?)?故障[。！!]?$|^这次没能查到相关规定[。！!]?$|^并不代表没有[。！!]?$"
)
CLAUSE_SPLIT = re.compile(r"[，,；;：:—]+")


def claim_text(text: str) -> str:
    """The sentence without an opening courtesy or list marker."""
    return LIST_NUMBER.sub("", COURTESY.sub("", normalize_width(text).strip()), count=1).strip()


def rule_is_claim(text: str) -> bool:
    claim = claim_text(text)
    if extract_numbers(claim):
        return True  # numbers are always checked
    if len(claim) < 6 or claim.endswith(("？", "?")):
        return False
    if _SENTENCE_NON_CLAIM.search(claim) or REFUSAL_RE.search(claim) or _NO_BASIS.search(claim):
        return False
    clauses = [c.strip() for c in CLAUSE_SPLIT.split(claim) if c.strip()]
    return not (clauses and all(_CLAIMLESS_CLAUSE.search(c) for c in clauses))


def claimless_clause(clause: str) -> bool:
    return bool(_CLAIMLESS_CLAUSE.search(clause.strip())) or bool(REFUSAL_RE.search(clause) or _NO_BASIS.search(clause))


class RuleClaims:
    name = "rules"
    threshold = 0.5

    def claim_probabilities(self, sentences: list[str]) -> list[float]:
        return [1.0 if rule_is_claim(s) else 0.0 for s in sentences]


class ModelClaims:
    """The fine-tuned encoder (ONNX). Loaded once, on first use; any failure to load or run falls back to
    the rules for that call and is logged, so a missing model never takes the verifier down."""

    name = "model"

    def __init__(self, model_dir: str, threshold: float = 0.5, max_length: int = 96):
        self.model_dir, self.threshold, self.max_length = Path(model_dir), threshold, max_length
        config = self.model_dir / "config.json"  # written by app.ml.claim_model: the threshold chosen on validation
        if config.exists():
            import json

            values = json.loads(config.read_text(encoding="utf-8"))
            self.threshold = float(values.get("threshold", threshold))
            self.max_length = int(values.get("max_length", max_length))
        self._session = self._tokenizer = None
        self._lock = threading.Lock()
        self.fallbacks = 0

    def _load(self):
        with self._lock:
            if self._session is None:
                import onnxruntime  # optional: requirements-claims.txt
                from tokenizers import Tokenizer

                self._tokenizer = Tokenizer.from_file(str(self.model_dir / "tokenizer.json"))
                self._tokenizer.enable_truncation(self.max_length)
                self._tokenizer.enable_padding()
                self._session = onnxruntime.InferenceSession(str(self.model_dir / "model.onnx"),
                                                             providers=["CPUExecutionProvider"])
        return self._session, self._tokenizer

    def claim_probabilities(self, sentences: list[str]) -> list[float]:
        if not sentences:
            return []
        try:
            import numpy as np

            session, tokenizer = self._load()
            encoded = tokenizer.encode_batch([claim_text(s) for s in sentences])
            feed = {"input_ids": np.array([e.ids for e in encoded], dtype=np.int64),
                    "attention_mask": np.array([e.attention_mask for e in encoded], dtype=np.int64)}
            names = {i.name for i in session.get_inputs()}
            if "token_type_ids" in names:
                feed["token_type_ids"] = np.array([e.type_ids for e in encoded], dtype=np.int64)
            logits = session.run(None, {k: v for k, v in feed.items() if k in names})[0]
            exp = np.exp(logits - logits.max(axis=1, keepdims=True))
            probs = (exp / exp.sum(axis=1, keepdims=True))[:, 1]
        except Exception:  # noqa: BLE001 - a broken model must not break the answer check
            self.fallbacks += 1
            logger.exception("claim model failed; the rules decide this answer")
            return RuleClaims().claim_probabilities(sentences)
        # A number is a claim whatever the model thinks: numbers are always rule-checked.
        return [1.0 if extract_numbers(claim_text(s)) else float(p) for s, p in zip(sentences, probs)]


@lru_cache(maxsize=4)
def _model(model_dir: str, threshold: float) -> ModelClaims:
    return ModelClaims(model_dir, threshold)


def classifier(settings) -> RuleClaims | ModelClaims:
    """LOOPDESK_CLAIM_CLASSIFIER=model with LOOPDESK_CLAIM_MODEL_DIR set uses the encoder; anything else the rules.
    The model's own config.json threshold wins over LOOPDESK_CLAIM_MODEL_THRESHOLD."""
    if getattr(settings, "claim_classifier", "rules") == "model" and getattr(settings, "claim_model_dir", None):
        return _model(settings.claim_model_dir, settings.claim_model_threshold)
    return RuleClaims()


def timed(classify, sentences: list[str]) -> tuple[list[float], float]:
    started = time.perf_counter()
    probs = classify.claim_probabilities(sentences)
    return probs, (time.perf_counter() - started) * 1000
