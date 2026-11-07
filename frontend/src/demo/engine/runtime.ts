/**
 * One turn of the agent in the browser: app/agent/runtime.py without a model or a server. It emits the same AgentEvent
 * stream, so the console's citations, trace and approval screens work unchanged.
 */
import type { AgentEvent, EvidenceItem, SpanRow, Verification } from "../../types";
import { INJECTION, M, PII, T, W, pack, type TenantConfig } from "./pack";
import { compose, mentionsRefund, nextCall, readsAsOrder, refundRequested, theOneOrder, understood, type Results, type Turn } from "./planner";
import { ANSWERS_IN_CHINESE, DECLINE_TEXT, NEW_TICKET, NOTHING_FILED, NOT_FOR_A_PERSON, NOT_GRANTED, NO_ORDERS_HERE, NO_TICKET_LOOKUP, asksAboutTickets, asksIfHuman, asksWhatItIs, canDo, declines, holdsOff, inEnglish, isCanDo, nextSteps, onlyAcknowledges } from "./reading";
import { search } from "./retrieval";
import { addEvidence, availableTools, queueNotice, runTool, type ApprovalRow, type Evidence, type ToolContext, type World, SIMULATED } from "./tools";
import { asksForPerson, canonical, classifyReply, extractNumbers, findAll, handoffCancelled, isRefusal, money, normalizeWidth, overlapRecall, refundWithdrawRequested, splitSentences, stripCitations, sub, withdrawConfirmed } from "./text";

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
  /** The order the last reply asked whether to file a refund for: a plain yes files it, and only that. */
  refundOffer?: string | null;
  /** The last order this conversation was about, however many turns ago. */
  lastOrder?: string | null;
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
  question: string; // the turn as the rules read it
  evidence: number[]; // chunk ids the model picked
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

// What a customer's turn refers to, past the offline model's rules.
const REFERS = /它|这单|这一单|这个订单|这笔|那单|那一单|那个订单|刚才那|上面那|该订单/;
// "退款" alone names a subject; "退吧", "帮我退款" and "退款吧" ask for it.
const BARE_DEMAND = /^(?:(?:那|那就|就|好的?|好吧|行|行吧|嗯|对)[，, ]*)*(?:(?:帮我|给我|直接|麻烦|确认)(?:退款|退钱)|(?:帮我|给我|直接|麻烦|确认)?(?:退了?|退掉)|(?:退款|退钱)(?:吧|呗)|(?<=(?:好的?|行|嗯|对)[，, ]*(?:那|那就|就)?[，, ]*)退款)(?:吧|了吧|了|呗)?(?:谢谢)?[。！!~～]*$/;
const YES_TO_AN_OFFER = /^(?:嗯+|恩|ok|okay|yes|y|(?:对|嗯|是|好|行)(?:的|吧)?[，, ]*(?:就是这单)?[，, ]*(?:就)?退(?:吧|了吧)?)[。！!~～]*$/i;
const ACK_THEN_MORE = /^(?:好的|好|嗯+|行)[，, ]+(?=\S)/;
const CORRECTION = /^(?:不对|不是|错了|搞错了?|说错了?|弄错了?|我说的是|我是说|我问的是|应该是)[，,。 ]*/;
const STATUS_AGAIN = /^(?:那|现在|目前)?(?:怎么样了?|什么情况|啥情况|进度|进展|状态|什么状态|啥状态|到哪一步了?|审核了吗|审核通过了吗|通过了吗|有结果了吗|结果呢|处理了吗|处理得怎么样了?)[吗呢呀啊了？?。！!~～]*$/;
const DROPS_IT = /^(?:算了|不对|不用了|那)?[，, ]*(?:(?:不退了|不退款了|不要退了|不想退了|先不退了?|别退了|(?:帮我|给我)?撤回(?:吧|一下)?|取消退款|取消申请)[，, ]*)+[吧了。！!~～]*$/;
const ONLY_NO = /^(?:不用了?|不要了?|算了|不需要|没有了?|没了|不了|不)[，,。！!~～ ]*(?:谢谢)?[。！!~～]*$/;
const ASKED_WHICH = /要申请退款的是哪一单|要查询的是哪一单|要撤回的是哪一单/;
const CONFIRMS_REFUND = /^确认退(?:款|钱)?[。！!~～]*$/;
const BARE_NUMBER = /^\s*SO\d+\s*(?:这单|这个|那单|吧)?[。！!]?\s*$/i;

