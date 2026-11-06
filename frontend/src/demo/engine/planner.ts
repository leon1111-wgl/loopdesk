/**
 * What to do with a message and how to say the result: app/llm/mock.py, the backend's offline model, on the backend's
 * own expressions (pack.ts). It has no language understanding. It reads rules, calls tools one at a time, and composes
 * the answer from what they returned, so every fact in an answer comes from a tool or a quoted knowledge chunk.
 */
import { M, T, pack, type TenantTool } from "./pack";
import { asksHowLong, canDo, conditionalRefund, plainRequest } from "./reading";
import { answerSentences } from "./retrieval";
import { tenantTools } from "./tools";
import {
  cancelRequested, expandQuery, findAll, fixTypos, fullMatch, handoffCancelled, money, normalizeWidth, otherQuestion, refundBareDecline,
  refundRequested as requestedByTheRules, refundWithdrawRequested, splitSentences, statedRefundAmount, sub, tokenSet, withdrawConfirmed,
} from "./text";

// The words of every item on sale here: what a clause may name and still be part of a plain request.
const ITEM_WORDS = new Set(Object.values(pack.orders).flat().flatMap((order) => order.items.flatMap((item) => {
  const han = item.name.replace(/[^一-鿿]/g, "");
  return [...han].slice(0, -1).map((_, i) => han.slice(i, i + 2));
})));

/** What counts as a request on this page: the rules' reading, no condition, and nothing in the message that is not part of one. */
export const isPlainRequest = (text: string): boolean => plainRequest(text, T.ORDER_ID_RE, ITEM_WORDS);

// "SO… 我要退": a bare 退 the rules leave out, in a message that is nothing but a request for it.
const SAYS_WHOSE = /(?:我要|帮我|给我|请|麻烦|要|想|赶紧|立刻|现在就)退/;

export const refundRequested = (text: string): boolean => !conditionalRefund(text) && isPlainRequest(text)
  && (requestedByTheRules(text) || (T.ORDER_ID_RE.test(text) && SAYS_WHOSE.test(sub(T.ORDER_ID_RE, " ", text)) && !T.NEGATED_REFUND_RE.test(text) && !refundWithdrawRequested(text)));

type Output = Record<string, any>; // eslint-disable-line @typescript-eslint/no-explicit-any
export type Results = Record<string, Output[]>;
export type Call = [string, Record<string, unknown>];
export interface Turn {
  role: "user" | "assistant";
  content: string;
}

const X = pack.texts.mock;
const ORDINAL_VALUE: Record<string, number> = { 一: 1, 二: 2, 三: 3, 四: 4, 五: 5, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5 };
const FAULTS_AT_ONCE = 3;
const BUILTIN = new Set(["search_knowledge", "get_order", "list_my_orders", "track_shipment", "request_refund", "withdraw_refund_request", "create_ticket", "handoff_to_human"]);

const strip = (text: string, chars: string) => {
  let start = 0, end = text.length;
  while (start < end && chars.includes(text[start])) start++;
  while (end > start && chars.includes(text[end - 1])) end--;
  return text.slice(start, end);
};

const upperIds = (text: string) => [...new Set(findAll(T.ORDER_ID_RE, text).map((m) => m[0].toUpperCase()))];

function thenRefundTiming(text: string): [string, string] | null {
  for (const cut of findAll(M._CUT, text)) {
    const head = text.slice(0, cut.index), rest = text.slice(cut.index + cut[0].length);
    if (M._SHIPMENT.test(head) && !M._REFUND.test(head) && M._REFUND.test(rest) && M._TIMING.test(rest) && !T.ORDER_ID_RE.test(rest) && !refundRequested(text)) {
      return [head.trim(), rest.trim()];
    }
  }
  return null;
}

const ASKS_SOMETHING = /吗|呢|多少|多久|几天|怎么|如何|能不能|可不可以|可以|咋|在哪|谁/;
const MY_POINTS = /积分(?:还有|还剩|余额|一共|有)多少|有多少积分|我的积分|查积分/;
const ARRIVAL = /(?:什么时候|多久|几天|啥时候|何时)(?:能|才能)?(?:到|送到|收到)(?!账)/;
const HAS_IT_COME = /到了吗|到了没|到没到|收到了吗/;
const WHO_SIGNED = /谁签|谁收|签收了吗|签收没|有人签|有人收/;
// The money coming back ("这笔钱什么时候到") is not the parcel coming.
const wantsTracking = (text: string) => !/钱|款/.test(text) && (M._SHIPMENT.test(text) || ARRIVAL.test(text) || WHO_SIGNED.test(text) || HAS_IT_COME.test(text));
const OUTAGE = /彻底|完全|全部|整个/;

/** "SO… 到哪了 能开发票吗": a question about the order, and one that is not about it. */
function secondQuestion(text: string): [string, string] | null {
  if (!T.ORDER_ID_RE.test(text) && !MY_POINTS.test(text)) return null;
  const clauses = sub(T.ORDER_ID_RE, " ", text).split(/[，,。！!？?；;~～\s]+/).filter(Boolean);
  const other = clauses.slice(1).find((c) => c.length >= 4 && ASKS_SOMETHING.test(c) && !mentionsRefund(c) && !wantsTracking(c) && !M._RUSH.test(c) && !M._FAULTY.test(c) && !M._CHASE.test(c));
  if (!other) return null;
  const main = strip(text.replace(other, " "), " ，,。？?！!；;　");
  // Never when taking the clause away changes what the rest asks for: "如果我申请退款，下载的歌还能听吗" is one question.
  return refundRequested(main) === refundRequested(text) ? [main, other] : null;
}

export function splitIntents(text: string): [string, string | null] {
  const match = M._EXTRA.exec(text);
  if (!match) {
    const other = otherQuestion(text);
    if (other && text.includes(other)) return [strip(text.replace(other, " "), " ，,。？?！!；;　"), other];
    return thenRefundTiming(text) ?? secondQuestion(text) ?? [text, null];
  }
  const extra = text.slice(match.index + match[0].length).trim();
  if (extra.length < 2 || T.ORDER_ID_RE.test(extra)) return [text, null];
  return [text.slice(0, match.index).trim(), extra];
}

// Beyond the backend's refund words, for a model it has and this page has not: "能退吗", "就帮我退了吧", "退不退".
const LOOSE_REFUND = /退(?:了|吧|吗|么|嘛|不退)|(?:能|可以|可|要|想|帮我|给我)退(?!订|出|群|役|回)/;

