/**
 * One turn of the agent in the browser: app/agent/runtime.py without a model or a server. It emits the same AgentEvent
 * stream, so the console's citations, trace and approval screens work unchanged.
 */
import type { AgentEvent, EvidenceItem, SpanRow, Verification } from "../../types";
import { INJECTION, PII, T, W, pack, type TenantConfig } from "./pack";
import { compose, nextCall, readsAsOrder, understood, type Results, type Turn } from "./planner";
import { search } from "./retrieval";
import { addEvidence, availableTools, runTool, type ApprovalRow, type Evidence, type ToolContext, type World, SIMULATED } from "./tools";
import { asksForPerson, canonical, extractNumbers, findAll, handoffCancelled, money, normalizeWidth, overlapRecall, refundWithdrawRequested, splitSentences, stripCitations, sub } from "./text";

const LEADING = pack.texts.leading;
const H = pack.texts.handoff;
export const ENGINE_RULES = "browser-rules";
export const ENGINE_MODEL = "qwen3-0.6b-webgpu";

export interface Frame {
  topic: string;
  predicate: string;
}

export interface Message {
  id: number;
  role: "user" | "assistant" | "system";
  content: string;
  run_id: string | null;
  meta: Record<string, unknown>;
  created_at: string;
  feedback: null;
}

/** A conversation as the engine remembers it. */
export interface EngineConversation {
  id: number;
  email: string;
  messages: Message[];
  /** Standalone reading of the customer's last turn and the frame after it, for follow-ups. */
  previous: string | null;
  frame: Frame | null;
  withdrawOffer: Record<string, unknown> | null;
  handoff: { ticket_id: number; status: string; position: number; assignee: null } | null;
}

export interface RunRecord {
  id: string;
  tenant: string;
  email: string;
  name: string;
  role: string;
  status: string;
  model: string;
  route_tier: string;
  input_text: string;
  output_text: string;
  steps: number;
  latency_ms: number;
  evidence_coverage: number | null;
  verdict: string | null;
  created_at: string;
  spans: SpanRow[];
  evidence: Evidence[];
  approvals: { id: number; status: string; tool_name: string; reason: string }[];
}

/** What a model tier chose, already validated by the program (tier2.ts). */
export interface ModelChoice {
  question: string; // the turn as the rules should read it
  evidence: number[]; // chunk ids the model picked, for a policy question
  raw: string;
  note: string; // what the validator did
}

export interface TurnOptions {
  world: World;
  conversation: EngineConversation;
  email: string;
  name: string;
  role: "admin" | "agent" | "customer";
  tenant: string;
  config: TenantConfig;
  question: string;
  choice?: ModelChoice | null;
  /** Called once a run exists. */
  onRun?: (run: RunRecord) => void;
}

function maskPii(text: string): { masked: string; found: string[] } {
  const found: string[] = [];
  let out = text;
  for (const [kind, pattern] of PII) {
    out = sub(pattern, (value) => {
      found.push(kind);
      if (kind === "email") {
        const [name, domain] = value.split("@");
        return `${name.slice(0, 1)}***@${domain}`;
      }
      let digits = value.replace(/\D/g, "");
      if (kind === "phone") {
        digits = digits.slice(-11);
        return `${digits.slice(0, 3)}****${digits.slice(-4)}`;
      }
      if (kind === "id_card") digits = value;
      return `${digits.slice(0, 4)}${"*".repeat(Math.max(0, digits.length - 8))}${digits.slice(-4)}`;
    }, out);
  }
  return { masked: out, found };
}

function detectInjection(text: string): string[] {
  const norm = canonical(text.normalize("NFKC").toLowerCase()).replace(/[\s​-‏⁠﻿‪-‮]/g, "");
  return INJECTION.filter(([, pattern]) => pattern.test(norm)).map(([name]) => name);
}

// ---------------------------------------------------------------- follow-ups (app/agent/rewrite.py)
const QUESTION_WORDS = W._QUESTION_WORDS, LEADING_RE = W._LEADING_RE, OWN_QUESTION = W._OWN_QUESTION;
const END_PUNCT = "？?！!。 ";
const rstrip = (text: string, chars: string) => {
  let end = text.length;
  while (end > 0 && chars.includes(text[end - 1])) end--;
  return text.slice(0, end);
};