function theOrderTalkedAbout(previous: string | null, history: Turn[]): string | null {
  if (previous && W._DISOWNED.test(previous)) return null;
  const inPrevious = [...new Set(findAll(T.ORDER_ID_RE, previous ?? "").map((m) => m[0].toUpperCase()))];
  if (inPrevious.length) return inPrevious.length === 1 ? inPrevious[0] : null;
  const lastReply = [...history].reverse().find((m) => m.role === "assistant")?.content ?? "";
  const inReply = [...new Set(findAll(T.ORDER_ID_RE, lastReply).map((m) => m[0].toUpperCase()))];
  return inReply.length === 1 ? inReply[0] : null;
}

const intentOf = (previous: string | null) => (mentionsRefund(previous ?? "") ? "退款" : /到哪|物流|快递|发货|运单/.test(previous ?? "") ? "到哪了" : "查一下");

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
    const factual = !(claim.length < 6 || isCanDo(claim) || (!numbers.length && claimsNothing(claim)));
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
  const refundOffer = conversation.refundOffer ?? null;
  conversation.refundOffer = null;
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
      const notice = queueNotice(conversation.handoff.position);
      return [{ type: "handoff", ...conversation.handoff }, { type: "final", run_id: "", message_id: null, text: notice, status: "handoff", meta: { ...({ engine } as object) } },
        { type: "done", run_id: "", status: "handoff", latency_ms: 0 }];
    }
  }

  const runId = newRunId();
  const run: RunRecord = { id: runId, tenant, email, name, role, status: "running", model: engine, route_tier: choice ? "browser" : "rule", input_text: pii.masked, output_text: "", steps: 0, latency_ms: 0,
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
    const meta = { citations, verification, tools, status: finalStatus, engine, ...(choice ? { model_choice: { raw: choice.raw, note: choice.note } } : {}), ...(role === "customer" && (isRefusal(answer) || answer.startsWith(NO_ORDERS_HERE)) ? { next_steps: nextSteps(tenant) } : {}), ...extra };
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

  const asksIfItIsAPerson = role === "customer" && !choice && asksIfHuman(turnText);
  if (!asksIfItIsAPerson && asksForPerson(sub(NOT_FOR_A_PERSON, "", turnText))) {
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

  // Only punctuation: nothing to read, and a greeting would pretend otherwise.
  if (role === "customer" && !choice && !/[\p{L}\p{N}]/u.test(turnText)) return finish(`没有看懂您的问题，暂时无法回答。${canDo(tenant)}`, null, false);

  if (role === "customer" && !choice && (asksIfItIsAPerson || asksWhatItIs(turnText)) && !T.ORDER_ID_RE.test(turnText)) {
    return finish(`我是${String(config.name ?? "")}的在线客服助手。这是演示，由浏览器里的规则引擎回答，不是模型，数据都是虚构的。${canDo(tenant)}`, null, false, { next_steps: nextSteps(tenant) });
  }

  // What no message may grant, whatever else it says.
  const decline = role === "customer" && !choice ? declines(turnText, injection.length > 0) : null;
  if (decline) run.spans.push(span("guardrail", "declined", { text: turnText }, { rule: decline }));
  // With an order number the rules run on it as on any other request: an injected line changes nothing, either way.
  const preface = decline && decline !== "internal" && T.ORDER_ID_RE.test(turnText) ? NOT_GRANTED : "";
  if (decline === "internal") {
    // Customers search the public documents only; what that found is not what was asked for, so nothing is quoted from it.
    const call = { id: "browser_0_search_knowledge", name: "search_knowledge", arguments: { query: turnText } };
    events.push({ type: "tool_call", ...call });
    const outcome = runTool(world, ctx, call.name, call.arguments);
    const output = outcome.kind === "result" ? outcome.output : {};
    run.spans.push(span("tool", call.name, { arguments: call.arguments, risk: "read" }, { output }));
    events.push({ type: "tool_result", id: call.id, name: call.name, output, is_error: false });
    run.evidence.length = 0;
  }
  if (decline && !preface) return finish(DECLINE_TEXT[decline], null, false);

  if (role === "customer" && !choice && asksAboutTickets(turnText) && !T.ORDER_ID_RE.test(turnText)) return finish(NO_TICKET_LOOKUP, null, false, { next_steps: [NEW_TICKET] });

  // A yes or a no answers the last question, and files only what that question offered.
  const reply = role === "customer" && !choice && turnText.trim().length <= 8 && !/[？?]/.test(turnText) ? classifyReply(turnText) : null;
  const saysYes = reply === "yes" || (refundOffer !== null && (BARE_DEMAND.test(turnText.trim()) || YES_TO_AN_OFFER.test(turnText.trim())));
  if (reply === "no" && refundOffer) return finish(`好的，没有为订单 ${refundOffer} 提交退款申请。`, null, false);
  if (reply === "no" && offer) return finish(`好的，不撤回订单 ${String(offer.order_id)} 的退款申请。`, null, false);
  if (role === "customer" && !choice && !saysYes && reply !== "no" && onlyAcknowledges(turnText.trim())) {
    return finish(refundOffer ? `好的，还没有为订单 ${refundOffer} 提交退款申请。需要的话，回复“${refundOffer} 申请退款”。` : offer ? `好的，还没有撤回订单 ${String(offer.order_id)} 的退款申请。` : "好的。还有要查的，直接告诉我。", null, false);
  }
  if (!refundOffer && !offer && !turnText.includes("退") && (reply === "yes" || (reply === "no" && ONLY_NO.test(turnText.trim())))) return finish(reply === "yes" ? "好的。还有要查的，直接告诉我。" : "好的，有需要再告诉我。", null, false);

  // Multi-turn: an explicit follow-up becomes a standalone question.
  let question = choice ? choice.question : turnText;
  const hasNumber = T.ORDER_ID_RE.test(turnText);
  const adjacent = !choice && role === "customer" ? theOrderTalkedAbout(previous, history) : null;
  const talkedAbout = adjacent ?? (role === "customer" && !choice ? conversation.lastOrder ?? null : null);
  const own = () => world.ordersOf(tenant, email).map((o) => ({ id: o.id, items: o.items.map((i) => i.name), amount: o.amount }));
  let read: { question: string; unconfirmed: string | null; method: string } | null = null;
  if (!choice && role === "customer") {
    const correction = previous ? CORRECTION.exec(turnText.trim()) : null;
    const rest = correction ? turnText.trim().slice(correction[0].length) : "";
    // Only a correction that says nothing else: "不对 SO… 帮我退款" is a new request, "不是说七天无理由吗" a new question.
    const onlyCorrects = correction && !M._QUESTION_MARK.test(rest) && !mentionsRefund(rest) && sub(T.ORDER_ID_RE, "", rest).length <= 10;
    const corrected = onlyCorrects ? findAll(T.ORDER_ID_RE, rest)[0]?.[0].toUpperCase() ?? theOneOrder(rest, own())?.id : undefined;
    const ack = ACK_THEN_MORE.exec(turnText.trim());
    const lastReply = [...history].reverse().find((m) => m.role === "assistant")?.content ?? "";
    if (saysYes && refundOffer) {
      read = { question: `${refundOffer} 申请退款`, unconfirmed: null, method: "yes_to_offer" };
      ctx.customerText = read.question;
    }
    else if (saysYes && offer) read = { question: `确认撤回 ${String(offer.order_id)}`, unconfirmed: null, method: "yes_to_offer" };
    else if (corrected) {
      // The corrected order is read, not filed: what was asked of the wrong one has not been asked of this one yet.
      read = { question: `${corrected} ${intentOf(previous)}`, unconfirmed: corrected, method: "correction" };
    } else if (ASKED_WHICH.test(lastReply) && !hasNumber && theOneOrder(turnText, own())) {
      // Which one, answered with its name: read as that order, and a refund on it is only offered.
      const id = theOneOrder(turnText, own())!.id;
      read = { question: `${id} ${lastReply.includes("退款") ? "申请退款" : "查一下"}`, unconfirmed: id, method: "order_context" };
    } else if (BARE_NUMBER.test(turnText) && lastReply.includes("要申请退款的是哪一单")) {
      const id = findAll(T.ORDER_ID_RE, turnText)[0][0].toUpperCase();
      read = { question: `${id} ${refundRequested(previous ?? "") ? "申请退款" : "退款"}`, unconfirmed: null, method: "order_context" };
      ctx.customerText = read.question;
    } else if (!hasNumber && holdsOff(turnText.trim())) {
      // Say nothing was filed only when nothing was: a request or a refund already in this tab is answered about that one.
      if (refundOffer) return finish(`好的，没有为订单 ${refundOffer} 提交退款申请。`, null, false);
      const live = world.approvals.some((a) => a.tenant === tenant && a.tool === "request_refund" && a.status === "pending" && a.requester === email) || world.refunds.some((r) => r.tenant === tenant && r.customer === email && r.filed);
      if (!live) return finish(NOTHING_FILED, null, false);
      read = { question: talkedAbout ? `${talkedAbout} 撤回退款申请` : "我想撤回退款申请", unconfirmed: null, method: "order_context" };
      ctx.customerText = read.question;
    } else if (talkedAbout && !hasNumber && !T.OTHER_ORDER.test(turnText) && BARE_DEMAND.test(turnText.trim())) {
      // A bare "退了吧" right after an order files it; with other turns in between it is only offered.
      read = { question: `${talkedAbout} 申请退款`, unconfirmed: adjacent ? null : talkedAbout, method: "order_context" };
      if (adjacent) ctx.customerText = read.question;
    } else if (!talkedAbout && !hasNumber && CONFIRMS_REFUND.test(turnText.trim())) {
      read = { question: "我要退款", unconfirmed: null, method: "order_context" }; // which one
    } else if (talkedAbout && !hasNumber && DROPS_IT.test(turnText.trim())) {
      const waiting = world.approvals.some((a) => a.tool === "request_refund" && a.status === "pending" && a.requester === email && a.args.order_id === talkedAbout);
      read = { question: `${talkedAbout} ${waiting ? "撤回退款申请" : turnText}`, unconfirmed: null, method: "order_context" };
      if (waiting) ctx.customerText = read.question;
    } else if (adjacent && !hasNumber && reply === "yes" && turnText.includes("退")) {
      // "可以退" after an order: about that order, and offered.
      read = { question: `${adjacent} 退款`, unconfirmed: adjacent, method: "order_context" };
    } else if (ack && !hasNumber && !refundOffer && !mentionsRefund(turnText)) {
      read = { question: turnText.trim().slice(ack[0].length), unconfirmed: null, method: "order_context" };
    } else if (talkedAbout && !hasNumber && STATUS_AGAIN.test(turnText.trim())) {
      read = { question: `${talkedAbout} ${mentionsRefund(previous ?? "") || world.approvals.some((a) => a.status === "pending" && a.args.order_id === talkedAbout) ? "退款进度" : "查一下"}`, unconfirmed: null, method: "order_context" };
    } else if (talkedAbout && !hasNumber && !T.OTHER_ORDER.test(turnText) && REFERS.test(turnText)) {
      read = { question: `${talkedAbout} ${turnText}`, unconfirmed: null, method: "order_context" };
    } else if (!hasNumber && (refundWithdrawRequested(turnText) || (withdrawConfirmed(turnText) && !offer))) {
      const waiting = [...new Set(world.approvals.filter((a) => a.tenant === tenant && a.tool === "request_refund" && a.status === "pending" && a.requester === email).map((a) => String(a.args.order_id)))];
      const id = talkedAbout && waiting.includes(talkedAbout) ? talkedAbout : waiting.length === 1 ? waiting[0] : null;
      if (id) read = { question: `${id} 撤回退款申请`, unconfirmed: null, method: "order_context" };
      else if (waiting.length) return finish(`您有 ${waiting.length} 笔等待人工审核的退款申请：${waiting.join("、")}。要撤回哪一笔？请回复“订单号 撤回退款申请”。`, null, false);
      else read = { question: "我想撤回退款申请", unconfirmed: null, method: "order_context" }; // which one: never "缺少订单号"
    }
  }
  const carried = read ? null : carryOrder(turnText, previous);
  const loose = read || carried !== null ? null : carryOrderLoosely(turnText, previous);
  if (read) {
    question = read.question;
    ctx.unconfirmedOrder = read.unconfirmed;
    run.spans.push(span("rewrite", read.method, { question: turnText, previous }, { standalone: question, unconfirmed: read.unconfirmed }));
    conversation.previous = question;
    conversation.frame = frameOf(question);
    events.push({ type: "rewrite", standalone: question, method: read.method });
  } else if (!choice && (carried !== null || loose)) {
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
    const item = readsAsOrder(canonical(turnText), own());
    if (item) {
      question = item.question;
      ctx.unconfirmedOrder = item.unconfirmed;
      run.spans.push(span("rewrite", "item_to_order", { question: turnText }, { standalone: question, unconfirmed: item.unconfirmed }));
      conversation.previous = question;
      conversation.frame = frameOf(question);
      events.push({ type: "rewrite", standalone: question, method: "item_to_order" });
    }
  }

  if (choice) run.spans.push(span("llm", `choose:${ENGINE_MODEL}`, { question: turnText }, { raw: choice.raw, validated: choice.question, evidence: choice.evidence, note: choice.note }));

  run.spans.push(span("router", "model_router", { length: text.length, history_turns: Math.floor(history.length / 2) }, { tier: run.route_tier, model: engine, reasons: ["static demo: rules in the browser"] }));

  const { text: planned, exclude } = understood(question, history, previous ?? conversation.lastOrder ?? null);
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
      noteOrder(conversation, planned);
      return finish(preface + pausedText(ctx, toolName, outcome.args, shown), "waiting_approval", false, { approval_id: approval.id });
    }
    (results[toolName] ??= []).push(outcome.output);
    run.spans.push(span("tool", toolName, { arguments: args, risk: toolName === "request_refund" ? "high" : "read" }, { output: outcome.output }, outcome.isError ? "rejected" : "ok", toolStart));
    events.push({ type: "tool_result", id: callId, name: toolName, output: outcome.output, is_error: outcome.isError });
    if (toolName === "handoff_to_human") noteHandoff(world, conversation, outcome.output);
    if (toolName === "withdraw_refund_request" && outcome.output.confirm_required) conversation.withdrawOffer = outcome.output;
  }

  const composed = compose(planned, results, tenant, exclude);
  // English nobody here can answer gets an honest reply in English, and examples it can answer: not a Chinese apology.
  const asksWhich = results.list_my_orders && !results.get_order;
  const answer = preface + (role === "customer" && inEnglish(turnText) && (isRefusal(composed) || asksWhich) ? ANSWERS_IN_CHINESE : composed);
  const offeredFor = results.request_refund?.at(-1)?.confirm_order;
  if (offeredFor) conversation.refundOffer = String(offeredFor);
  noteOrder(conversation, planned);
  // Streamed in small pieces, like the real backend's model tokens.
  for (let start = 0; start < answer.length; start += 8) events.push({ type: "token", step: Math.max(1, run.steps), text: answer.slice(start, start + 8) });
  if (results.handoff_to_human) events.push(...handoffEvent(conversation));
  return finish(answer, null, true);
}

