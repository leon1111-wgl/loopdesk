"""Agent runtime: the tool-calling loop, human-in-the-loop pause/resume, and the
evidence check on the final answer.

    user msg ─► guardrails ─► router ─► ┌─────────── loop (≤ max steps) ───────────┐
                                        │ LLM ─► tool calls? ─yes─► policy check ──┤
                                        │   │                     ├─ needs human ─►│ pause: persist state,
                                        │   │                     │                │ create approval
                                        │   │                     └─ run tool ─────┘
                                        │   └─no─► final answer ─► verifier ─► save message
                                        └──────────────────────────────────────────┘

The loop is a generator of events, so the same code serves SSE streaming, the
non-streaming API, the Feishu channel and the eval runner.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..channels.wecom import notify_approval
from ..config import Settings
from ..guardrails.injection import detect_injection
from ..guardrails.pii import PIIReport, mask_pii
from ..llm.base import Embedder, LLMError, LLMMessage, LLMProvider, ToolCall
from ..llm.router import ModelRouter
from ..models import Approval, Conversation, Message, Run, Tenant, User
from .prompts import build_system_prompt
from .tools import Evidence, ToolContext, ToolError, ToolRegistry
from .tracing import Tracer
from .verifier import verify_answer

STRICT_FALLBACK = "抱歉，我暂时无法给出有可靠依据的答复。为避免误导，建议您转人工客服确认。"
MAX_STEPS_TEXT = "抱歉，这个问题需要的处理步骤较多，建议您转人工客服继续处理。"
MODEL_ERROR_TEXT = "抱歉，智能客服暂时繁忙，请稍后再试或转人工客服。"
REFUSAL_TEXT = "抱歉，这个问题我无法处理，建议您转人工客服。"
TOOL_LABEL = {"request_refund": "退款"}


@dataclass
class RunOptions:
    dry_run: bool = False
    inject_hallucination: bool = False


@dataclass
class ToolOutcome:
    kind: str  # result | approval
    output: dict[str, Any] = field(default_factory=dict)
    is_error: bool = False
    reason: str = ""


class AgentRuntime:
    def __init__(self, db: Session, settings: Settings, provider: LLMProvider, embedder: Embedder,
                 registry: ToolRegistry):
        self.db = db
        self.settings = settings
        self.provider = provider
        self.embedder = embedder
        self.registry = registry
        self.router = ModelRouter(settings)

    # ================================================================ entry points
    def start(self, *, tenant: Tenant, user: User, text: str, conversation: Conversation | None = None,
              options: RunOptions | None = None) -> Iterator[dict[str, Any]]:
        options = options or RunOptions()
        pii = mask_pii(text) if self.settings.pii_masking else PIIReport(text, [])
        history = self._history(conversation)
        if conversation is not None and not options.dry_run:
            self.db.add(Message(tenant_id=tenant.id, conversation_id=conversation.id, role="user", content=pii.masked))
            if conversation.title == "新对话":
                conversation.title = pii.masked[:30]
            conversation.updated_at = datetime.now(timezone.utc)
        run = Run(
            tenant_id=tenant.id, conversation_id=conversation.id if conversation else None, user_id=user.id,
            input_text=pii.masked, is_eval=options.dry_run, status="running",
        )
        self.db.add(run)
        self.db.commit()
        tracer = Tracer(self.db, run, self.settings)
        yield {"type": "run_started", "run_id": run.id}

        injection = detect_injection(text)
        with tracer.span("guardrail", "input_check", {"text": text}) as span:
            span.set(pii_found=pii.found, injection_matches=injection.matches)
        if injection.suspicious:
            yield {"type": "guardrail", "name": "prompt_injection", "matches": injection.matches}

        decision = self.router.route(text, history_turns=len(history) // 2)
        run.model, run.route_tier = decision.model, decision.tier
        with tracer.span("router", "model_router", {"length": len(text), "history_turns": len(history) // 2}) as span:
            span.set(tier=decision.tier, model=decision.model, reasons=decision.reasons)

        state: dict[str, Any] = {
            "user_text": pii.masked,
            "steps": 0,
            "models": self.router.candidates(decision),
            "options": {"dry_run": options.dry_run, "inject_hallucination": options.inject_hallucination},
            "elapsed_ms": 0,
        }
        messages = [LLMMessage(role="system", content=build_system_prompt(tenant, user)), *history,
                    LLMMessage(role="user", content=pii.masked)]
        ctx = self._context(tenant, user, run, conversation, options.dry_run, [])
        yield from self._loop(run, tracer, ctx, state, messages, queue=[], started=time.perf_counter())

    def resume(self, approval: Approval, decision: str, reviewer: User, comment: str = "") -> list[dict[str, Any]]:
        """Continue a paused run after a human decision. Runs synchronously; the result
        lands in the conversation as a new assistant message."""
        if approval.status != "pending":
            raise ValueError("该审批单已处理")
        if decision not in ("approve", "reject"):
            raise ValueError("decision 必须是 approve 或 reject")
        run = self.db.get(Run, approval.run_id)
        state = dict(run.state or {})
        pending = state.get("pending") or {}
        approval.status = "approved" if decision == "approve" else "rejected"
        approval.reviewer_id = reviewer.id
        approval.comment = comment
        approval.decided_at = datetime.now(timezone.utc)
        run.status = "running"
        self.db.commit()

        tenant = self.db.get(Tenant, run.tenant_id)
        user = self.db.get(User, run.user_id)
        conversation = self.db.get(Conversation, run.conversation_id) if run.conversation_id else None
        evidence = [Evidence(**e) for e in state.get("evidence", [])]
        ctx = self._context(tenant, user, run, conversation, False, evidence)
        tracer = Tracer(self.db, run, self.settings)
        messages = [LLMMessage.from_dict(m) for m in state.get("messages", [])]
        call = ToolCall(**pending["call"])
        queue = [ToolCall(**c) for c in pending.get("queue", [])]
        started = time.perf_counter()

        events: list[dict[str, Any]] = []
        with tracer.span("approval", f"human_{approval.status}", {"approval_id": approval.id}) as span:
            span.set(reviewer=reviewer.name, comment=comment)
        if decision == "approve":
            ctx.approval_id = approval.id
            outcome = self._run_tool(tracer, ctx, call)
            ctx.approval_id = None
        else:
            outcome = ToolOutcome("result", {
                "status": "rejected",
                "comment": comment,
                "message": f"{TOOL_LABEL.get(call.name, '该操作')}申请未通过人工审核。审核意见：{comment or '无'}。",
            })
            ctx.add_evidence("tool", call.name, json.dumps(outcome.output, ensure_ascii=False))
        messages.append(self._tool_message(call, outcome))
        events.append({"type": "tool_result", "id": call.id, "name": call.name, "output": outcome.output,
                       "is_error": outcome.is_error})
        events.extend(self._loop(run, tracer, ctx, state, messages, queue, started))
        return events

    # ================================================================ the loop
    def _loop(self, run: Run, tracer: Tracer, ctx: ToolContext, state: dict[str, Any],
              messages: list[LLMMessage], queue: list[ToolCall], started: float) -> Iterator[dict[str, Any]]:
        stream_tokens = self.settings.evidence_mode != "strict"
        specs = [t.spec() for t in self.registry.for_role(ctx.user.role)]
        while True:
            while queue:
                call = queue.pop(0)
                yield {"type": "tool_call", "id": call.id, "name": call.name, "arguments": call.arguments}
                outcome = self._run_tool(tracer, ctx, call)
                if outcome.kind == "approval":
                    yield from self._pause(run, tracer, ctx, state, messages, call, queue, outcome, started)
                    return
                messages.append(self._tool_message(call, outcome))
                yield {"type": "tool_result", "id": call.id, "name": call.name, "output": outcome.output,
                       "is_error": outcome.is_error}

            if state["steps"] >= self.settings.max_agent_steps:
                yield from self._finish(run, tracer, ctx, state, MAX_STEPS_TEXT, started, status="handoff")
                return
            state["steps"] += 1
            try:
                result = None
                for event in self._call_llm(tracer, messages, specs, state):
                    if event.type == "delta":
                        if stream_tokens:
                            yield {"type": "token", "step": state["steps"], "text": event.text}
                    else:
                        result = event.result
            except LLMError as exc:
                run.error = str(exc)[:2000]
                yield from self._finish(run, tracer, ctx, state, MODEL_ERROR_TEXT, started, status="failed")
                return

            if result.finish_reason == "refusal":
                yield from self._finish(run, tracer, ctx, state, REFUSAL_TEXT, started, status="handoff")
                return
            if result.tool_calls:
                messages.append(LLMMessage(role="assistant", content=result.content,
                                           tool_calls=result.tool_calls, raw=result.raw))
                if result.content and stream_tokens:
                    yield {"type": "step_done", "step": state["steps"]}
                queue = list(result.tool_calls)
                continue
            yield from self._finish(run, tracer, ctx, state, result.content.strip(), started, verify=True)
            return

    def _call_llm(self, tracer: Tracer, messages, specs, state):
        options = {"inject_hallucination": state["options"].get("inject_hallucination", False),
                   "dry_run": state["options"].get("dry_run", False)}
        last_error: LLMError | None = None
        for model in state["models"]:
            emitted = False
            try:
                with tracer.span("llm", f"chat:{model}", {"step": state["steps"], "messages": len(messages)}) as span:
                    span.model = model
                    for event in self.provider.stream(messages, specs, model, options):
                        if event.type == "delta":
                            emitted = True
                        else:
                            r = event.result
                            span.prompt_tokens = r.usage.prompt_tokens
                            span.completion_tokens = r.usage.completion_tokens
                            span.set(
                                content=r.content[:2000],
                                tool_calls=[{"name": c.name, "arguments": c.arguments} for c in r.tool_calls],
                                finish_reason=r.finish_reason,
                                usage_estimated=r.usage.estimated,
                            )
                            tracer.run.model = model
                        yield event
                return
            except LLMError as exc:
                last_error = exc
                if emitted:  # tokens already reached the user; switching models would duplicate text
                    raise
        raise last_error or LLMError("no model available")

    # ================================================================ tools
    def _run_tool(self, tracer: Tracer, ctx: ToolContext, call: ToolCall) -> ToolOutcome:
        tool = self.registry.get(call.name)
        allowed = {t.name for t in self.registry.for_role(ctx.user.role)}
        with tracer.span("tool", call.name, {"arguments": call.arguments, "risk": tool.risk if tool else None}) as span:
            if tool is None or call.name not in allowed:
                outcome = ToolOutcome("result", {"error": f"工具 {call.name} 不存在或当前身份无权使用"}, True)
            elif "__invalid_json__" in call.arguments:
                outcome = ToolOutcome("result", {"error": "工具参数不是合法 JSON"}, True)
            else:
                try:
                    if tool.precheck:
                        tool.precheck(ctx, call.arguments)
                    reason = tool.approval(ctx, call.arguments) if (tool.approval and ctx.approval_id is None) else None
                    if reason:
                        span.set(approval_required=True, reason=reason)
                        return ToolOutcome("approval", reason=reason)
                    output = tool.handler(ctx, call.arguments)
                    outcome = ToolOutcome("result", output)
                except ToolError as exc:
                    span.status = "rejected"
                    outcome = ToolOutcome("result", {"error": str(exc)}, True)
                except Exception as exc:  # unexpected bug: keep the loop alive, surface in trace
                    self.db.rollback()
                    span.status = "error"
                    span.set(exception=f"{type(exc).__name__}: {exc}")
                    outcome = ToolOutcome("result", {"error": "工具执行异常，请稍后重试"}, True)
            debug = outcome.output.pop("_debug", None)
            if debug is not None:
                span.set(retrieval=debug)
            span.set(output=outcome.output)
        if call.name != "search_knowledge" or outcome.is_error:
            ctx.add_evidence("tool", call.name, json.dumps(outcome.output, ensure_ascii=False))
        return outcome

    @staticmethod
    def _tool_message(call: ToolCall, outcome: ToolOutcome) -> LLMMessage:
        return LLMMessage(role="tool", content=json.dumps(outcome.output, ensure_ascii=False),
                          tool_call_id=call.id, name=call.name, is_error=outcome.is_error)

    # ================================================================ pause / finish
    def _pause(self, run, tracer, ctx, state, messages, call, queue, outcome, started):
        args = call.arguments
        amount = args.get("amount")
        amount_text = f"（¥{float(amount):.2f}）" if amount is not None else ""
        text = (f"您的{TOOL_LABEL.get(call.name, '操作')}申请{amount_text}{outcome.reason}。"
                "审核完成后会第一时间在这里通知您。")
        if ctx.dry_run:
            yield {"type": "approval_required", "approval_id": None, "tool": call.name, "arguments": args,
                   "reason": outcome.reason}
            yield from self._finish(run, tracer, ctx, state, text, started, status="waiting_approval")
            return
        approval = Approval(
            tenant_id=ctx.tenant.id, run_id=run.id, conversation_id=ctx.conversation_id,
            requested_by=ctx.user.id, tool_name=call.name, tool_call_id=call.id, arguments=args,
            reason=outcome.reason, evidence=[e.to_dict() for e in ctx.evidence],
        )
        self.db.add(approval)
        self.db.commit()
        with tracer.span("notify", "wecom_approval", {"approval_id": approval.id}) as span:
            span.set(**notify_approval(self.settings, approval, ctx.tenant))
        state["pending"] = {
            "approval_id": approval.id,
            "call": {"id": call.id, "name": call.name, "arguments": args},
            "queue": [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in queue],
        }
        state["messages"] = [m.to_dict() for m in messages]
        yield {"type": "approval_required", "approval_id": approval.id, "tool": call.name, "arguments": args,
               "reason": outcome.reason}
        yield from self._finish(run, tracer, ctx, state, text, started, status="waiting_approval",
                                extra_meta={"approval_id": approval.id})

    def _finish(self, run: Run, tracer: Tracer, ctx: ToolContext, state: dict[str, Any], text: str,
                started: float, status: str | None = None, verify: bool = False,
                extra_meta: dict[str, Any] | None = None) -> Iterator[dict[str, Any]]:
        final_text = text or MODEL_ERROR_TEXT
        verification = None
        if verify:
            judge = self.provider if (self.settings.verifier == "llm" and self.provider.name != "mock") else None
            with tracer.span("verify", "evidence_check", {"answer": final_text}) as span:
                verification = verify_answer(final_text, ctx.evidence, state["user_text"], self.settings,
                                             judge, state["models"][0])
                span.set(**verification.to_dict())
            run.evidence_coverage = verification.coverage
            run.verdict = verification.verdict
            yield {"type": "verification", **verification.to_dict()}
            if self.settings.evidence_mode == "strict" and verification.verdict == "blocked":
                final_text = STRICT_FALLBACK
                yield {"type": "answer_replaced", "text": final_text, "reason": "证据校验未通过（严格模式）"}

        tools_used = [s for s in self._tool_names(run.id)]
        if status is None:
            status = "handoff" if "handoff_to_human" in tools_used else "completed"
        citations = [e.to_dict() for e in ctx.evidence if e.kind == "kb"]
        meta = {
            "citations": citations,
            "verification": verification.to_dict() if verification else None,
            "tools": tools_used,
            "status": status,
            **(extra_meta or {}),
        }
        message_id = None
        if ctx.conversation_id and not ctx.dry_run:
            message = Message(tenant_id=run.tenant_id, conversation_id=ctx.conversation_id, role="assistant",
                              content=final_text, run_id=run.id, meta=meta)
            self.db.add(message)
            self.db.flush()
            message_id = message.id

        state["elapsed_ms"] = int(state.get("elapsed_ms", 0) + (time.perf_counter() - started) * 1000)
        state["evidence"] = [e.to_dict() for e in ctx.evidence]
        if status != "waiting_approval":
            state.pop("messages", None)
            state.pop("pending", None)
            run.finished_at = datetime.now(timezone.utc)
        run.state = state
        run.status = status
        run.output_text = final_text
        run.steps = state["steps"]
        run.latency_ms = state["elapsed_ms"]
        self.db.commit()
        yield {"type": "final", "run_id": run.id, "message_id": message_id, "text": final_text,
               "status": status, "meta": meta}
        yield {"type": "done", "run_id": run.id, "status": status, "latency_ms": run.latency_ms,
               "prompt_tokens": run.prompt_tokens, "completion_tokens": run.completion_tokens,
               "cost": run.cost, "model": run.model, "route_tier": run.route_tier}

    # ================================================================ helpers
    def _context(self, tenant, user, run, conversation, dry_run, evidence) -> ToolContext:
        return ToolContext(
            db=self.db, settings=self.settings, tenant=tenant, user=user, embedder=self.embedder,
            run_id=run.id, conversation_id=conversation.id if conversation else None,
            dry_run=dry_run, evidence=evidence,
        )

    def _history(self, conversation: Conversation | None) -> list[LLMMessage]:
        if conversation is None:
            return []
        limit = self.settings.history_turns * 2
        rows = self.db.execute(
            select(Message).where(Message.conversation_id == conversation.id).order_by(Message.id.desc()).limit(limit)
        ).scalars().all()
        return [LLMMessage(role=m.role, content=m.content) for m in reversed(rows)]

    def _tool_names(self, run_id: str) -> list[str]:
        from ..models import Span

        return list(self.db.execute(
            select(Span.name).where(Span.run_id == run_id, Span.kind == "tool").order_by(Span.id)
        ).scalars())


def collect(events: Iterator[dict[str, Any]]) -> dict[str, Any]:
    """Drain an event stream into a summary (used by the non-streaming API, channels and evals)."""
    summary: dict[str, Any] = {"events": [], "tool_calls": [], "tool_results": [], "approval": None,
                               "verification": None, "final": None, "done": None}
    for event in events:
        summary["events"].append(event)
        kind = event["type"]
        if kind == "tool_call":
            summary["tool_calls"].append(event["name"])
        elif kind == "tool_result":
            summary["tool_results"].append(event)
        elif kind == "approval_required":
            summary["approval"] = event
        elif kind == "verification":
            summary["verification"] = event
        elif kind == "final":
            summary["final"] = event
        elif kind == "done":
            summary["done"] = event
    return summary