function isFollowup(text: string, previous: string | null): boolean {
  if (!previous) return false;
  const stripped = text.trim();
  if (T.ORDER_ID_RE.test(stripped)) return false;
  return LEADING.some((word) => stripped.startsWith(word)) || rstrip(stripped, "？? ").endsWith("呢");
}

const topicOf = (question: string) => sub(QUESTION_WORDS, "", sub(LEADING_RE, "", question.trim())).trim();
const frameOf = (question: string): Frame => ({ topic: topicOf(question), predicate: topicOf(question) });

function kind(text: string): "new" | "subject" | "predicate" {
  const stripped = text.trim();
  const rest = rstrip(sub(LEADING_RE, "", stripped), END_PUNCT);
  if ((stripped.startsWith("还有") || stripped.startsWith("另外")) && !rest.endsWith("呢") && OWN_QUESTION.test(rest)) return "new";
  if (rest.endsWith("呢") && !OWN_QUESTION.test(rest.slice(0, -1))) return "subject";
  return "predicate";
}

function focus(text: string): string {
  const out = rstrip(sub(LEADING_RE, "", text.trim()), END_PUNCT);
  return out.endsWith("呢") ? out.slice(0, -1) : out;
}

function ruleRewrite(text: string, previous: Frame): string {
  let frame = previous;
  if (T.OTHER_ORDER.test(text)) {
    const clean = (s: string) => sub(T.ORDER_ID_RE, "", s).replace(/^[ ，,]+|[ ，,]+$/g, "");
    frame = { topic: clean(frame.topic), predicate: clean(frame.predicate) };
  }
  const k = kind(text);
  if (k === "new") return sub(LEADING_RE, "", text.trim());
  if (k === "subject") return `${focus(text)}，${frame.predicate}`;
  return `${frame.topic}，${sub(LEADING_RE, "", text.trim())}`;
}

function nextFrame(previous: Frame, text: string, standalone: string): Frame {
  const k = kind(text);
  if (k === "new") return frameOf(standalone);
  if (k === "subject") return { topic: focus(text), predicate: previous.predicate };
  return { topic: previous.topic, predicate: topicOf(text) };
}

function carryOrder(text: string, previous: string | null): string | null {
  if (!previous || T.ORDER_ID_RE.test(text) || W._DISOWNED.test(previous)) return null;
  const named = [...new Set(findAll(T.ORDER_ID_RE, previous).map((m) => m[0].toUpperCase()))];
  if (named.length !== 1) return null;
  if (!(T.REFUND_RE.test(text) || refundWithdrawRequested(text) || W._PARCEL.test(text))) return null;
  if (sub(W._CARRY_WORDS, "", text) || rstrip(text.trim(), END_PUNCT).endsWith("呢")) return null;
  return `${named[0]} ${text.trim()}`;
}

function carryOrderLoosely(text: string, previous: string | null): [string, string] | null {
  if (!previous || T.ORDER_ID_RE.test(text) || W._DISOWNED.test(previous) || W._DISOWNED.test(text) || T.OTHER_ORDER.test(text)) return null;
  const named = [...new Set(findAll(T.ORDER_ID_RE, previous).map((m) => m[0].toUpperCase()))];
  if (named.length !== 1 || text.trim().length > 40 || W._POLICY_WORDS.test(text)) return null;
  if (!W._LOOSE_CARRY.test(text) || W._OTHER_ITEM.test(text)) return null;
  return [`${named[0]} ${text.trim()}`, named[0]];
}

// ---------------------------------------------------------------- the answer check (app/agent/verifier.py, lexical)
const V = Object.fromEntries(Object.entries(pack.rules.verifier).map(([name, rule]) => [name, new RegExp(rule.source, rule.flags)]));

