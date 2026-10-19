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
import logging
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..channels.wecom import notify_approval
from ..config import Settings
from ..guardrails.injection import detect_injection
from ..guardrails.pii import PIIReport, mask_pii
from ..llm.base import Embedder, LLMError, LLMMessage, LLMProvider, ToolCall
from ..llm.router import ModelRouter
from ..models import Approval, Conversation, Message, Run, Tenant, User
from . import answer_cache
from .rewrite import Frame, frame_of, is_followup, next_frame, rewrite_followup
from ..observability import request_id_var
from ..textutil import normalize_width
from .prompts import build_system_prompt, choose_prompt_version
from . import handoff
from .tools import ApprovalNeed, Evidence, ToolContext, ToolError, ToolRegistry
from .tracing import Tracer
from .verifier import verify_answer

STRICT_FALLBACK = "抱歉，我暂时无法给出有可靠依据的答复。为避免误导，建议您转人工客服确认。"
MAX_STEPS_TEXT = "抱歉，这个问题需要的处理步骤较多。"
MODEL_ERROR_TEXT = "抱歉，智能客服暂时繁忙，请稍后再试或转人工客服。"
REFUSAL_TEXT = "抱歉，这个问题我无法处理。"
APPROVED_FALLBACK_TEXT = "您的申请已通过人工审核并处理完成。"
TOOL_LABEL = {"request_refund": "退款"}

logger = logging.getLogger(__name__)


class ResumeError(RuntimeError):
    """Resuming a run after a human decision failed before the decision took effect.
    The approval is back to pending, so the reviewer can simply decide again."""


@dataclass
class RunOptions:
    dry_run: bool = False
    inject_hallucination: bool = False
    prompt_version: str | None = None  # force a version (offline A/B); otherwise default/canary