export const mentionsRefund = (text: string): boolean => M._REFUND.test(text) || refundRequested(text) || LOOSE_REFUND.test(text);

export const asksRefundStatus = (text: string): boolean =>
  (mentionsRefund(text) || text.includes("到账") || M._MY_MONEY.test(text)) && M._PROGRESS.test(text) && !M._APPLY.test(text);

export function refundQuestion(text: string): string {
  let question = sub(M._ASKING_WORDS, " ", sub(T.ORDER_ID_RE, " ", sub(T.NEGATED_REFUND_RE, " ", text)));
  question = sub(M._FILLER, " ", question);
  question = strip(question.replace(/\s+/g, " "), " ，,。！!？?:：");
  if (question && !/[一-鿿]/.test(question)) return M._EN_TIMING.test(question) ? "退款 到账 工作日" : "七天无理由退货 质量问题 退换";
  return question || "退款 到账 工作日";
}

export const faultReport = (text: string): boolean => M._FAULTY.test(text) && M._OWN.test(text) && !M._SELF_DAMAGE.test(text);

const thing = (words: string) => sub(/[^\p{L}\p{N}_]/u, "", sub(M._NOT_A_THING, "", sub(T.ORDER_ID_RE, "", words)));

function namesItem(text: string, name: string): boolean {
  const han = name.replace(/[^一-鿿]/g, "");
  for (let i = 0; i + 1 < han.length; i++) if (text.includes(han.slice(i, i + 2))) return true;
  return false;
}

function severalFaults(text: string): string[] {
  if (!(M._OWN.test(text) || M._REPORTED.test(text) || T.ORDER_ID_RE.test(text)) || mentionsRefund(text) || wantsHuman(text)) return [];
  const clauses = text.split(M._FAULT_SEP);
  const damaged = clauses.filter((clause) => M._SELF_DAMAGE.test(clause));
  if (damaged.some((clause) => thing(clause.slice(0, M._SELF_DAMAGE.exec(clause)!.index)).length < 2)) return [];
  const faults: string[] = [];
  let before = "";
  for (let clause of clauses) {
    const fault = M._FAULTY.exec(clause);
    if (!fault) {
      before = clause;
      continue;
    }
    let what = thing(clause.slice(0, fault.index));
    if (what.length < 2 && before) {
      clause = `${before} ${clause}`;
      what = thing(before);
    }
    before = "";
    if (M._NOT_FAULTY.test(clause) || M._FAULT_QUESTION.test(clause) || M._SELF_DAMAGE.test(clause)) continue;
    if ((T.ORDER_ID_RE.test(clause) || (what.length >= 2 && !M._A_PART.test(what))) && !damaged.some((other) => namesItem(other, what))) faults.push(clause);
  }
  if (faults.length < (damaged.length ? 1 : 2)) return [];
  return faults.length > FAULTS_AT_ONCE ? [...faults.slice(0, FAULTS_AT_ONCE - 1), faults.slice(FAULTS_AT_ONCE - 1).join("；")] : faults;
}

function faultIntents(text: string): [string[], string, string | null] {
  const [main, extra] = splitIntents(text);
  let faults = severalFaults(main);
  if (faults.length) return [faults, main, extra];
  faults = severalFaults(text);
  if (faults.length) return [faults, text, null];
  return [[], main, extra];
}

export function aboutMyOrder(text: string): boolean {
  if (T.ORDER_ID_RE.test(text)) return false;
  if (refundWithdrawRequested(text)) return true;
  if (M._LIST_MINE.test(text) || T.OTHER_ORDER.test(text)) return true;
  if (asksRefundStatus(text)) return M._OWN.test(text) || text.includes("单子") || M._MY_MONEY.test(text);
  if (mentionsRefund(text)) {
    const kept = sub(T.NEGATED_REFUND_RE, "，", text);
    return M._APPLY.test(kept) || (M._OWN.test(text) && !M._ASKING.test(text.trim()));
  }
  if (faultReport(text)) return true;
  return M._SHIPMENT.test(text) && M._MINE.test(text);
}

function orderClauses(text: string): Record<string, string> {
  const spans = findAll(T.ORDER_ID_RE, text).map((m) => [m.index, m[0].toUpperCase()] as const);
  return Object.fromEntries(spans.map(([start, id], i) => [id, text.slice(start, i + 1 < spans.length ? spans[i + 1][0] : text.length)]));
}

// What the order's own record answers.
const ORDER_FACTS = /多少钱|金额|买的(?:什么|啥)|什么商品|啥东西|状态|怎么样|情况|进度/;

export function orderQuestion(text: string): string {
  const rest = sub(T.ORDER_ID_RE, " ", text);
  if (!M._QUESTION_MARK.test(rest) || ORDER_FACTS.test(rest)) return "";
  const bare = sub(M._LOOKUP_WORDS, "", rest.replace(/[\s，,。.！!？?~～]+/g, ""));
  return bare.length >= 3 ? strip(rest, " ，,") : "";
}

function namedOrders(text: string, listing: Output, exclude: Set<string>): Output[] {
  return (listing.orders ?? []).filter((o: Output) => !exclude.has(o.order_id) && (o.items ?? []).some((n: string) => namesItem(text, n)));
}

const THROUGH_APPLE = /苹果|iphone|ipad|ios|app\s*store/i;

/** How much of a product's name the message says: "豪华年卡" fits the yearly plan better than the monthly one. */
function nameFit(text: string, name: string): number {
  const han = name.replace(/[^一-鿿]/g, "");
  let fit = 0;
  for (let i = 0; i + 1 < han.length; i++) if (text.includes(han.slice(i, i + 2))) fit += 1;
  return fit + (THROUGH_APPLE.test(text) && name.includes("App Store") ? 2 : 0);
}

/** The orders that fit a message best, when one of its words names a product at all. */
function bestNamed<O>(text: string, orders: O[], names: (order: O) => string[]): O[] {
  const fits = orders.map((o) => Math.max(0, ...names(o).map((n) => nameFit(text, n))));
  const best = Math.max(0, ...fits);
  return best ? orders.filter((_, i) => fits[i] === best) : [];
}