function claimsNothing(text: string): boolean {
  if (V._NON_FACTUAL.test(text) || new RegExp(pack.rules.outcome.REFUSAL_RE.source).test(text)) return true;
  if (/如下[:：]$/.test(text)) return true;
  const clauses = text.split(V._CLAUSE_SPLIT).map((c) => c?.trim()).filter(Boolean);
  return clauses.length > 0 && clauses.every((c) => V._CLAIMLESS_CLAUSE.test(c));
}

/** Every sentence checked against the run's evidence: its numbers, its order numbers, its words. */
export function verifyAnswer(answer: string, evidence: Evidence[], userText: string): Verification {
  const byRef = new Map(evidence.map((e) => [e.ref, e]));
  const evidenceText = evidence.map((e) => e.text).join("\n");
  const pool = [...extractNumbers(evidenceText), ...extractNumbers(userText)];
  const knownIds = new Set(findAll(T.ORDER_ID_RE, `${evidenceText} ${userText}`).map((m) => m[0].toUpperCase()));
  const invalid = new Set<number>();
  const checks: Verification["sentences"] = [];
  for (const raw of splitSentences(answer)) {
    const [text, cited] = stripCitations(raw);
    if (!text) continue;
    cited.filter((r) => !byRef.has(r)).forEach((r) => invalid.add(r));
    const claim = sub(V._LIST_NUMBER, "", sub(V._COURTESY, "", text.replace(SIMULATED, "")));
    const numbers = extractNumbers(claim);
    const factual = !(claim.length < 6 || (!numbers.length && claimsNothing(claim)));
    const check = { text, factual, supported: true, score: 1, cited, unverified_numbers: [] as number[], unknown_ids: [] as string[] };
    if (factual) {
      check.unverified_numbers = numbers.filter((n) => !pool.some((p) => Math.abs(p - n) < 1e-6));
      check.unknown_ids = findAll(T.ORDER_ID_RE, claim).map((m) => m[0].toUpperCase()).filter((id) => !knownIds.has(id));
      const candidates = cited.map((r) => byRef.get(r)).filter((e): e is Evidence => Boolean(e));
      const scores = (candidates.length ? candidates : evidence).map((e) => overlapRecall(claim, `${e.text} ${String(e.meta.heading ?? "")}`));
      const best = Math.max(0, ...scores);
      check.score = Math.round(best * 1000) / 1000;
      check.supported = best >= (numbers.length ? 0.3 : 0.5) && !check.unverified_numbers.length && !check.unknown_ids.length;
    }
    checks.push(check);
  }
  const factualChecks = checks.filter((c) => c.factual);
  const supported = factualChecks.filter((c) => c.supported).length;
  const coverage = factualChecks.length ? supported / factualChecks.length : 1;
  const hard = factualChecks.some((c) => c.unverified_numbers.length || c.unknown_ids.length);
  const verdict = hard || coverage < 0.5 ? "blocked" : coverage >= 0.8 && !invalid.size ? "pass" : "warn";
  return { verdict, coverage: Math.round(coverage * 1000) / 1000, sentences: checks, invalid_citations: [...invalid].sort((a, b) => a - b), method: "lexical" };
}

// ---------------------------------------------------------------- one turn
const nowIso = (world: World) => new Date(world.now()).toISOString();
let spanId = 0;

function span(kindName: string, name: string, input: Record<string, unknown>, output: Record<string, unknown>, status = "ok", started?: number): SpanRow {
  spanId += 1;
  return { id: spanId, kind: kindName, name, input, output, status, latency_ms: started === undefined ? 0 : Math.max(0, Math.round(performance.now() - started)), prompt_tokens: 0, completion_tokens: 0 };
}

function evidenceItem(e: Evidence): EvidenceItem {
  return e as unknown as EvidenceItem;
}

export const newRunId = (): string => [...crypto.getRandomValues(new Uint8Array(16))].map((b) => b.toString(16).padStart(2, "0")).join("");

function addAssistant(world: World, conversation: EngineConversation, runId: string | null, content: string, meta: Record<string, unknown>): number {
  const id = world.id();
  conversation.messages.push({ id, role: "assistant", content, run_id: runId, meta, created_at: nowIso(world), feedback: null });
  return id;
}