@dataclass
class ToolOutcome:
    kind: str  # result | approval
    output: dict[str, Any] = field(default_factory=dict)
    is_error: bool = False
    reason: str = ""  # for reviewers and staff
    customer_note: str = ""  # what a customer may be told about the approval
    arguments: dict[str, Any] | None = None  # normalized arguments a human is asked to approve


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
        text = normalize_width(text)  # "ＳＯ２０２６…" is an order number too
        pii = mask_pii(text) if self.settings.pii_masking else PIIReport(text, [])
        history = self._history(conversation)
        previous, frame = self._previous_turn(conversation)
        user_message = None
        if conversation is not None:  # dry runs pass a conversation only inside the eval sandbox
            user_message = Message(tenant_id=tenant.id, conversation_id=conversation.id, role="user",
                                   content=pii.masked, meta={})
            self.db.add(user_message)
            if conversation.title == "新对话":
                conversation.title = pii.masked[:30]
            conversation.updated_at = datetime.now(timezone.utc)
            if conversation.status == "handoff":
                if handoff.active_ticket(self.db, conversation.id) is not None:
                    yield from self._forward_to_human(conversation)
                    return
                conversation.status = "bot"  # no live ticket behind the flag: the agent takes over again
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
            # Staff see which rules matched; telling an attacker that would help them rephrase.
            yield {"type": "guardrail", "name": "prompt_injection",
                   **({"matches": injection.matches} if user.role in ("admin", "agent") else {})}

        # Multi-turn: turn an explicit follow-up ("那运费谁出？") into a standalone question.
        question = pii.masked
        if is_followup(text, previous):
            with tracer.span("rewrite", "followup_rewrite", {"question": pii.masked, "previous": previous}) as span:
                span.model = self.settings.llm_model_fast
                result, span.prompt_tokens, span.completion_tokens = rewrite_followup(
                    self.provider, self.settings.llm_model_fast, history, pii.masked, frame)
                span.set(standalone=result.standalone, method=result.method)
            question = result.standalone
            if user_message is not None:  # the next follow-up builds on this, not on what was typed
                after = next_frame(frame, pii.masked, question)
                user_message.meta = {**(user_message.meta or {}), "standalone": question,
                                     "frame": {"topic": after.topic, "predicate": after.predicate}}
            yield {"type": "rewrite", "standalone": question, "method": result.method}

        # Prompt version: forced (offline A/B) > canary split by conversation > default.
        prompt_version = options.prompt_version or choose_prompt_version(
            self.settings.prompt_version, self.settings.prompt_canary_version, self.settings.prompt_canary_percent,
            f"conversation:{conversation.id}" if conversation else f"run:{run.id}",
        )

        # Answer cache: only for self-contained, PII-free, injection-free first questions.
        cache_key = None
        if (self.settings.answer_cache_ttl_hours > 0 and not history and not pii.found
                and not injection.suspicious and not options.dry_run and not options.inject_hallucination):
            cache_key = answer_cache.make_key(self.db, self.settings, tenant, user, text, prompt_version)
            entry = answer_cache.lookup(self.db, cache_key)
            if entry is not None:
                yield from self._serve_cached(run, tracer, tenant, user, conversation, pii.masked, entry)
                return

        decision = self.router.route(text, history_turns=len(history) // 2)
        run.model, run.route_tier = decision.model, decision.tier
        with tracer.span("router", "model_router", {"length": len(text), "history_turns": len(history) // 2}) as span:
            span.set(tier=decision.tier, model=decision.model, reasons=decision.reasons, prompt_version=prompt_version)

        state: dict[str, Any] = {
            "user_text": pii.masked,
            "steps": 0,
            "models": self.router.candidates(decision),
            "options": {"dry_run": options.dry_run, "inject_hallucination": options.inject_hallucination},
            "elapsed_ms": 0,
            "cache_key": cache_key.__dict__ if cache_key else None,
            "prompt_version": prompt_version,
            "request_id": request_id_var.get(),
        }
        messages = [LLMMessage(role="system", content=build_system_prompt(tenant, user, version=prompt_version)), *history,
                    LLMMessage(role="user", content=question)]
        ctx = self._context(tenant, user, run, conversation, options.dry_run, [])
        try:
            yield from self._loop(run, tracer, ctx, state, messages, queue=[], started=time.perf_counter())
        except Exception as exc:  # a bug or a DB error, not a model error (those end the run politely)
            self._mark_failed(run, exc, tracer)
            raise

    def _serve_cached(self, run: Run, tracer: Tracer, tenant: Tenant, user: User,
                      conversation: Conversation | None, user_text: str, entry) -> Iterator[dict[str, Any]]:
        started = time.perf_counter()
        run.model, run.route_tier = "answer-cache", "cache"
        age = (datetime.now(timezone.utc) - answer_cache_created(entry)).total_seconds()
        with tracer.span("cache", "answer_cache_hit", {"question": entry.question}) as span:
            span.set(cache_id=entry.id, hits=entry.hits, age_seconds=int(age), kb_signature=entry.kb_signature)
        for start in range(0, len(entry.answer), 8):
            yield {"type": "token", "step": 1, "text": entry.answer[start : start + 8]}
        verification = (entry.meta or {}).get("verification")
        if verification:
            run.evidence_coverage = verification.get("coverage")
            run.verdict = verification.get("verdict")
            yield {"type": "verification", **verification}
        evidence = [Evidence(**e) for e in (entry.meta or {}).get("evidence", [])]
        ctx = self._context(tenant, user, run, conversation, False, evidence)
        state = {"user_text": user_text, "steps": 0, "models": [], "options": {}, "elapsed_ms": 0}
        yield from self._finish(run, tracer, ctx, state, entry.answer, started, status="completed",
                                extra_meta={"verification": verification, "cached": True})

    def resume(self, approval: Approval, decision: str, reviewer: User, comment: str = "") -> list[dict[str, Any]]:
        """Continue a paused run after a human decision. Runs synchronously; the result
        lands in the conversation as a new assistant message.

        Failure handling: if anything breaks before the decision takes effect, the run is
        marked failed and the approval goes back to pending (``ResumeError``), so it can be
        decided again. If the approved tool already ran (money moved), the decision stands
        and the customer gets the tool's own result instead of a model-written answer."""
        if approval.status != "pending":
            raise ValueError("该审批单已处理")
        if decision not in ("approve", "reject"):
            raise ValueError("decision 必须是 approve 或 reject")
        # Claim the approval with a conditional UPDATE: when two reviewers decide at the
        # same time, exactly one of them moves it out of "pending" and runs the tool.
        claimed = self.db.execute(
            update(Approval)
            .where(Approval.id == approval.id, Approval.status == "pending")
            .values(status="approved" if decision == "approve" else "rejected", reviewer_id=reviewer.id,
                    comment=comment, decided_at=datetime.now(timezone.utc))
        ).rowcount
        if claimed != 1:
            self.db.rollback()
            raise ValueError("该审批单已处理")
        run = self.db.get(Run, approval.run_id)
        run.status = "running"
        self.db.commit()

        state = dict(run.state or {})
        tenant = self.db.get(Tenant, run.tenant_id)
        user = self.db.get(User, run.user_id)
        conversation = self.db.get(Conversation, run.conversation_id) if run.conversation_id else None
        evidence = [Evidence(**e) for e in state.get("evidence", [])]
        ctx = self._context(tenant, user, run, conversation, False, evidence)
        tracer = Tracer(self.db, run, self.settings)
        started = time.perf_counter()
        executed: ToolOutcome | None = None  # set once the approved tool has really run

        events: list[dict[str, Any]] = []
        try:
            pending = state["pending"]
            messages = [LLMMessage.from_dict(m) for m in state.get("messages", [])]
            call = ToolCall(**pending["call"])
            queue = [ToolCall(**c) for c in pending.get("queue", [])]
            with tracer.span("approval", f"human_{approval.status}", {"approval_id": approval.id}) as span:
                span.set(reviewer=reviewer.name, comment=comment)
            if decision == "approve":
                ctx.approval_id = approval.id
                outcome = self._run_tool(tracer, ctx, call)
                ctx.approval_id = None
                if not outcome.is_error:
                    executed = outcome
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
        except Exception as exc:
            self._mark_failed(run, exc, tracer)
            if executed is None:
                self._reopen(approval, run)
                raise ResumeError(f"审批执行失败，审批单 #{approval.id} 已退回待处理，可以重新审批。"
                                  f"错误：{type(exc).__name__}") from exc
            # The decision already took effect: keep it and tell the customer what happened.
            # The run stays "failed" for operators; the customer-facing outcome is complete.
            fallback = str(executed.output.get("message") or APPROVED_FALLBACK_TEXT)
            events.extend(self._finish(run, tracer, ctx, state, fallback, started, status="failed",
                                       extra_meta={"status": "completed", "fallback": True}))
            return events

    def _mark_failed(self, run: Run, exc: Exception, tracer: Tracer | None = None) -> None:
        """Record an unexpected crash on the run so it never stays "running"."""
        logger.exception("agent run %s failed", run.id)
        self.db.rollback()
        if tracer is not None:  # the spans up to the crash are what an operator needs most
            tracer.flush()
        run.status = "failed"
        run.error = f"{type(exc).__name__}: {exc}"[:2000]
        run.finished_at = datetime.now(timezone.utc)
        self.db.commit()

    def _reopen(self, approval: Approval, run: Run) -> None:
        approval.status = "pending"
        approval.reviewer_id = None
        approval.comment = ""
        approval.decided_at = None
        run.finished_at = None
        self.db.commit()
        with Tracer(self.db, run, self.settings, buffered=False).span(
                "approval", "reopened", {"approval_id": approval.id}) as span:
            span.status = "error"
            span.set(error=run.error, approval_status="pending")

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
                text = MAX_STEPS_TEXT + self._fallback_handoff(tracer, ctx, "Agent 达到最大步数")
                yield from self._finish(run, tracer, ctx, state, text, started, status="handoff")
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
                text = REFUSAL_TEXT + self._fallback_handoff(tracer, ctx, "模型拒绝回答")
                yield from self._finish(run, tracer, ctx, state, text, started, status="handoff")
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
                    args = call.arguments
                    if tool.precheck:
                        # The precheck returns the arguments as they would be executed (e.g. an
                        # omitted refund amount made explicit). The approval policy, the approval
                        # card and the execution all use them, never the model's raw arguments.
                        args = {**args, **(tool.precheck(ctx, args) or {})}
                    need = tool.approval(ctx, args) if (tool.approval and ctx.approval_id is None) else None
                    if isinstance(need, str):
                        need = ApprovalNeed(need)
                    if need:
                        span.set(approval_required=True, reason=need.reason, customer_note=need.customer_note,
                                 arguments=args)
                        return ToolOutcome("approval", reason=need.reason, customer_note=need.customer_note,
                                           arguments=args)
                    output = tool.handler(ctx, args)
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

    def _fallback_handoff(self, tracer: Tracer, ctx: ToolContext, reason: str) -> str:
        """The agent gave up (step limit, model refusal): really hand the customer to a person
        rather than only suggesting it. A ticket and the conversation's handoff state make the
        handoff count honest (someone is actually queued), and the trace shows it as a tool span."""
        outcome = self._run_tool(tracer, ctx, ToolCall(id="fallback_handoff", name="handoff_to_human",
                                                       arguments={"reason": reason}))
        return str(outcome.output.get("message") or "请稍后联系人工客服。")

    @staticmethod
    def _tool_message(call: ToolCall, outcome: ToolOutcome) -> LLMMessage:
        return LLMMessage(role="tool", content=json.dumps(outcome.output, ensure_ascii=False),
                          tool_call_id=call.id, name=call.name, is_error=outcome.is_error)

    # ================================================================ pause / finish
    def _pause(self, run, tracer, ctx, state, messages, call, queue, outcome, started):
        args = outcome.arguments or call.arguments
        amount = args.get("amount")
        amount_text = f"（¥{float(amount):.2f}）" if amount is not None else ""
        # The approval record keeps the full reason for the reviewer; a customer only hears
        # the public part of it (see ApprovalNeed).
        shown = outcome.reason if ctx.is_staff else outcome.customer_note
        text = (f"您的{TOOL_LABEL.get(call.name, '操作')}申请{amount_text}已提交人工审核"
                f"{'：' + shown if shown else ''}。审核完成后会第一时间在这里通知您。")
        if ctx.dry_run:
            yield {"type": "approval_required", "approval_id": None, "tool": call.name, "arguments": args,
                   "reason": shown}
            yield from self._finish(run, tracer, ctx, state, text, started, status="waiting_approval")
            return
        approval = Approval(
            tenant_id=ctx.tenant.id, run_id=run.id, conversation_id=ctx.conversation_id,
            requested_by=ctx.user.id, tool_name=call.name, tool_call_id=call.id, arguments=args,
            reason=outcome.reason, evidence=[e.to_dict() for e in ctx.evidence],
        )
        self.db.add(approval)
        tracer.flush()
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
               "reason": shown}
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

        # The tools this run used decide its status and whether the answer may be cached. They
        # are tracked in memory (not read back from span rows, which buffered trace writes have
        # not written yet), plus those from before an approval pause, kept in the run state.
        tools_used = list(state.get("tools", [])) + tracer.tools
        state["tools"] = tools_used
        if status is None:
            status = "handoff" if "handoff_to_human" in tools_used else "completed"
        cache_key = state.get("cache_key")
        if (cache_key and status == "completed" and verification is not None and verification.verdict == "pass"
                and tools_used and set(tools_used) == {"search_knowledge"}):
            answer_cache.store(self.db, answer_cache.CacheKey(**cache_key), final_text,
                               {"evidence": [e.to_dict() for e in ctx.evidence], "verification": verification.to_dict()},
                               self.settings.answer_cache_ttl_hours)
        citations = [e.to_dict() for e in ctx.evidence if e.kind == "kb"]
        meta = {
            "citations": citations,
            "verification": verification.to_dict() if verification else None,
            "tools": tools_used,
            "status": status,
            **(extra_meta or {}),
        }
        message_id = None
        if ctx.conversation_id:
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
        tracer.flush()
        self.db.commit()
        yield {"type": "final", "run_id": run.id, "message_id": message_id, "text": final_text,
               "status": status, "meta": meta}
        done = {"type": "done", "run_id": run.id, "status": status, "latency_ms": run.latency_ms}
        if ctx.is_staff:  # tokens, cost, model and routing are operational data, not for customers
            done.update(prompt_tokens=run.prompt_tokens, completion_tokens=run.completion_tokens,
                        cost=run.cost, model=run.model, route_tier=run.route_tier)
        yield done

    # ================================================================ helpers
    def _context(self, tenant, user, run, conversation, dry_run, evidence) -> ToolContext:
        return ToolContext(
            db=self.db, settings=self.settings, tenant=tenant, user=user, embedder=self.embedder,
            run_id=run.id, conversation_id=conversation.id if conversation else None,
            dry_run=dry_run, evidence=evidence,
        )

    def _forward_to_human(self, conversation: Conversation) -> Iterator[dict[str, Any]]:
        """A person owns this conversation: keep the message for them and tell the customer
        where they stand. No run, no model call."""
        self.db.commit()
        current = handoff.state(self.db, conversation)
        yield {"type": "handoff", **(current or {})}
        yield {"type": "final", "run_id": None, "message_id": None, "text": handoff.notice(current),
               "status": "handoff", "meta": {"handoff": current}}
        yield {"type": "done", "run_id": None, "status": "handoff", "latency_ms": 0}

    def _previous_turn(self, conversation: Conversation | None) -> tuple[str | None, Frame | None]:
        """The customer's previous question as understood (its standalone rewrite if it was a
        follow-up) and the conversation frame after it."""
        if conversation is None:
            return None, None
        last = self.db.execute(
            select(Message).where(Message.conversation_id == conversation.id, Message.role == "user")
            .order_by(Message.id.desc()).limit(1)
        ).scalar_one_or_none()
        if last is None:
            return None, None
        meta = last.meta or {}
        question = meta.get("standalone") or last.content
        frame = Frame(**meta["frame"]) if meta.get("frame") else frame_of(question)
        return question, frame

    def _history(self, conversation: Conversation | None) -> list[LLMMessage]:
        if conversation is None:
            return []
        limit = self.settings.history_turns * 2
        rows = self.db.execute(
            select(Message).where(Message.conversation_id == conversation.id).order_by(Message.id.desc()).limit(limit)
        ).scalars().all()
        history = []
        for m in reversed(rows):  # model APIs know only user and assistant turns
            if m.role in ("user", "assistant"):
                history.append(LLMMessage(role=m.role, content=m.content))
            elif m.role == "agent":  # what a person told the customer is context the agent needs after hand-back
                history.append(LLMMessage(role="assistant", content=f"（人工客服）{m.content}"))
        return history


def answer_cache_created(entry) -> datetime:
    created = entry.created_at
    return created.replace(tzinfo=timezone.utc) if created.tzinfo is None else created


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