function faultTickets(text: string, faults: string[], listing: Output, lookedUp: Output[], exclude: Set<string>): Output[] {
  const known = new Map<string, Output>([...(listing.orders ?? []), ...lookedUp].map((o: Output) => [o.order_id, o]));
  const tickets: Output[] = [];
  const byOrder = new Map<string, Output>();
  for (const clause of faults) {
    const number = T.ORDER_ID_RE.exec(clause);
    let order: Output | null | undefined;
    if (number) {
      order = known.get(number[0].toUpperCase());
      if (!order || order.error) continue;
    } else {
      const arrived = namedOrders(clause, listing, exclude).filter((o) => o.status === "delivered");
      order = arrived.length === 1 ? arrived[0] : null;
    }
    let title = clause.slice(0, 30);
    if (order) {
      const held = byOrder.get(order.order_id);
      if (held) {
        held.description += `；${clause}`;
        continue;
      }
      const items = (order.items ?? []).map((i: Output | string) => (typeof i === "string" ? i : i.name ?? "")).join("、");
      title = `${order.order_id} ${items} 换货/维修`.replace("  ", " ").slice(0, 60);
    }
    const ticket = { title, description: clause, priority: M._URGENT.test(text) ? "high" : "normal" };
    tickets.push(ticket);
    if (order) byOrder.set(order.order_id, ticket);
  }
  return tickets;
}

function pickMyOrder(text: string, listing: Output, exclude: Set<string>): string | null {
  const orders: Output[] = (listing.orders ?? []).filter((o: Output) => !exclude.has(o.order_id));
  const named = namedOrders(text, listing, exclude);
  let candidates: Output[];
  if (named.length) candidates = named;
  else if (asksRefundStatus(text)) candidates = orders.filter((o) => o.status === "refunding");
  else if (M._SHIPMENT.test(text) && !mentionsRefund(text)) candidates = orders.filter((o) => o.status === "shipped");
  else candidates = [];
  return candidates.length === 1 ? candidates[0].order_id : null;
}

// "人工审核要多久" asks about a review, not for a person.
export const wantsHuman = (text: string): boolean => M._HANDOFF.test(sub(/人工(?:审核|核实|审批)/, "", sub(M._IF_COMPLAIN, "", sub(M._NEGATED_HANDOFF, "", text))));

function chasing(text: string): boolean {
  const rest = sub(T.ORDER_ID_RE, "", text).replace(/[\s，,。.！!？?~～]+/g, "");
  return M._CHASE.test(text) || fullMatch(M._CHASE_SHORT, rest);
}

export function smalltalk(text: string): string | null {
  const stripped = text.trim();
  if (!stripped || fullMatch(M._GREETING, stripped)) return X.GREETING_TEXT;
  const [cancelled, rest] = handoffCancelled(stripped);
  if (cancelled && !rest) return X.NO_HANDOFF_TEXT;
  return fullMatch(M._THANKS, stripped) ? X.THANKS_TEXT : null;
}

function resolveOrdinal(text: string, history: Turn[]): string | null {
  const match = new RegExp(`^(?:${M._ORDINAL.source})$`).exec(text.trim());
  if (!match) return null;
  for (let i = history.length - 1; i >= 0; i--) {
    if (history[i].role !== "assistant" || !history[i].content.includes(X.LISTING_MARK)) continue;
    const listed = upperIds(history[i].content.split(X.LISTING_MARK)[1].split("。")[0]);
    const n = match[2] ? listed.length : ORDINAL_VALUE[match[1]];
    if (!(n >= 1 && n <= listed.length)) return null;
    const asked = [...history.slice(0, i)].reverse().find((m) => m.role === "user")?.content ?? "";
    let intent: string;
    if (refundWithdrawRequested(asked)) intent = "撤回退款申请";
    else if (asksRefundStatus(asked)) intent = "退款进度";
    else if (mentionsRefund(asked)) intent = "退款";
    else if (M._SHIPMENT.test(asked)) intent = "到哪了";
    else intent = "查一下";
    return `${listed[n - 1]} ${intent}`;
  }
  return null;
}

function earlierOrders(history: Turn[]): Set<string> {
  return new Set(history.filter((m) => m.role === "user").flatMap((m) => findAll(T.ORDER_ID_RE, m.content).map((x) => x[0].toUpperCase())));
}

// ---------------------------------------------------------------- tenant-declared tools
const matches = (text: string, tool: TenantTool) => {
  const norm = normalizeWidth(text).toLowerCase();
  return (tool.hints?.intents ?? []).filter((phrase) => norm.includes(phrase.toLowerCase())).length;
};

function bestTool(text: string, custom: TenantTool[]): TenantTool | null {
  const scored = custom.map((tool, i) => ({ score: matches(text, tool), i, tool })).filter((s) => s.score > 0);
  if (!scored.length) return null;
  const best = scored.reduce((a, b) => (b.score > a.score ? b : a)).tool;
  if ((best.risk === "write" || best.risk === "money") && M._HYPOTHETICAL.test(text.trim())) return null;
  return best;
}

function fill(tool: TenantTool, text: string): [Record<string, unknown>, string | null] {
  const props = tool.parameters.properties ?? {};
  const required = tool.parameters.required ?? [];
  const norm = normalizeWidth(text);
  const args: Record<string, unknown> = {};
  for (const [name, prop] of Object.entries(props)) {
    let value: unknown = null;
    if (prop.type === "number" || prop.type === "integer") {
      const found = M._CURRENCY_AMOUNT.exec(norm);
      if (found && (name === "amount" || name === "value")) value = Number(found[1]);
    }
    if (value === null && "default" in prop) continue;
    if (value === null) {
      if (required.includes(name)) return [args, prop.description ?? name];
      continue;
    }
    args[name] = value;
  }
  return [args, null];
}

interface CustomPlan {
  call: Call | null;
  missing: [string, string] | null;
}

function planCustom(text: string, custom: TenantTool[], called: string[]): CustomPlan | null {
  const tool = bestTool(text, custom);
  if (!tool) return null;
  if (called.includes(tool.name)) return { call: null, missing: null };
  const [args, missing] = fill(tool, text);
  if (missing) return { call: null, missing: [tool.name, missing] };
  if ("reason" in (tool.parameters.properties ?? {}) && !("reason" in args)) args.reason = text.slice(0, 120);
  return { call: [tool.name, args], missing: null };
}