export function runTurn(options: TurnOptions): AgentEvent[] {
  const { world, conversation, email, name, role, tenant, config, choice } = options;
  const started = performance.now();
  const said = normalizeWidth(options.question);
  const text = canonical(said);
  const pii = maskPii(text);
  const history: Turn[] = conversation.messages.filter((m) => m.role === "user" || m.role === "assistant")
    .map((m) => ({ role: m.role as Turn["role"], content: m.role === "user" ? canonical(m.content) : m.content }));
  const previous = conversation.previous;
  const frame = conversation.frame;
  const userMessage: Message = { id: world.id(), role: "user", content: maskPii(said).masked, run_id: null, meta: {}, created_at: nowIso(world), feedback: null };
  conversation.messages.push(userMessage);
  conversation.previous = canonical(userMessage.content);
  conversation.frame = frameOf(conversation.previous);
  const offer = conversation.withdrawOffer;
  conversation.withdrawOffer = null;
  const events: AgentEvent[] = [];
  const engine = choice ? ENGINE_MODEL : ENGINE_RULES;

  // A person has this conversation: the agent stays out, the message waits for them.
  let turnText = pii.masked;
  if (conversation.handoff) {
    const [cancelled, rest] = handoffCancelled(text);
    if (cancelled) {
      const ticket = conversation.handoff.ticket_id;
      world.tickets = world.tickets.filter((t) => t.id !== ticket);
      conversation.handoff = null;
      if (!rest) {
        const said2 = H.CANCELLED_TEXT;
        const id = addAssistant(world, conversation, null, said2, { status: "completed", engine });
        return [{ type: "final", run_id: "", message_id: id, text: said2, status: "completed", meta: { citations: [], status: "completed", ...({ engine } as object) } },
          { type: "done", run_id: "", status: "completed", latency_ms: 0 }];
      }
      turnText = rest;
    } else {
      const notice = `已为您转接人工客服，当前排队第 ${conversation.handoff.position} 位。您可以继续留言，人工客服接入后会在这里回复。`;
      return [{ type: "handoff", ...conversation.handoff }, { type: "final", run_id: "", message_id: null, text: notice, status: "handoff", meta: { ...({ engine } as object) } },
        { type: "done", run_id: "", status: "handoff", latency_ms: 0 }];
    }
  }

  const runId = newRunId();
  const run: RunRecord = { id: runId, tenant, email, name, role, status: "running", model: engine, route_tier: choice ? "fast" : "rule", input_text: pii.masked, output_text: "", steps: 0, latency_ms: 0,
    evidence_coverage: null, verdict: null, created_at: nowIso(world), spans: [], evidence: [], approvals: [] };
  options.onRun?.(run);
  events.push({ type: "run_started", run_id: runId });

  const injection = detectInjection(turnText);
  run.spans.push(span("guardrail", "input_check", { text: turnText }, { pii_found: pii.found, injection_matches: injection }));
  if (injection.length) events.push({ type: "guardrail", name: "prompt_injection", ...(role === "customer" ? {} : { matches: injection }) });

  const ctx: ToolContext = { tenant, config, user: email, role, conversationId: conversation.id, runId, evidence: run.evidence, customerText: turnText, unconfirmedOrder: null, approvalId: null, withdrawOffer: offer };
  const finish = (answer: string, status: string | null, verify: boolean, extra: Record<string, unknown> = {}): AgentEvent[] => {
    const tools = run.spans.filter((s) => s.kind === "tool").map((s) => s.name);
    const finalStatus = status ?? (tools.includes("handoff_to_human") ? "handoff" : "completed");
    let verification: Verification | null = null;
    if (verify) {
      verification = verifyAnswer(answer, run.evidence, turnText);
      run.spans.push(span("verify", "evidence_check", { answer }, verification as unknown as Record<string, unknown>));
      run.verdict = verification.verdict;
      run.evidence_coverage = verification.coverage;
      events.push({ type: "verification", ...verification });
    }
    const citations = run.evidence.filter((e) => e.kind === "kb").map(evidenceItem);
    const meta = { citations, verification, tools, status: finalStatus, engine, ...(choice ? { model_choice: { raw: choice.raw, note: choice.note } } : {}), ...extra };
    const messageId = addAssistant(world, conversation, runId, answer, meta);
    run.status = finalStatus;
    run.output_text = answer;
    run.latency_ms = Math.max(1, Math.round(performance.now() - started));
    events.push({ type: "final", run_id: runId, message_id: messageId, text: answer, status: finalStatus, meta: meta as never });
    events.push(role === "customer"
      ? { type: "done", run_id: runId, status: finalStatus, latency_ms: run.latency_ms }
      : { type: "done", run_id: runId, status: finalStatus, latency_ms: run.latency_ms, prompt_tokens: 0, completion_tokens: 0, cost: 0, model: engine, route_tier: run.route_tier });
    return events;
  };

  if (asksForPerson(turnText)) {
    run.spans.push(span("router", "explicit_handoff", { text: turnText }, { rule: "asks_for_person" }));
    const call = { id: "explicit_handoff", name: "handoff_to_human", arguments: { reason: "顾客要求转人工" } };
    events.push({ type: "tool_call", ...call });
    const toolStart = performance.now();
    const outcome = runTool(world, ctx, call.name, call.arguments);
    const output = outcome.kind === "result" ? outcome.output : {};
    run.spans.push(span("tool", call.name, { arguments: call.arguments, risk: "write" }, { output }, "ok", toolStart));
    events.push({ type: "tool_result", id: call.id, name: call.name, output, is_error: false });
    noteHandoff(world, conversation, output);
    events.push(...handoffEvent(conversation));
    return finish(String(output.message ?? "已为您转接人工客服，请稍候。"), "handoff", false);
  }

  // Multi-turn: an explicit follow-up becomes a standalone question.
  let question = choice ? choice.question : turnText;
  const carried = carryOrder(turnText, previous);
  const loose = carried === null ? carryOrderLoosely(turnText, previous) : null;
  if (!choice && (carried !== null || loose)) {
    question = carried ?? loose![0];
    if (loose) ctx.unconfirmedOrder = loose[1];
    run.spans.push(span("rewrite", "order_context", { question: turnText, previous }, { standalone: question, method: "order_context" }));
    const after = frameOf(question);
    conversation.previous = question;
    conversation.frame = after;
    events.push({ type: "rewrite", standalone: question, method: "order_context" });
  } else if (!choice && isFollowup(text, previous)) {
    const current = frame ?? frameOf(previous!);
    question = ruleRewrite(turnText, current);
    run.spans.push(span("rewrite", "followup_rewrite", { question: turnText, previous }, { standalone: question, method: "rule" }));
    conversation.previous = question;
    conversation.frame = nextFrame(current, turnText, question);
    events.push({ type: "rewrite", standalone: question, method: "rule" });
  }

  // An item instead of an order number: read as that order, and a refund on it is only offered.
  if (!choice && question === turnText && role === "customer") {
    const own = world.ordersOf(tenant, email).map((o) => ({ id: o.id, items: o.items.map((i) => i.name), amount: o.amount }));
    const read = readsAsOrder(canonical(turnText), own);
    if (read) {
      question = read.question;
      ctx.unconfirmedOrder = read.unconfirmed;
      run.spans.push(span("rewrite", "item_to_order", { question: turnText }, { standalone: question, unconfirmed: read.unconfirmed }));
      conversation.previous = question;
      conversation.frame = frameOf(question);
      events.push({ type: "rewrite", standalone: question, method: "item_to_order" });
    }
  }

  if (choice) run.spans.push(span("llm", `choose:${ENGINE_MODEL}`, { question: turnText }, { raw: choice.raw, validated: choice.question, evidence: choice.evidence, note: choice.note }));

  run.spans.push(span("router", "model_router", { length: text.length, history_turns: Math.floor(history.length / 2) }, { tier: run.route_tier, model: engine, reasons: ["static demo: rules in the browser"] }));

  const { text: planned, exclude } = understood(question, history);
  const available = availableTools(ctx);
  const results: Results = {};
  const called: string[] = [];
  const maxSteps = pack.settings.max_agent_steps * 3; // several tools per step in the backend, one here

  // The model tier picked the evidence itself: the program quotes it, or falls back to the rules' search.
  if (choice?.evidence.length) {
    const output = quoteChosen(ctx, choice);
    results.search_knowledge = [output];
    called.push("search_knowledge");
    const callId = `browser_0_search_knowledge`;
    events.push({ type: "tool_call", id: callId, name: "search_knowledge", arguments: { query: output.query as string, chosen_chunks: choice.evidence } });
    run.spans.push(span("tool", "search_knowledge", { arguments: { query: output.query }, risk: "read" }, { output }));
    events.push({ type: "tool_result", id: callId, name: "search_knowledge", output, is_error: false });
  }

  for (let step = 1; step <= maxSteps; step++) {
    const call = nextCall(planned, called, results, available, tenant, exclude);
    if (!call) break;
    const [toolName, args] = call;
    const callId = `browser_${step}_${toolName}`;
    called.push(toolName);
    run.steps = step;
    events.push({ type: "tool_call", id: callId, name: toolName, arguments: args });
    const toolStart = performance.now();
    const outcome = runTool(world, ctx, toolName, args);
    if (outcome.kind === "approval") {
      const approval = fileApproval(world, ctx, run, toolName, callId, outcome.args, outcome.reason, outcome.approverRole, turnText);
      run.spans.push(span("tool", toolName, { arguments: args, risk: "high" }, { approval_required: true, reason: outcome.reason, customer_note: outcome.customerNote, arguments: outcome.args }, "ok", toolStart));
      const shown = role === "customer" ? outcome.customerNote : outcome.reason;
      events.push({ type: "approval_required", approval_id: approval.id, tool: toolName, arguments: outcome.args, reason: shown });
      return finish(pausedText(ctx, toolName, outcome.args, shown), "waiting_approval", false, { approval_id: approval.id });
    }
    (results[toolName] ??= []).push(outcome.output);
    run.spans.push(span("tool", toolName, { arguments: args, risk: toolName === "request_refund" ? "high" : "read" }, { output: outcome.output }, outcome.isError ? "rejected" : "ok", toolStart));
    events.push({ type: "tool_result", id: callId, name: toolName, output: outcome.output, is_error: outcome.isError });
    if (toolName === "handoff_to_human") noteHandoff(world, conversation, outcome.output);
    if (toolName === "withdraw_refund_request" && outcome.output.confirm_required) conversation.withdrawOffer = outcome.output;
  }

  const answer = compose(planned, results, tenant, exclude);
  // Streamed in small pieces, like the real backend's model tokens.
  for (let start = 0; start < answer.length; start += 8) events.push({ type: "token", step: Math.max(1, run.steps), text: answer.slice(start, start + 8) });
  if (results.handoff_to_human) events.push(...handoffEvent(conversation));
  return finish(answer, null, true);
}

