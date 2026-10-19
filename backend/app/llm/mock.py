"""Deterministic offline model.

It implements the same tool-calling contract as a real LLM, so the whole platform
(agent loop, approvals, verifier, evals, UI) runs and is testable without an API key.
Behaviour is rule-based and reproducible: the same input always yields the same
trajectory, which is exactly what CI regression tests need.

It is NOT a stand-in for model quality. Numbers measured with it validate the pipeline,
not answer quality; re-run the eval suite with a real model for that.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Iterator
from typing import Any

from ..textutil import ORDER_ID_RE, estimate_tokens, expand_query, split_sentences, token_set
from .base import ChatResult, LLMMessage, LLMProvider, StreamEvent, ToolCall, ToolSpec, Usage

_HANDOFF = re.compile(r"人工|真人|投诉|转接")
_REFUND = re.compile(r"退款|退钱|退货|退掉")
_SHIPMENT = re.compile(r"物流|快递|到哪|发货了吗|配送|运单|什么时候到")
_TICKET = re.compile(r"报修|故障|坏了|工单|无法使用|打不开|不工作|失灵|连不上")
_URGENT = re.compile(r"紧急|严重|马上|立刻")
_AMOUNT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|块)")

HALLUCINATION = "另外，所有订单均支持 90 天无理由退货，退款 2 小时内到账。"
REFUSAL = (
    "抱歉，知识库中没有找到与该问题直接相关的依据。"
    "为避免给出不准确的信息，建议您转人工客服确认。"
)


def _loads(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return {"raw": text}
    return value if isinstance(value, dict) else {"value": value}


def _clean(sentence: str) -> str:
    return sentence.lstrip("-*• ").strip()


class MockLLM(LLMProvider):
    name = "mock"

    def __init__(self, stream_delay: float = 0.0):
        self.stream_delay = stream_delay

    # ------------------------------------------------------------------ public API
    def chat(self, messages: list[LLMMessage], tools: list[ToolSpec], model: str,
             options: dict[str, Any] | None = None) -> ChatResult:
        options = options or {}
        prompt_tokens = sum(estimate_tokens(m.content) for m in messages)
        if options.get("task") == "kb_patch":
            content = self._kb_patch(messages[-1].content)
            return ChatResult(content, [], Usage(prompt_tokens, estimate_tokens(content), True), model)

        available = {t.name for t in tools}
        user_idx = max(i for i, m in enumerate(messages) if m.role == "user")
        user_text = messages[user_idx].content
        turn = messages[user_idx + 1 :]
        called = [c.name for m in turn if m.role == "assistant" for c in m.tool_calls]
        results: dict[str, list[dict[str, Any]]] = {}
        for m in turn:
            if m.role == "tool" and m.name:
                results.setdefault(m.name, []).append(_loads(m.content))

        call = self._next_call(user_text, called, results, available)
        if call is not None:
            name, args = call
            tool_call = ToolCall(id=f"mock_{len(called) + 1}_{name}", name=name, arguments=args)
            completion = estimate_tokens(json.dumps(args, ensure_ascii=False))
            return ChatResult("", [tool_call], Usage(prompt_tokens, completion, True), model, "tool_calls")

        content = self._compose(user_text, results)
        if options.get("inject_hallucination"):
            content += HALLUCINATION
        return ChatResult(content, [], Usage(prompt_tokens, estimate_tokens(content), True), model)

    def stream(self, messages, tools, model, options=None) -> Iterator[StreamEvent]:
        result = self.chat(messages, tools, model, options)
        text = result.content
        delay = 0.0 if (options or {}).get("dry_run") else self.stream_delay
        for start in range(0, len(text), 4):
            if delay:
                time.sleep(delay)
            yield StreamEvent(type="delta", text=text[start : start + 4])
        yield StreamEvent(type="done", result=result)

    # ------------------------------------------------------------------ planning
    def _next_call(self, text: str, called: list[str], results: dict, available: set[str]):
        def can(name: str) -> bool:
            return name in available and name not in called

        order_match = ORDER_ID_RE.search(text)
        if _HANDOFF.search(text) and can("handoff_to_human"):
            return "handoff_to_human", {"reason": text[:120]}

        if order_match:
            order_id = order_match.group(0).upper()
            if can("get_order"):
                return "get_order", {"order_id": order_id}
            order = (results.get("get_order") or [{}])[-1]
            if order.get("error"):
                return None
            if _REFUND.search(text):
                if not order.get("refundable", False):
                    return None
                if can("search_knowledge"):
                    return "search_knowledge", {"query": "退款 到账 工作日"}
                if can("request_refund"):
                    amount_match = _AMOUNT.search(text)  # a gullible model takes the user's number
                    amount = float(amount_match.group(1)) if amount_match else float(order.get("amount", 0))
                    return "request_refund", {"order_id": order_id, "amount": amount, "reason": text[:120]}
                return None
            if _SHIPMENT.search(text) and can("track_shipment"):
                return "track_shipment", {"order_id": order_id}
            return None

        if _TICKET.search(text):
            if can("search_knowledge"):
                return "search_knowledge", {"query": text}
            if can("create_ticket"):
                priority = "high" if _URGENT.search(text) else "normal"
                return "create_ticket", {"title": text[:30], "description": text, "priority": priority}
            return None

        if not called and can("search_knowledge"):
            return "search_knowledge", {"query": text}
        return None

    # ------------------------------------------------------------------ answering
    def _compose(self, text: str, results: dict[str, list[dict[str, Any]]]) -> str:
        parts: list[str] = []

        def last(name: str) -> dict[str, Any] | None:
            return results[name][-1] if results.get(name) else None

        handoff = last("handoff_to_human")
        if handoff:
            parts.append(handoff.get("message") or handoff.get("error", ""))

        order = last("get_order")
        refund = last("request_refund")
        shipment = last("track_shipment")
        if order:
            if order.get("error"):
                parts.append(f"未能查询到该订单：{order['error']}。请核对订单号后再试。")
            elif not refund and not (shipment and not shipment.get("error")):
                items = "、".join(f"{i['name']}×{i['qty']}" for i in order.get("items", []))
                parts.append(
                    f"订单 {order['order_id']} 当前状态为“{order['status_text']}”，"
                    f"订单金额 ¥{order['amount']:.2f}，商品：{items}。"
                )
                if _REFUND.search(text) and not order.get("refundable", False):
                    parts.append(f"该订单暂不能发起退款：{order.get('refund_note', '')}。")

        if shipment:
            if shipment.get("error"):
                parts.append(f"物流查询失败：{shipment['error']}。")
            elif shipment.get("message"):
                parts.append(shipment["message"])

        if refund:
            if refund.get("error"):
                parts.append(f"退款申请未能提交：{refund['error']}。")
            else:
                parts.append(refund.get("message", ""))

        ticket = last("create_ticket")
        knowledge = last("search_knowledge")
        if knowledge:
            query = "退款 到账 工作日" if refund and not refund.get("error") else expand_query(text)
            if not (refund and refund.get("error")):
                limit = 1 if (refund or ticket) else 3
                sentences = self._extract(query, knowledge.get("results") or [], limit)
                if sentences:
                    parts.extend(sentences)
                elif not parts and not ticket:
                    parts.append(REFUSAL)
        if ticket:
            parts.append(ticket.get("message") or f"工单创建失败：{ticket.get('error', '')}。")

        if not parts:
            parts.append(REFUSAL)
        return "".join(p for p in parts if p)

    @staticmethod
    def _extract(query: str, hits: list[dict[str, Any]], limit: int) -> list[str]:
        """Pick the sentences that best cover the query, only from strongly relevant hits."""
        query_tokens = token_set(query)
        if not hits or not query_tokens:
            return []
        top = max(float(h.get("relevance", 0)) for h in hits)
        scored: list[tuple[float, int, int, str]] = []
        for rank, hit in enumerate(hits):
            if float(hit.get("relevance", 0)) < 0.6 * top:
                continue
            heading_tokens = token_set(hit.get("heading", ""))
            for pos, raw in enumerate(split_sentences(hit.get("text", ""))):
                sentence = _clean(raw)
                if len(sentence) < 6:
                    continue
                sentence_tokens = token_set(sentence)
                direct = len(query_tokens & sentence_tokens) / len(query_tokens)
                context = len(query_tokens & (heading_tokens - sentence_tokens)) / len(query_tokens)
                score = direct + 0.3 * context
                if score >= 0.34:
                    scored.append((score - rank * 0.05, hit["ref"], pos, sentence))
        scored.sort(key=lambda s: -s[0])
        picked: list[tuple[int, int, str]] = []
        seen: set[str] = set()
        for _, ref, pos, sentence in scored:
            if sentence in seen:
                continue
            seen.add(sentence)
            picked.append((ref, pos, sentence))
            if len(picked) == limit:
                break
        picked.sort()
        return [f"{sentence.rstrip('。；;')}[{ref}]。" for ref, _, sentence in picked]

    @staticmethod
    def _kb_patch(prompt: str) -> str:
        match = re.search(r"\{.*\}", prompt, re.S)
        data = _loads(match.group(0)) if match else {}
        question = str(data.get("question", "")).strip().rstrip("？?")
        correction = str(data.get("correction", "")).strip()
        return f"## {question}\n\n{correction}\n"