function composeCustom(text: string, results: Results, custom: TenantTool[]): string[] {
  const parts: string[] = [];
  const tool = bestTool(text, custom);
  if (tool) {
    const plan = planCustom(text, custom, custom.filter((t) => t.name in results).map((t) => t.name));
    if (plan?.missing && !(plan.missing[0] in results)) return [`请提供${plan.missing[1]}，我来为您办理${tool.label}。`];
  }
  for (const spec of custom) {
    for (const output of results[spec.name] ?? []) {
      if (output.error) parts.push(`${spec.label}未能办理：${output.error}。`);
      else if (output.status === "rejected") {
        parts.push(String(output.message ?? `${spec.label}未通过人工审核。`));
        parts.push(X.REJECTED_NEXT_STEP);
      } else if (output.message) parts.push(String(output.message));
    }
  }
  return parts;
}

// ---------------------------------------------------------------- planning
const ABOUT_REFUNDS = /退|到账|原路/;
const OTHER_SUBJECT = /自动续费|续费|续订|订阅|积分|发票|密码|账号/;
const asksBeyondRefunds = (text: string) => text.split(/[，,。！!？?；;~～\s]+/).some((c) => M._QUESTION_MARK.test(c) && !/退/.test(c));

function mainCall(text: string, called: string[], results: Results, available: Set<string>, exclude: Set<string>): Call | null {
  const can = (name: string) => available.has(name) && !called.includes(name);
  let orderIds = upperIds(text);
  if (wantsHuman(text) && can("handoff_to_human")) return ["handoff_to_human", { reason: text.slice(0, 120) }];

  if (orderIds.length > 1) {
    const orders = results.get_order ?? [];
    if (orders.length < orderIds.length && available.has("get_order")) return ["get_order", { order_id: orderIds[orders.length] }];
    const found = orders.filter((o) => !o.error).map((o) => o.order_id as string);
    const tracked = new Set((results.track_shipment ?? []).map((t) => t.order_id));
    const clauses = orderClauses(text);
    for (const orderId of found) {
      let clause = clauses[orderId] ?? text;
      if (!(M._SHIPMENT.test(clause) || mentionsRefund(clause))) clause = text;
      if (!tracked.has(orderId) && M._SHIPMENT.test(clause) && !mentionsRefund(clause) && available.has("track_shipment")) return ["track_shipment", { order_id: orderId }];
    }
    const asking = Object.values(clauses).filter((c) => mentionsRefund(c) && !refundRequested(c));
    if (asking.length && can("search_knowledge") && !refundRequested(text)) return ["search_knowledge", { query: refundQuestion(asking[0]) }];
    return null;
  }

  if (!orderIds.length && withdrawConfirmed(text)) return can("withdraw_refund_request") ? ["withdraw_refund_request", {}] : null;

  if (aboutMyOrder(text) && available.has("list_my_orders")) {
    if (can("list_my_orders")) return ["list_my_orders", {}];
    const listing = (results.list_my_orders ?? [{}]).at(-1)!;
    const fault = faultReport(text) && !refundRequested(text);
    let picked: string | null;
    if (fault) {
      const arrived = namedOrders(text, listing, exclude).filter((o) => o.status === "delivered");
      picked = arrived.length === 1 ? arrived[0].order_id : null;
    } else {
      picked = (refundRequested(text) && !asksRefundStatus(text)) || refundWithdrawRequested(text) ? null : pickMyOrder(text, listing, exclude);
    }
    if (picked === null && fault && namedOrders(text, listing, exclude).length <= 1) {
      // the item is on none of these orders, or not delivered: a ticket without one
    } else if (picked === null) {
      return null;
    } else {
      orderIds = [picked];
    }
  }

  if (orderIds.length) {
    const orderId = orderIds[0];
    if (can("get_order")) return ["get_order", { order_id: orderId }];
    const order = (results.get_order ?? [{}]).at(-1)!;
    if (order.error) return null;
    if (refundWithdrawRequested(text) || withdrawConfirmed(text) || (order.refund_awaiting_approval && refundBareDecline(text))) {
      return can("withdraw_refund_request") ? ["withdraw_refund_request", { order_id: orderId }] : null;
    }
    if (asksRefundStatus(text)) {
      const live = order.refund_awaiting_approval || order.status === "refunded" || (order.refunds ?? []).some((r: Output) => r.status === "pending" || r.status === "processing");
      if (live && can("search_knowledge")) return ["search_knowledge", { query: "退款 到账 工作日" }];
      return null;
    }
    if (M._CANCEL.test(text) && !mentionsRefund(text)) {
      if (cancelRequested(text) && isPlainRequest(text) && order.status === "paid" && order.refundable && can("request_refund")) {
        return ["request_refund", { order_id: orderId, amount: Number(order.amount ?? 0), reason: `顾客要求取消未发货订单：${text.slice(0, 100)}` }];
      }
      return null;
    }
    const reported = !asksHowLong(text) && ((M._TICKET.test(text) && (M._REPORTED.test(text) || !M._ASKING.test(text.trim()))) || (!T.ORDER_ID_RE.test(text) && faultReport(text))
      || (M._FAULTY.test(text) && !M._NOT_FAULTY.test(text) && !M._SELF_DAMAGE.test(text) && !M._FAULT_QUESTION.test(text) && !mentionsRefund(text)));
    const repair = M._REPAIR.test(text) && (M._FAULTY.test(text) || !M._ASKING.test(text.trim()));
    if ((repair || reported) && !refundRequested(text)) {
      if (can("search_knowledge")) return ["search_knowledge", { query: "质量问题 退货 换货" }];
      if (can("create_ticket")) {
        const items = (order.items ?? []).map((i: Output) => i.name ?? "").join("、");
        return ["create_ticket", { title: `${orderId} ${items} 换货/维修`.replace("  ", " ").slice(0, 60), description: text, priority: M._URGENT.test(text) ? "high" : "normal" }];
      }
      return null;
    }
    if (M._SELF_DAMAGE.test(text)) return can("search_knowledge") ? ["search_knowledge", { query: X.SELF_DAMAGE_QUERY }] : null;
    if (mentionsRefund(text)) {
      if (!order.refundable) return null;
      if (!refundRequested(text)) return can("search_knowledge") ? ["search_knowledge", { query: refundQuestion(text) }] : null;
      if (can("search_knowledge")) return ["search_knowledge", { query: "退款 到账 工作日" }];
      if (can("request_refund")) {
        const stated = statedRefundAmount(text);
        const amountMatch = stated !== null ? null : M._AMOUNT.exec(sub(T.NEGATED_REFUND_RE, "，", text));
        const amount = stated !== null ? stated
          : amountMatch ? Number(/\d+(?:\.\d+)?/.exec(amountMatch[0])![0])
          : M._HALF.test(text) ? Math.round((Number(order.amount ?? 0) / 2) * 100) / 100
          : Number(order.amount ?? 0);
        return ["request_refund", { order_id: orderId, amount, reason: text.slice(0, 120) }];
      }
      return null;
    }
    if (M._NOT_RECEIVED.test(text)) {
      if (can("track_shipment")) return ["track_shipment", { order_id: orderId }];
      if (can("create_ticket")) return ["create_ticket", { title: `${orderId} 显示签收但未收到`, description: text, priority: "high" }];
      return null;
    }
    if (M._CHANGE_INFO.test(text)) {
      if (can("create_ticket")) return ["create_ticket", { title: `${orderId} 修改收货信息`, description: text, priority: "normal" }];
      return null;
    }
    if ((wantsTracking(text) || chasing(text)) && order.status !== "paid") {
      if (can("track_shipment")) return ["track_shipment", { order_id: orderId }];
      const shipment = (results.track_shipment ?? [{}]).at(-1)!;
      if ((M._ETA.test(text) || ARRIVAL.test(text)) && shipment.shipped && !shipment.delivered && can("search_knowledge")) return ["search_knowledge", { query: "发货后 预计 送达" }];
      return null;
    }
    if (wantsTracking(text) && can("track_shipment")) return ["track_shipment", { order_id: orderId }];
    if (M._RUSH.test(text) && can("search_knowledge")) return ["search_knowledge", { query: "发货时效 付款后 小时" }];
    const rest = orderQuestion(text);
    if (rest && can("search_knowledge")) return ["search_knowledge", { query: rest }];
    return null;
  }

  if (M._SELF_DAMAGE.test(text)) return can("search_knowledge") ? ["search_knowledge", { query: X.SELF_DAMAGE_QUERY }] : null;

  if (!asksHowLong(text) && ((M._TICKET.test(text) && (M._REPORTED.test(text) || !M._ASKING.test(text.trim()))) || (faultReport(text) && !refundRequested(text)))) {
    if (can("search_knowledge")) return ["search_knowledge", { query: text }];
    if (can("create_ticket")) return ["create_ticket", { title: text.slice(0, 30), description: text, priority: M._URGENT.test(text) || OUTAGE.test(text) ? "high" : "normal" }];
    return null;
  }

  if (!called.length && can("search_knowledge")) return ["search_knowledge", { query: text }];
  return null;
}