function handoffEvent(conversation: EngineConversation): AgentEvent[] {
  return conversation.handoff ? [{ type: "handoff", ...conversation.handoff }] : [];
}

function noteHandoff(world: World, conversation: EngineConversation, output: Record<string, unknown>) {
  if (!output.queued) return;
  const ticket = world.tickets.find((t) => t.kind === "handoff" && t.conversationId === conversation.id);
  if (ticket) conversation.handoff = { ticket_id: ticket.id, status: "open", position: Number(output.queue_position ?? 1), assignee: null };
}

function quoteChosen(ctx: ToolContext, choice: ModelChoice): Record<string, unknown> {
  const scope = new Set(ctx.role === "customer" ? ["public"] : ["public", "internal"]);
  const hits = search(ctx.tenant, choice.question, scope, 12, false).filter((h) => choice.evidence.includes(h.chunk.id));
  const results = hits.map((hit) => {
    const item = addEvidence(ctx, "kb", hit.chunk.doc, hit.chunk.text, { heading: hit.chunk.heading, chunk_id: hit.chunk.id, document_id: hit.chunk.doc_id, page: null, relevance: Math.round(hit.relevance * 1000) / 1000 });
    return { ref: item.ref, doc: hit.chunk.doc, heading: hit.chunk.heading, text: hit.chunk.text, relevance: Math.max(hit.relevance, 1), page: null };
  });
  return { query: choice.question, results, chosen: true };
}

