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

from ..textutil import (NEGATED_PERSON_RE, NEGATED_REFUND_RE, ORDER_ID_RE, REFUND_APPLY_RE, REFUND_RE,
                        estimate_tokens, expand_query, normalize_width, refund_bare_decline, refund_requested,
                        refund_withdraw_requested, withdraw_confirmed,
                        split_sentences, token_set)
from .base import ChatResult, LLMMessage, LLMProvider, StreamEvent, ToolCall, ToolSpec, Usage

_HANDOFF = re.compile(r"人工|真人|投诉|转接")
# Negated requests ("不用转人工") are cut out before matching, the way a real model reads them;
# otherwise the offline model (the demo path and the eval baseline) inflates the handoff rate.
_NEGATED_HANDOFF = NEGATED_PERSON_RE
_REFUND = REFUND_RE
_SHIPMENT = re.compile(r"物流|快递|到哪|发货了吗|配送|运单|什么时候到|送到|where is|track|arrive|deliver|shipping",
                       re.IGNORECASE)
# "退款到哪一步了", "退款进度查一下", "SO… 的退款多久到账": a question about a refund, read-only (N-056).
# An explicit request in the same sentence ("我要退款，多久到账") still files one.
_PROGRESS = re.compile(r"进度|到哪一?步|退款到哪了|怎么样了|处理得?怎么样|到账了?(?:吗|没)|到没到账|退回来了?(?:吗|没)"
                       r"|(?:多久|几天|什么时候|啥时候)(?:能|才能|可以)?到账|status|progress", re.IGNORECASE)
_APPLY = REFUND_APPLY_RE
# "我的快递咋还没到", "我最近买的那个扫地机器人到哪了", "快递到哪了": about one of the customer's
# own orders even without its number, so look their orders up instead of searching policy (N-057).
# "我的银行卡号…能退到这张卡吗" is a policy question: "我的" counts only before an order word.
_OWN = re.compile(r"我的(?:快递|包裹|订单|单|货|东西|退款)|我那|我最近|我买的|我刚买|我上次|我下的")
_MINE = re.compile(_OWN.pattern + r"|单子|到哪了|到哪儿|还没到|咋还没|怎么还没")
_TICKET = re.compile(r"报修|故障|坏了|工单|无法使用|打不开|不工作|失灵|连不上")
# A ticket is for a problem being reported ("坏了", "打不开", "出故障了"), not for a question about how
# problems are handled ("P1故障多久响应？", "怎么报修？"): the knowledge base answers those.
_REPORTED = re.compile(r"坏了|无法使用|打不开|不工作|失灵|连不上|开不了|没反应|出故障|故障了")
_ASKING = re.compile(r"多久|多长时间|怎么|如何|什么|哪些|是否|能不能|可以吗|吗[？?]?$|[？?]$")
_URGENT = re.compile(r"紧急|严重|马上|立刻")
_AMOUNT = re.compile(r"(\d+(?:\.\d+)?)\s*(?:元|块)")

HALLUCINATION = "另外，所有订单均支持 90 天无理由退货，退款 2 小时内到账。"
REFUSAL = (
    "抱歉，知识库中没有找到与该问题直接相关的依据。"
    "为避免给出不准确的信息，建议您转人工客服确认。"
)
# The search itself failed or ran partly (N-078): say so instead of "the knowledge base has nothing".
SEARCH_DOWN = "抱歉，知识库检索暂时出了故障，这次没能查到相关规定，并不代表没有。请稍后再试，或回复“转人工”。"


# "SO… 我要退货退款，另外发票怎么开？" carries two requests. The clause after a connector is
# answered on its own instead of being silently dropped.
_EXTRA = re.compile(r"[，,。；;！!？?\s]+(?:另外|还有|顺便问一下|顺便|再问一下|以及)[，,\s]*")
MULTI_REFUND_TEXT = "一次只能为一个订单申请退款，请告诉我要退哪一个订单。"
REJECTED_NEXT_STEP = "如有补充凭证，可以重新提交退款申请，也可以回复“转人工”由客服进一步处理。"


def split_intents(text: str) -> tuple[str, str | None]:
    """(main request, an extra question or None)."""
    match = _EXTRA.search(text)
    if not match:
        return text, None
    extra = text[match.end():].strip()
    if len(extra) < 2 or ORDER_ID_RE.search(extra):
        return text, None
    return text[:match.start()].strip(), extra


def _mentions_refund(text: str) -> bool:
    """A refund word, or a bare demand without one ("SO… 退了吧", N-126)."""
    return bool(_REFUND.search(text)) or refund_requested(text)


def asks_refund_status(text: str) -> bool:
    return bool(_mentions_refund(text) or "到账" in text) and bool(_PROGRESS.search(text)) and not _APPLY.search(text)