function faultCall(text: string, faults: string[], called: string[], results: Results, available: Set<string>, exclude: Set<string>, policy: boolean): Call | null {
  if (available.has("list_my_orders") && !called.includes("list_my_orders")) return ["list_my_orders", {}];
  const listing = (results.list_my_orders ?? [{}]).at(-1)!;
  const listed = new Set((listing.orders ?? []).map((o: Output) => o.order_id));
  const unlisted = faults.map((c) => T.ORDER_ID_RE.exec(c)?.[0].toUpperCase()).filter((id): id is string => Boolean(id) && !listed.has(id));
  const lookedUp = results.get_order ?? [];
  if (lookedUp.length < unlisted.length && available.has("get_order")) return ["get_order", { order_id: unlisted[lookedUp.length] }];
  if (policy && available.has("search_knowledge") && !called.includes("search_knowledge")) {
    return ["search_knowledge", { query: M._SELF_DAMAGE.test(text) ? X.SELF_DAMAGE_QUERY : "质量问题 退货 换货" }];
  }
  const tickets = faultTickets(text, faults, listing, lookedUp, exclude);
  const made = (results.create_ticket ?? []).length;
  return made < tickets.length ? ["create_ticket", tickets[made]] : null;
}

export function nextCall(text: string, called: string[], results: Results, available: Set<string>, tenant: string, exclude: Set<string>): Call | null {
  if (smalltalk(text)) return null;
  const [faults, main, extra] = available.has("create_ticket") ? faultIntents(text) : ([[], ...splitIntents(text)] as [string[], string, string | null]);
  if (wantsHuman(main) && available.has("handoff_to_human") && !called.includes("handoff_to_human")) return ["handoff_to_human", { reason: main.slice(0, 120) }];
  const plan = planCustom(main, tenantTools(tenant), called);
  if (plan && (plan.call || plan.missing || !extra)) return plan.call;
  const call = plan ? null : faults.length ? faultCall(main, faults, called, results, available, exclude, !extra) : mainCall(main, called, results, available, exclude);
  if (call) return call;
  const searched = new Set((results.search_knowledge ?? []).map((r) => r.query));
  if (extra && available.has("search_knowledge") && !searched.has(extra)) return ["search_knowledge", { query: extra }];
  return null;
}

// ---------------------------------------------------------------- answering
function askWhich(text: string, listing: Output): string {
  if (listing.error) return `暂时查不到您的订单：${listing.error}。也可以直接告诉我订单号。`;
  const every: Output[] = listing.orders ?? [];
  if (!every.length) return "没有查到您账号下的订单。如果是用其他账号下的单，请告诉我订单号。";
  const fitting = bestNamed(text, every, (o) => o.items ?? []);
  const orders = fitting.length > 1 ? fitting : every;
  const lines = orders.map((o) => `${o.order_id}（${(o.items ?? []).join("、")}，${o.status_text}，¥${money(o.amount)}）`).join("；");
  if (refundWithdrawRequested(text)) return `您账号下最近的订单有：${lines}。请问要撤回的是哪一单的退款申请？请回复“订单号 撤回退款申请”，例如“${orders[0].order_id} 撤回退款申请”。`;
  if (mentionsRefund(text) && !asksRefundStatus(text)) return `您账号下最近的订单有：${lines}。请问要申请退款的是哪一单？请回复“订单号 申请退款”，例如“${orders[0].order_id} 申请退款”。`;
  if (asksRefundStatus(text) && !orders.some((o) => o.status === "refunding")) return `没有查到您账号下正在处理中的退款。最近的订单有：${lines}。如果问的是其中一单的退款，回复订单号即可帮您查。`;
  if (M._FAULTY.test(text)) return `您账号下最近的订单有：${lines}。请问出问题的是哪一单？回复订单号即可帮您处理。`;
  if (orders.length === 1) return `您账号下最近的订单有：${lines}。如需查询这一单，回复订单号即可。`;
  return `您账号下最近的订单有：${lines}。请问要查询的是哪一单？回复订单号即可。`;
}