function noteOrder(conversation: EngineConversation, question: string) {
  const named = [...new Set(findAll(T.ORDER_ID_RE, question).map((m) => m[0].toUpperCase()))];
  if (named.length === 1) conversation.lastOrder = named[0];
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
const REVIEW_TIME = /人工审核时效为[^，。；]+/;

/** How long a person takes, in the tenant's own words and with its source. */
function reviewTime(ctx: ToolContext): string {
  const hit = search(ctx.tenant, "退款 人工审核时效", new Set(["public"]), 4).find((h) => REVIEW_TIME.test(h.chunk.text));
  if (!hit) return "";
  const item = addEvidence(ctx, "kb", hit.chunk.doc, hit.chunk.text, { heading: hit.chunk.heading, chunk_id: hit.chunk.id, document_id: hit.chunk.doc_id, page: null, relevance: Math.round(hit.relevance * 1000) / 1000 });
  return `${REVIEW_TIME.exec(hit.chunk.text)![0]}[${item.ref}]。`;
}

function pausedText(ctx: ToolContext, tool: string, args: Record<string, unknown>, shown: string): string {
  const custom = pack.tools[ctx.tenant]?.find((t) => t.name === tool);
  const label = custom?.label ?? LABEL[tool] ?? "操作";
  const amount = args.amount === undefined || args.amount === null ? "" : `（¥${money(Number(args.amount))}）`;
  if (ctx.role !== "customer") {
    const order = args.order_id ? `订单 ${args.order_id} 的` : "";
    return `${SIMULATED}已为顾客提交${order}${label}申请${amount}，等待另一位员工审核${shown ? `：${shown}` : ""}。审核结果会在这里显示。`;
  }
  const whose = args.order_id ? `订单 ${args.order_id} 的` : "您的";
  return `${SIMULATED}${whose}${label}申请${amount}已提交人工审核${shown ? `：${shown}` : ""}。${tool === "request_refund" ? reviewTime(ctx) : ""}审核完成后会第一时间在这里通知您。`;
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