function fileApproval(world: World, ctx: ToolContext, run: RunRecord, tool: string, callId: string, args: Record<string, unknown>, reason: string, approverRole: "admin" | "agent" | undefined, question: string): ApprovalRow {
  const approval: ApprovalRow = { id: world.id(), tenant: ctx.tenant, runId: run.id, conversationId: ctx.conversationId, tool, callId, args, reason, evidence: [...ctx.evidence], status: "pending",
    requester: ctx.user, reviewer: null, question, comment: "", approverRole, createdAt: world.now(), decidedAt: null };
  world.approvals.push(approval);
  run.approvals.push({ id: approval.id, status: "pending", tool_name: tool, reason });
  return approval;
}

const LABEL: Record<string, string> = { request_refund: "退款" };

function pausedText(ctx: ToolContext, tool: string, args: Record<string, unknown>, shown: string): string {
  const custom = pack.tools[ctx.tenant]?.find((t) => t.name === tool);
  const label = custom?.label ?? LABEL[tool] ?? "操作";
  const amount = args.amount === undefined || args.amount === null ? "" : `（¥${money(Number(args.amount))}）`;
  if (ctx.role !== "customer") {
    const order = args.order_id ? `订单 ${args.order_id} 的` : "";
    return `${SIMULATED}已为顾客提交${order}${label}申请${amount}，等待另一位员工审核${shown ? `：${shown}` : ""}。审核结果会在这里显示。`;
  }
  return `${SIMULATED}您的${label}申请${amount}已提交人工审核${shown ? `：${shown}` : ""}。审核完成后会第一时间在这里通知您。`;
}