function refundProgressText(order: Output): string {
  const oid = order.order_id;
  const lines: string[] = [];
  const waiting = order.refund_awaiting_approval;
  if (waiting) lines.push(`订单 ${oid} 有一笔 ¥${money(waiting.amount)} 的退款申请（${waiting.filed_on} 提交）正在等待人工审核，审核通过后会按原支付方式退回。`);
  for (const r of (order.refunds ?? []).slice(0, 3)) lines.push(`订单 ${oid} 的退款单 #${r.refund_id}（¥${money(r.amount)}，${r.filed_on} 提交）${r.status_text}。`);
  if (!lines.length && order.status === "refunded") lines.push(`订单 ${oid} 当前状态为“已退款”。如超过到账时效仍未收到，可以回复“转人工”帮您核查。`);
  if (!lines.length) {
    lines.push(`订单 ${oid} 当前状态为“${order.status_text}”，没有查到在线提交的退款记录。`);
    if (order.refundable) lines.push(`如需退款，可以直接回复“${oid} 申请退款”。`);
  }
  return lines.join("");
}

/** The sentences that best cover the query, only from strongly relevant hits. */
export function extract(query: string, hits: Output[], limit: number): string[] {
  const queryTokens = tokenSet(query);
  if (!hits.length || !queryTokens.size) return [];
  const top = Math.max(...hits.map((h) => Number(h.relevance ?? 0)));
  let scored: { score: number; ref: number; pos: number; sentence: string }[] = [];
  hits.forEach((hit, rank) => {
    if (Number(hit.relevance ?? 0) < 0.6 * top) return;
    const headingTokens = tokenSet(hit.heading ?? "");
    splitSentences(hit.text ?? "").forEach((raw, pos) => {
      const sentence = strip(raw, "-*• ").trimEnd();
      if ([...sentence].length < 6) return;
      const sentenceTokens = tokenSet(sentence);
      let direct = 0, context = 0;
      for (const t of queryTokens) {
        if (sentenceTokens.has(t)) direct += 1;
        else if (headingTokens.has(t)) context += 1;
      }
      const score = direct / queryTokens.size + (0.3 * context) / queryTokens.size;
      if (score >= 0.34) scored.push({ score: score - rank * 0.05, ref: hit.ref, pos, sentence });
    });
  });
  const CUT = X.CUT_MARK;
  if (scored.some((s) => !s.sentence.endsWith(CUT))) scored = scored.filter((s) => !s.sentence.endsWith(CUT));
  scored = scored.map((s) => ({ ...s, sentence: s.sentence.endsWith(CUT) ? s.sentence.slice(0, -CUT.length) : s.sentence }));
  scored.sort((a, b) => b.score - a.score);
  const picked: { ref: number; pos: number; sentence: string }[] = [];
  const seen = new Set<string>();
  for (const s of scored) {
    if (seen.has(s.sentence)) continue;
    seen.add(s.sentence);
    picked.push(s);
    if (picked.length === limit) break;
  }
  picked.sort((a, b) => a.ref - b.ref || a.pos - b.pos);
  return picked.map((p) => `${p.sentence.replace(/[。；;]+$/, "")}[${p.ref}]。`);
}

/** The parts of a run-on question worth searching alone. */
export function clausesOf(text: string): string[] {
  const clauses = sub(T.ORDER_ID_RE, " ", text).split(/[，,。！!？?；;~～\s]+/).filter((c) => c.length >= 4);
  return clauses.length > 1 ? clauses : [];
}

function opening(hit: Output): string[] {
  const sentence = splitSentences(hit.text ?? "").map((raw) => strip(raw, "-*• ").trimEnd()).find((s) => [...s].length >= 6);
  return sentence ? [`${sentence.replace(/[。；;…]+$/, "")}[${hit.ref}]。`] : [];
}