_ASKING_WORDS = re.compile(r"如果|要是|假如|假设|万一|的话|只是问问|就问问|问问而已|随便问问|先问问|咨询一下|想?问一?下"
                           r"|just\s+asking|just\s+wondering|what\s+if|(?<![A-Za-z])if(?![A-Za-z])", re.IGNORECASE)
_FILLER = re.compile(r"^[，,\s]*(?:我|先)?|[啊哈呀吧呢嘛]+(?=[\s，,！!。]|$)|我?就?问问|\bI\b"
                     r"|(?<!需)(?:我?(?:申请|想要?|要)|帮我|给我)(?=退)")  # "如果我申请退款，…" looks up the refund, not 申请


def refund_question(text: str) -> str:
    """What to look up for a question about refunds: the words without the order number or the "别退" part."""
    question = _ASKING_WORDS.sub(" ", ORDER_ID_RE.sub(" ", NEGATED_REFUND_RE.sub(" ", text)))
    question = _FILLER.sub(" ", question)
    return re.sub(r"\s+", " ", question).strip(" ，,。！!？?:：") or "退款 到账 工作日"


def about_my_order(text: str) -> bool:
    """A refund or delivery question about the customer's own order, asked without an order number."""
    if ORDER_ID_RE.search(text):
        return False
    if refund_withdraw_requested(text):  # "取消我的退款申请": which one? (N-127)
        return True
    if asks_refund_status(text):
        return bool(_OWN.search(text) or "单子" in text)
    if _mentions_refund(text):
        # "我要退款", "我买的那个扫地机器人退货"; not "快递还没到能退款吗" or "我买的东西能退货吗", which ask about policy.
        return bool(_APPLY.search(text) or (_OWN.search(text) and not _ASKING.search(text.strip())))
    return bool(_SHIPMENT.search(text) and _MINE.search(text))


def _names_item(text: str, name: str) -> bool:
    """"扫地机器人" names "扫地机器人 X1": any two Chinese characters of the item name in a row."""
    han = re.sub(r"[^\u4e00-\u9fff]", "", name)
    return any(han[i:i + 2] in text for i in range(len(han) - 1))


def pick_my_order(text: str, listing: dict[str, Any]) -> str | None:
    """The one order a question without a number is about, or None when it has to be asked.
    Only read-only questions are followed up on a guess; a refund is never filed on one."""
    orders = listing.get("orders") or []
    named = [o for o in orders if any(_names_item(text, n) for n in o.get("items") or [])]
    if named:
        candidates = named
    elif asks_refund_status(text):
        candidates = [o for o in orders if o.get("status") == "refunding"]
    elif _SHIPMENT.search(text) and not _mentions_refund(text):
        candidates = [o for o in orders if o.get("status") == "shipped"]
    else:
        candidates = []
    return candidates[0]["order_id"] if len(candidates) == 1 else None