// ---------------------------------------------------------------- a person decides (app/api/approvals.py)
export function resumeApproval(world: World, approval: ApprovalRow, decision: "approve" | "reject", reviewer: { email: string; name: string; role: "admin" | "agent" }, comment: string,
  config: TenantConfig, conversation: EngineConversation | undefined, run: RunRecord | undefined): { text: string } {
  const now = world.now();
  approval.status = decision === "approve" ? "approved" : "rejected";
  approval.reviewer = reviewer.name;
  approval.comment = comment;
  approval.decidedAt = now;
  const ctx: ToolContext = { tenant: approval.tenant, config, user: approval.requester, role: pack.accounts[approval.requester]?.role ?? "customer", conversationId: approval.conversationId, runId: approval.runId,
    evidence: [...approval.evidence], customerText: approval.question, unconfirmedOrder: null, approvalId: approval.id, withdrawOffer: null };
  const label = pack.tools[approval.tenant]?.find((t) => t.name === approval.tool)?.label ?? LABEL[approval.tool] ?? "该操作";
  let text: string;
  if (decision === "approve") {
    const outcome = runTool(world, ctx, approval.tool, approval.args);
    const output = outcome.kind === "result" ? outcome.output : {};
    text = String(output.message ?? output.error ?? `${SIMULATED}您的申请已通过人工审核并处理完成。`);
  } else {
    text = `${SIMULATED}${label}申请未通过人工审核。审核意见：${comment || "无"}。${pack.texts.mock.REJECTED_NEXT_STEP}`;
  }
  if (run) {
    run.status = "completed";
    run.output_text = text;
    run.spans.push(span("approval", `human_${approval.status}`, { approval_id: approval.id }, { reviewer: reviewer.name, comment }));
    const row = run.approvals.find((a) => a.id === approval.id);
    if (row) row.status = approval.status;
  }
  if (conversation) addAssistant(world, conversation, approval.runId, text, { citations: [], verification: null, status: "completed", tools: [approval.tool], engine: ENGINE_RULES });
  return { text };
}