export function compose(input: string, allResults: Results, tenant: string, exclude: Set<string>): string {
  if (!Object.values(allResults).some((r) => r.length)) {
    const reply = smalltalk(input);
    if (reply) return reply;
  }
  const other = otherQuestion(input);
  const [faults, text, extra] = allResults.create_ticket?.length ? faultIntents(input) : ([[], ...splitIntents(input)] as [string[], string, string | null]);
  const custom = tenantTools(tenant);
  const customParts = composeCustom(text, allResults, custom);
  const searches = allResults.search_knowledge ?? [];
  const extraSearch = extra ? searches.find((r) => r.query === extra) : undefined;
  const results: Results = { ...allResults, search_knowledge: searches.filter((r) => r !== extraSearch) };
  const parts: string[] = [];
  const last = (name: string): Output | null => (results[name]?.length ? results[name][results[name].length - 1] : null);

  const handoff = last("handoff_to_human");
  if (handoff) parts.push(handoff.message ?? handoff.error ?? "");
  parts.push(...customParts);
  for (const [name, outputs] of Object.entries(results)) {
    if (BUILTIN.has(name) || custom.some((t) => t.name === name)) continue;
    parts.push(...outputs.filter((o) => o.status === "rejected" && o.message).map((o) => `${o.message}${X.REJECTED_NEXT_STEP}`));
  }

  let listing = last("list_my_orders");
  if (listing && !results.get_order?.length && !results.create_ticket?.length) {
    if (exclude.size) listing = { ...listing, orders: (listing.orders ?? []).filter((o: Output) => !exclude.has(o.order_id)) };
    parts.push(askWhich(text, listing!));
  }

  const withdrawal = last("withdraw_refund_request");
  if (withdrawal) parts.push(withdrawal.message ?? `未能撤回退款申请：${withdrawal.error ?? ""}。`);
  let orders: Output[] = withdrawal ? [] : results.get_order ?? [];
  const refund = last("request_refund");
  const progress = orders.length === 1 && !orders[0].error && asksRefundStatus(text);
  if (progress) {
    parts.push(refundProgressText(orders[0]));
    orders = [];
  }
  const shipments = results.track_shipment ?? [];
  const tracked = new Set(shipments.filter((s) => !s.error).map((s) => s.order_id));
  for (const order of orders) {
    if (order.error) {
      const check = String(order.error).includes("订单不存在") ? "请核对订单号后再试。" : "";
      parts.push(`未能查询到该订单：${order.error}。${check}`);
    } else if (!refund && !tracked.has(order.order_id)) {
      const items = (order.items ?? []).map((i: Output) => `${i.name}×${i.qty}`).join("、");
      parts.push(`订单 ${order.order_id} 当前状态为“${order.status_text}”，订单金额 ¥${money(order.amount)}，商品：${items}。`);
      if (orders.length === 1 && mentionsRefund(text) && !order.refundable) parts.push(`该订单暂不能发起退款：${order.refund_note ?? ""}。`);
      else if (orders.length === 1 && mentionsRefund(text) && order.refund_note) parts.push(`${order.refund_note}。`);
    }
  }
  const clauses = orders.length > 1 ? orderClauses(text) : {};
  for (const order of orders.length > 1 ? orders : []) {
    const clause = clauses[order.order_id] ?? "";
    if (!order.error && mentionsRefund(clause) && !refundRequested(text)) {
      parts.push(!order.refundable ? `订单 ${order.order_id} 暂不能发起退款：${order.refund_note ?? ""}。`
        : (order.refund_note ? `订单 ${order.order_id}：${order.refund_note}。` : "") + `如需为订单 ${order.order_id} 申请退款，回复“${order.order_id} 申请退款”即可。`);
    }
  }
  if (orders.length > 1 && refundRequested(text)) parts.push(X.MULTI_REFUND_TEXT);

  for (const shipment of shipments) {
    if (shipment.error) parts.push(`物流查询失败：${shipment.error}。`);
    else if (shipment.message) parts.push(shipment.message);
  }

  const refundFailed = Boolean(refund) && (Boolean(refund!.error) || refund!.status === "rejected" || refund!.refund_status === "pending");
  if (refund) {
    if (refund.awaiting_review || refund.confirm_order) parts.push(`${refund.error}。`);
    // The tool read the customer's own words as not a request: never its note to the model, only the offer.
    else if (String(refund.error ?? "").startsWith("顾客这句话") && orders.length === 1) parts.push(`如需现在申请退款，回复“${orders[0].order_id} 申请退款”即可，我马上为您提交。`);
    else if (refund.error) parts.push(`退款申请未能提交：${refund.error}。`);
    else if (refund.status === "rejected") {
      parts.push(refund.message ?? "");
      parts.push(X.REJECTED_NEXT_STEP);
    } else parts.push(refund.message ?? "");
  }

  const ticket = last("create_ticket");
  const knowledge = last("search_knowledge");
  const selfDamage = M._SELF_DAMAGE.test(text);
  const eta = shipments.length > 0 && (M._ETA.test(text) || wantsTracking(text) && ARRIVAL.test(text));
  const rush = orders.length > 0 && !shipments.length && M._RUSH.test(text);
  const inquiry = orders.length === 1 && !refund && !orders[0].error && orders[0].refundable && mentionsRefund(text) && !refundRequested(text);
  if (knowledge) {
    const query = (refund && !refundFailed) || progress ? "退款 到账 工作日"
      : selfDamage ? X.SELF_DAMAGE_QUERY
      : eta || rush || ticket ? expandQuery(knowledge.query ?? text)
      : expandQuery(inquiry ? refundQuestion(text) : text);
    if (!refundFailed) {
      const plain = !(refund || ticket || progress || customParts.length || eta || rush || selfDamage || knowledge.chosen);
      const limit = !plain && !knowledge.chosen ? 1 : inquiry ? 2 : 4;
      let sentences = plain ? answerSentences(tenant, inquiry ? refundQuestion(text) : sub(T.ORDER_ID_RE, " ", text), knowledge.results ?? [], limit) : extract(query, knowledge.results ?? [], limit);
      // About one order's refund: only what is about refunds, whatever else the message's other words brought up.
      if (inquiry && !extra && !asksBeyondRefunds(text) && !OTHER_SUBJECT.test(text)) sentences = sentences.filter((s) => ABOUT_REFUNDS.test(s));
      // A question in several clauses is answered clause by clause, not only by what covers all of it.
      if (!plain && sentences.length < limit) {
        for (const clause of clausesOf(text)) {
          for (const sentence of extract(expandQuery(clause), knowledge.results ?? [], 1)) {
            if (sentences.length < limit && !sentences.includes(sentence)) sentences = [...sentences, sentence];
          }
        }
      }
      // The model tier chose these chunks for a question whose words they do not share: their opening sentences are quoted.
      if (!sentences.length && knowledge.chosen) sentences = (knowledge.results ?? []).flatMap((hit: Output) => opening(hit));
      const asked = orders.length === 1 && !inquiry && !mentionsRefund(text) && !(eta || rush || ticket) ? orderQuestion(text) : "";
      if (sentences.length) parts.push(...sentences);
      // What is asked about the order's own item was answered by its record: saying there is no basis would contradict that.
      else if (asked && !(orders[0]?.items ?? []).some((item: Output) => namesItem(asked, item.name ?? ""))) parts.push(`关于“${asked}”，知识库中没有找到相关依据。`);
      else if (!parts.length && !ticket) parts.push(knowledge.error ? X.SEARCH_DOWN : `${X.REFUSAL}${canDo(tenant)}`);
    }
  }
  if (inquiry) {
    const oid = orders[0].order_id;
    if (T.NEGATED_REFUND_RE.test(normalizeWidth(text))) parts.push(`如需退款，可以直接回复“${oid} 申请退款”，这次只解答问题、不会为您发起退款。`);
    else parts.push(`如需现在申请退款，回复“${oid} 申请退款”即可，我马上为您提交。`);
  }
  if (faults.length) {
    const things = faultTickets(text, faults, last("list_my_orders") ?? {}, results.get_order ?? [], exclude);
    const found = things.filter((t) => t.title.endsWith(" 换货/维修")).map((t) => t.title.slice(0, -" 换货/维修".length));
    const loose = things.filter((t) => !t.title.endsWith(" 换货/维修")).map((t) => t.description);
    if (found.length > 1 || (found.length && loose.length)) parts.push(`这几件分开处理：${found.join("、")}。`);
    if (loose.length) parts.push(`如有${loose.map((c) => `“${c}”`).join("、")}对应的订单号，可以回复给我，方便同事核对。`);
  }
  for (const made of results.create_ticket ?? []) parts.push(made.message ?? `工单创建失败：${made.error ?? ""}。`);
  if (selfDamage) parts.push("如需协商处理，可以回复“转人工”。");
  if (shipments.length && chasing(text) && !shipments.some((s) => s.delivered)) parts.push(X.CHASE_TEXT);
  if (rush) parts.push(X.RUSH_TEXT);
  if (orders.length === 1 && !refund && M._CANCEL.test(text) && !mentionsRefund(text) && !orders[0].error) {
    const oid = orders[0].order_id;
    parts.push(orders[0].status === "paid" && orders[0].refundable
      ? `如需取消这个订单，回复“${oid} 帮我取消订单”即可，未发货的订单按全额退款提交审核。`
      : "如需取消：这个订单已经发货或不能在线退款，不能直接取消；签收后可以按售后政策申请退货退款，或回复“转人工”。");
  }
  if (M._IF_COMPLAIN.test(text) && !handoff) parts.push(X.IF_COMPLAIN_TEXT);

  if (extraSearch) {
    const sentences = answerSentences(tenant, extra!, extraSearch.results ?? [], refund && extra === other ? 1 : 2);
    parts.push(sentences.length ? `另外，${sentences.join("")}` : `关于“${extra}”，知识库中没有找到相关依据。`);
  }
  // "补偿券要是能发的话…": a tenant's tool the message only wonders about.
  const wondered = custom.find((t) => matches(text, t) > 0 && (t.risk === "write" || t.risk === "money"));
  if (wondered && parts.length === 1 && parts[0].startsWith(X.REFUSAL)) parts[0] = `这次没有办理${wondered.label}。如需办理，请直接告诉我，例如“${wondered.hints?.intents?.[0] ?? wondered.label}”。`;
  if (!parts.filter(Boolean).length) parts.push(`${X.REFUSAL}${canDo(tenant)}`);
  return parts.filter(Boolean).join("");
}