def wants_human(text: str) -> bool:
    return bool(_HANDOFF.search(_NEGATED_HANDOFF.sub("", text)))


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
        if options.get("task") == "kb_questions":
            content = self._kb_questions(messages[-1].content)
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
        main, extra = split_intents(text)
        call = self._main_call(main, called, results, available)
        if call is not None:
            return call
        searched = {r.get("query") for r in results.get("search_knowledge") or []}
        if extra and "search_knowledge" in available and extra not in searched:
            return "search_knowledge", {"query": extra}
        return None

    def _main_call(self, text: str, called: list[str], results: dict, available: set[str]):
        def can(name: str) -> bool:
            return name in available and name not in called

        order_ids = list(dict.fromkeys(m.upper() for m in ORDER_ID_RE.findall(text)))
        if wants_human(text) and can("handoff_to_human"):
            return "handoff_to_human", {"reason": text[:120]}

        if len(order_ids) > 1:  # look every order up; track each if asked; never guess which to refund
            orders = results.get("get_order") or []
            if len(orders) < len(order_ids) and "get_order" in available:
                return "get_order", {"order_id": order_ids[len(orders)]}
            found = [o["order_id"] for o in orders if not o.get("error")]
            tracked = results.get("track_shipment") or []
            if (_SHIPMENT.search(text) and not _mentions_refund(text) and "track_shipment" in available
                    and len(tracked) < len(found)):
                return "track_shipment", {"order_id": found[len(tracked)]}
            return None

        if not order_ids and withdraw_confirmed(text):  # "确认撤回": the tool takes the order from its own offer (N-145)
            return ("withdraw_refund_request", {}) if can("withdraw_refund_request") else None

        if about_my_order(text) and "list_my_orders" in available:
            if can("list_my_orders"):
                return "list_my_orders", {}
            listing = (results.get("list_my_orders") or [{}])[-1]
            # Read-only questions may follow a single match; money never moves on an order we picked.
            picked = (None if (refund_requested(text) and not asks_refund_status(text)) or refund_withdraw_requested(text)
                      else pick_my_order(text, listing))
            if picked is None:
                return None  # list the orders and ask which one
            order_ids = [picked]

        if order_ids:
            order_id = order_ids[0]
            if can("get_order"):
                return "get_order", {"order_id": order_id}
            order = (results.get("get_order") or [{}])[-1]
            if order.get("error"):
                return None
            # "我刚才的退款申请能撤回吗", or "不想退了" while one waits for review: take it back (N-127).
            if (refund_withdraw_requested(text) or withdraw_confirmed(text)
                    or (order.get("refund_awaiting_approval") and refund_bare_decline(text))):
                return ("withdraw_refund_request", {"order_id": order_id}) if can("withdraw_refund_request") else None
            if asks_refund_status(text):  # answer from the order's refunds; never file one
                live = order.get("refund_awaiting_approval") or any(
                    r.get("status") in ("pending", "processing") for r in order.get("refunds") or [])
                if live and can("search_knowledge"):
                    return "search_knowledge", {"query": "退款 到账 工作日"}
                return None
            if _mentions_refund(text):
                if not order.get("refundable", False):
                    return None
                # "先别退款，如果退款的话钱退到哪" asks about a refund: answer it, never file one (N-091).
                if not refund_requested(text):
                    if can("search_knowledge"):
                        return "search_knowledge", {"query": refund_question(text)}
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

        if _TICKET.search(text) and (_REPORTED.search(text) or not _ASKING.search(text.strip())):
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
        text, extra = split_intents(text)
        searches = results.get("search_knowledge") or []
        extra_search = next((r for r in searches if extra and r.get("query") == extra), None)
        results = {**results, "search_knowledge": [r for r in searches if r is not extra_search]}
        parts: list[str] = []

        def last(name: str) -> dict[str, Any] | None:
            return results[name][-1] if results.get(name) else None

        handoff = last("handoff_to_human")
        if handoff:
            parts.append(handoff.get("message") or handoff.get("error", ""))

        listing = last("list_my_orders")
        if listing and not results.get("get_order"):
            parts.append(self._ask_which(text, listing))

        withdrawal = last("withdraw_refund_request")
        if withdrawal:  # its message says what happened; the order's own summary would only repeat it
            parts.append(withdrawal.get("message") or f"未能撤回退款申请：{withdrawal.get('error', '')}。")
        orders = [] if withdrawal else results.get("get_order") or []
        refund = last("request_refund")
        progress = len(orders) == 1 and not orders[0].get("error") and asks_refund_status(text)
        if progress:
            parts.append(self._refund_progress(orders[0]))
            orders = []
        shipments = results.get("track_shipment") or []
        tracked = {s.get("order_id") for s in shipments if not s.get("error")}
        for order in orders:
            if order.get("error"):  # a wrong number is worth checking; an outage is not
                check = "请核对订单号后再试。" if "订单不存在" in order["error"] else ""
                parts.append(f"未能查询到该订单：{order['error']}。{check}")
            elif not refund and order.get("order_id") not in tracked:
                items = "、".join(f"{i['name']}×{i['qty']}" for i in order.get("items", []))
                parts.append(
                    f"订单 {order['order_id']} 当前状态为“{order['status_text']}”，"
                    f"订单金额 ¥{order['amount']:.2f}，商品：{items}。"
                )
                if len(orders) == 1 and _mentions_refund(text) and not order.get("refundable", False):
                    parts.append(f"该订单暂不能发起退款：{order.get('refund_note', '')}。")
        if len(orders) > 1 and _mentions_refund(text):
            parts.append(MULTI_REFUND_TEXT)

        for shipment in shipments:
            if shipment.get("error"):
                parts.append(f"物流查询失败：{shipment['error']}。")
            elif shipment.get("message"):
                parts.append(shipment["message"])

        # Only a refund that went through gets the payout timing; a rejected or failed one
        # gets its reason and a next step, never "arrives in 1-3 working days".
        refund_failed = bool(refund) and (bool(refund.get("error")) or refund.get("status") == "rejected"
                                          or refund.get("refund_status") == "pending")  # not paid, or not yet known
        if refund:
            if refund.get("error"):
                parts.append(f"退款申请未能提交：{refund['error']}。")
            elif refund.get("status") == "rejected":
                parts.append(refund.get("message", ""))
                parts.append(REJECTED_NEXT_STEP)
            else:
                parts.append(refund.get("message", ""))

        ticket = last("create_ticket")
        knowledge = last("search_knowledge")
        # A question about refunds on one of the customer's orders, answered without filing one (N-091).
        inquiry = (len(orders) == 1 and not refund and not orders[0].get("error") and orders[0].get("refundable")
                   and bool(_mentions_refund(text)) and not refund_requested(text))
        if knowledge:
            query = ("退款 到账 工作日" if (refund and not refund_failed) or progress
                     else expand_query(refund_question(text) if inquiry else text))
            if not refund_failed:
                limit = 1 if (refund or ticket or progress) else 3
                sentences = self._extract(query, knowledge.get("results") or [], limit)
                if sentences:
                    parts.extend(sentences)
                elif not parts and not ticket:
                    down = knowledge.get("error") or knowledge.get("degraded")
                    parts.append(SEARCH_DOWN if down else REFUSAL)
        if inquiry:
            oid = orders[0]["order_id"]
            # Only a customer who said "别退 / 不退" is told nothing will be filed; otherwise ask (N-126): an angry
            # customer misread as asking is offered to apply, not told "不会为您发起退款".
            if NEGATED_REFUND_RE.search(normalize_width(text)):
                parts.append(f"如需退款，可以直接回复“{oid} 申请退款”，这次只解答问题、不会为您发起退款。")
            else:
                parts.append(f"如需现在申请退款，回复“{oid} 申请退款”即可，我马上为您提交。")
        if ticket:
            parts.append(ticket.get("message") or f"工单创建失败：{ticket.get('error', '')}。")

        if extra_search is not None:
            sentences = self._extract(expand_query(extra), extra_search.get("results") or [], 2)
            parts.append(f"另外，{''.join(sentences)}" if sentences else f"关于“{extra}”，知识库中没有找到相关依据。")

        if not parts:
            parts.append(REFUSAL)
        return "".join(p for p in parts if p)

    @staticmethod
    def _ask_which(text: str, listing: dict[str, Any]) -> str:
        if listing.get("error"):
            return f"暂时查不到您的订单：{listing['error']}。也可以直接告诉我订单号。"
        orders = listing.get("orders") or []
        if not orders:
            return "没有查到您账号下的订单。如果是用其他账号下的单，请告诉我订单号。"
        lines = "；".join(f"{o['order_id']}（{'、'.join(o.get('items') or [])}，{o['status_text']}，¥{o['amount']:.2f}）"
                         for o in orders)
        if refund_withdraw_requested(text):
            return (f"您账号下最近的订单有：{lines}。请问要撤回的是哪一单的退款申请？"
                    f"请回复“订单号 撤回退款申请”，例如“{orders[0]['order_id']} 撤回退款申请”。")
        if _mentions_refund(text) and not asks_refund_status(text):
            return (f"您账号下最近的订单有：{lines}。请问要申请退款的是哪一单？"
                    f"请回复“订单号 申请退款”，例如“{orders[0]['order_id']} 申请退款”。")
        return f"您账号下最近的订单有：{lines}。请问要查询的是哪一单？回复订单号即可。"

    @staticmethod
    def _refund_progress(order: dict[str, Any]) -> str:
        oid = order["order_id"]
        lines: list[str] = []
        waiting = order.get("refund_awaiting_approval")
        if waiting:
            lines.append(f"订单 {oid} 有一笔 ¥{waiting['amount']:.2f} 的退款申请（{waiting['filed_on']} 提交）正在等待人工审核，"
                         "审核通过后会按原支付方式退回。")
        for r in (order.get("refunds") or [])[:3]:
            lines.append(f"订单 {oid} 的退款单 #{r['refund_id']}（¥{r['amount']:.2f}，{r['filed_on']} 提交）{r['status_text']}。")
        if not lines:
            lines.append(f"订单 {oid} 当前状态为“{order['status_text']}”，没有查到在线提交的退款记录。")
            if order.get("refundable"):
                lines.append(f"如需退款，可以直接回复“{oid} 申请退款”。")
        return "".join(lines)

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
    def _kb_questions(prompt: str) -> str:
        """Three questions from the section title: enough to exercise the review flow offline. Real
        colloquial wordings need a real model (the generator runs on GitHub Actions)."""
        match = re.search(r"^标题：(.*)$", prompt, re.M)
        topic = (match.group(1) if match else "").split(" > ")[-1].strip() or "这个问题"
        return json.dumps({"questions": [f"{topic}是怎么规定的？", f"想问一下{topic}", f"{topic}有什么要求？"]},
                          ensure_ascii=False)

    @staticmethod
    def _kb_patch(prompt: str) -> str:
        match = re.search(r"\{.*\}", prompt, re.S)
        data = _loads(match.group(0)) if match else {}
        question = str(data.get("question", "")).strip().rstrip("？?")
        correction = str(data.get("correction", "")).strip()
        return f"## {question}\n\n{correction}\n"