// ---------------------------------------------------------------- an item instead of an order number
// Most people say "那个陶瓷马克杯", not its number. The browser engine goes one step past the offline model here, for the
// customer's own orders only: an item (or a price) that fits one order is read as that order. It never files anything on
// such a guess: a refund demand is offered back ("需要为订单 … 申请退款吗"), everything else only reads.
const LIST_ORDERS = /(?:下了|下过|买了|买过|下的|买的).{0,4}(?:哪些|什么|啥)|(?:哪些|什么).{0,3}(?:订单|单子)|最近.{0,4}(?:订单|单子|下的单)|有几个?(?:订单|单子)/;
const ORDER_NUMBER = /订单号|单号|哪一?单|什么单/;
const TRACKING = /运单|快递|物流|到哪|发货了吗|发了没|发货没/;
const STATUS = /状态|怎么样了|开通了吗|生效了吗|查一下|看一下|看看/;
const PRICE = /(?:[¥￥]\s*)?(\d+(?:\.\d+)?)\s*(?:元|块)?/g;

interface OwnOrder {
  id: string;
  items: string[];
  amount: number;
}

export function theOneOrder(text: string, orders: OwnOrder[]): OwnOrder | null {
  const named = bestNamed(text, orders, (o) => o.items);
  const prices = [...sub(T.ORDER_ID_RE, " ", text).matchAll(PRICE)].map((m) => Number(m[1])).filter((n) => n > 0);
  const priced = orders.filter((o) => prices.some((p) => Math.abs(p - o.amount) < 0.005));
  if (named.length === 1) return named[0];
  // "豪华年卡 198块": both narrow it down; the name alone often fits several (a membership by the month, and by the year).
  const both = (named.length ? named : orders).filter((o) => priced.includes(o));
  if (both.length === 1 && (named.length || priced.length === 1)) return both[0];
  return null;
}

const DEMANDS = /我要退|要退|想退|退给我|退掉|退我|给我退|帮我退|退一下/;
// "数字专辑买了能退吗" asks about the policy; "那个专辑能退吗" and "五天前扣的会员能退吗", about the one they bought.
const OWN_ITEM = /那个|这个|我的|我要|我买|我那|刚买|上次|上周|昨天|前天|帮我|给我|\d+[块元]|[前几].{0,2}天|扣的|扣了|买的|收到|签收/;

export function readsAsOrder(text: string, orders: OwnOrder[]): { question: string; unconfirmed: string | null } | null {
  if (T.ORDER_ID_RE.test(text) || T.OTHER_ORDER.test(text) || refundWithdrawRequested(text)) return null;
  if (LIST_ORDERS.test(text) && !mentionsRefund(text)) return { question: "我的订单有哪些", unconfirmed: null };
  const order = theOneOrder(text, orders);
  const several = orders.filter((o) => o.items.some((name) => namesItem(text, name)));
  if (!order && several.length >= 2 && several.length <= 3 && TRACKING.test(text) && !mentionsRefund(text)) return { question: `${several.map((o) => o.id).join(" ")} 到哪了`, unconfirmed: null };
  if (!order) {
    // "豪华会员刚扣的费我要退": several fit, so which one is asked, and nothing can be filed without a number.
    return DEMANDS.test(text) && bestNamed(text, orders, (o) => o.items).length > 1 ? { question: `我要退款 ${text}`, unconfirmed: null } : null;
  }
  if (refundRequested(text) || (mentionsRefund(text) && (OWN_ITEM.test(text) || aboutMyOrder(text)))) return { question: `${order.id} ${text}`, unconfirmed: order.id };
  if (aboutMyOrder(text)) return null; // the offline model's own path follows up on one match
  if (M._RUSH.test(text)) return { question: `${order.id} ${text}`, unconfirmed: order.id };
  if (TRACKING.test(text) || wantsTracking(text)) return { question: `${order.id} 到哪了`, unconfirmed: null };
  if (ORDER_NUMBER.test(text) || STATUS.test(text)) return { question: `${order.id} 查一下`, unconfirmed: null };
  return null;
}

/** What the planner reads: typos fixed, an ordinal after a listing resolved. */
export function understood(question: string, history: Turn[], previous: string | null = null): { text: string; exclude: Set<string> } {
  const fixed = fixTypos(question);
  const text = resolveOrdinal(fixed, history) ?? fixed;
  return { text, exclude: T.OTHER_ORDER.test(text) ? new Set([...earlierOrders(history), ...upperIds(previous ?? "")]) : new Set() };
}
