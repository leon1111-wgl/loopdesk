/**
 * The agent's tools over an in-tab copy of the demo data: app/agent/business_tools.py and app/tooling, without a
 * server. The same checks in the same order: who may see an order, the after-sales windows, the tenant's refund
 * limit, the velocity limit, four eyes. Thresholds come from the tenant's configuration, never from this file.
 * Nothing leaves the page: a reload starts again from the seed data.
 */
import { noteToolCall } from "./decisionlog";
import { decide, denial, entity, fen, NOT_LOADED, policyEngine, uid, type Decision, type PolicyEngine } from "./policy";
import { pack, T, TOOLS, type SeedOrder, type TenantConfig, type TenantTool } from "./pack";
import { clauseHits, search } from "./retrieval";
import { claimsAuthority, classifyReply, conditionalRefund, findAll, fixTypos, itemWords, money, normalizeWidth, plainRequest, refundBareDecline, refundDeclined, refundRequested, refundWithdrawRequested, statedRefundAmount, withdrawConfirmed } from "./text";

const TX = pack.texts.tools;
const HOUR = 3_600_000;
const DAY = 24 * HOUR;
/** Every change the engine makes is marked, like the backend's dry run marks its own with "（演练）". */
export const SIMULATED = "（模拟）";
const REFUNDABLE = new Set(["paid", "shipped", "delivered"]);
const LIVE = new Set(["pending", "processing", "succeeded"]); // what holds an order, and what the velocity limit counts

export interface Evidence {
  ref: number;
  kind: "kb" | "tool";
  title: string;
  text: string;
  meta: Record<string, unknown>;
}

export class ToolError extends Error {}

/** The order's application already waits for a person, or the order has to be confirmed: not a failure. */
class RefundWaiting extends ToolError {
  constructor(message: string, public output: Record<string, unknown>) {
    super(message);
  }
}

export interface ApprovalNeed {
  reason: string;
  customerNote: string;
  approverRole?: "admin" | "agent";
}

export type Outcome = (
  | { kind: "result"; output: Record<string, unknown>; isError: boolean }
  | { kind: "approval"; reason: string; customerNote: string; args: Record<string, unknown>; approverRole?: "admin" | "agent" }
) & { decisions?: Decision[] }; // the policy decisions the call took: each becomes a span

interface OrderRow {
  id: string;
  customer: string;
  amount: number;
  status: string;
  items: SeedOrder["items"];
  region: string;
  paidAt: number | null;
  deliveredAt: number | null;
  shipment: { carrier: string; tracking_no: string; events: { time: string; desc: string }[] } | null;
}

export interface RefundRow {
  id: number;
  tenant: string;
  orderId: string;
  customer: string;
  amount: number;
  status: string;
  createdAt: number;
  reason: string;
  filed: boolean; // through this ledger; the seed's old refunds were not, and do not hold their orders
}

export interface ApprovalRow {
  id: number;
  tenant: string;
  runId: string;
  conversationId: number | null;
  tool: string;
  callId: string;
  args: Record<string, unknown>;
  reason: string;
  evidence: Evidence[];
  status: "pending" | "approved" | "rejected" | "withdrawn";
  requester: string;
  reviewer: string | null;
  question: string;
  comment: string;
  approverRole?: "admin" | "agent";
  createdAt: number;
  decidedAt: number | null;
  /** From a replayed recording: it holds the order here, and the recording's own queue already shows it. */
  recorded?: boolean;
}

export interface TicketRow {
  id: number;
  tenant: string;
  customer: string;
  conversationId: number | null;
  title: string;
  description: string;
  priority: string;
  kind: "ticket" | "handoff";
  dueAt: number;
  createdAt: number;
}

export interface ToolContext {
  tenant: string;
  config: TenantConfig;
  user: string; // the account's e-mail
  role: "admin" | "agent" | "customer";
  conversationId: number | null;
  runId: string;
  evidence: Evidence[];
  customerText: string;
  unconfirmedOrder: string | null;
  approvalId: number | null;
  /** The answer before this turn, for the second step of a withdrawal. */
  withdrawOffer: Record<string, unknown> | null;
  /** The policy decisions taken during the current tool call, and the refund precheck's, read again by its approval step. */
  decisions?: Decision[];
  refundGate?: RefundGate | null;
}

const isStaff = (ctx: ToolContext) => ctx.role === "admin" || ctx.role === "agent";

function shanghai(ms: number, withTime = false): string {
  const shifted = new Date(ms + 8 * HOUR).toISOString();
  return withTime ? `${shifted.slice(0, 10)} ${shifted.slice(11, 16)}` : shifted.slice(0, 10);
}

function words(order: OrderRow) {
  return virtual(order)
    ? { delivered: "已开通", since: "开通", first: "无理由退款", second: "需核实本期权益未使用后才能退款" }
    : { delivered: "已签收", since: "签收", first: "无理由退货", second: "仅质量问题可以退换" };
}

function virtual(order: OrderRow): boolean {
  return order.items.length > 0 && order.items.every((i) => i.virtual);
}

function statusText(order: OrderRow): string {
  return order.status === "delivered" ? words(order).delivered : TX.STATUS_TEXT[order.status] ?? order.status;
}

function noOnlineRefund(order: OrderRow): string | null {
  return order.items.find((i) => i.no_online_refund)?.no_online_refund ?? null;
}

function lastRefundDay(config: TenantConfig): number {
  return Math.max(config.return_window_days, config.quality_window_days);
}

export function addEvidence(ctx: ToolContext, kind: Evidence["kind"], title: string, text: string, meta: Record<string, unknown> = {}): Evidence {
  const chunk = kind === "kb" ? meta.chunk_id : undefined;
  if (chunk !== undefined) {
    const seen = ctx.evidence.find((e) => e.kind === "kb" && e.meta.chunk_id === chunk);
    if (seen) return seen;
  }
  const item: Evidence = { ref: ctx.evidence.length + 1, kind, title, text, meta };
  ctx.evidence.push(item);
  return item;
}

/** The in-tab copy of one browser's demo data. */
export class World {
  orders = new Map<string, OrderRow>();
  refunds: RefundRow[] = [];
  approvals: ApprovalRow[] = [];
  tickets: TicketRow[] = [];
  vouchers: { id: string; amount: number; reason: string; owner: string }[] = [];
  /** The policy this copy of the data is decided by: the shipped one (null), or a what-if variant (pages/PolicyPage.tsx). */
  policy: PolicyEngine | null = null;
  private seq = 900_000;

  constructor(public now: () => number = () => Date.now()) {
    const at = now();
    const ago = (h: number | null) => (h === null ? null : at - h * HOUR);
    for (const [tenant, orders] of Object.entries(pack.orders)) {
      for (const o of orders) {
        this.orders.set(`${tenant}/${o.id}`, {
          id: o.id, customer: o.customer, amount: o.amount, status: o.status, items: o.items, region: o.region ?? "domestic",
          paidAt: ago(o.paid_ago_h), deliveredAt: ago(o.delivered_ago_h),
          shipment: o.shipment && { carrier: o.shipment.carrier, tracking_no: o.shipment.tracking_no,
            events: o.shipment.events.map((e) => ({ time: shanghai(at - e.ago_h * HOUR, true), desc: e.desc })) },
        });
      }
    }
    for (const [tenant, refunds] of Object.entries(pack.refunds)) {
      for (const r of refunds) {
        this.refunds.push({ id: this.id(), tenant, orderId: r.order_id, customer: r.customer, amount: r.amount, status: r.status,
          createdAt: ago(r.ago_h) ?? at, reason: r.reason, filed: false });
      }
    }
  }

  id(): number {
    this.seq += 1;
    return this.seq;
  }

  order(tenant: string, id: string): OrderRow | undefined {
    return this.orders.get(`${tenant}/${id}`);
  }

  ordersOf(tenant: string, customer: string): OrderRow[] {
    return [...this.orders.entries()].filter(([key, o]) => key.startsWith(`${tenant}/`) && o.customer === customer).map(([, o]) => o)
      .sort((a, b) => (b.paidAt ?? 0) - (a.paidAt ?? 0));
  }
}

function orderIdOf(args: Record<string, unknown>): string {
  const value = normalizeWidth(String(args.order_id ?? "")).trim().toUpperCase();
  if (!value) throw new ToolError("缺少订单号");
  if (value.length > 64) throw new ToolError("订单号格式不正确");
  return value;
}

function loadOrder(world: World, ctx: ToolContext, id: string): OrderRow {
  const order = world.order(ctx.tenant, id);
  if (!order || (!isStaff(ctx) && order.customer !== ctx.user)) throw new ToolError(TX.NOT_FOUND_TEXT);
  return order;
}

function daysSince(order: OrderRow, at: number): number | null {
  return order.deliveredAt === null ? null : Math.max(0, Math.floor((at - order.deliveredAt) / DAY));
}

function liveRefund(world: World, ctx: ToolContext, orderId: string): RefundRow | undefined {
  return world.refunds.find((r) => r.tenant === ctx.tenant && r.orderId === orderId && r.filed && LIVE.has(r.status));
}

function heldBy(refund: RefundRow | undefined): string {
  if (!refund || refund.status === "pending") return "该订单已有一笔退款正在处理，请勿重复提交";
  if (refund.status === "succeeded") return `该订单已退款（退款单 #${refund.id}），${TX.ONE_REFUND_TEXT}`;
  return `该订单的退款已受理（退款单 #${refund.id}），${TX.ONE_REFUND_TEXT}`;
}

function refundProgress(world: World, ctx: ToolContext, orderId: string) {
  const refunds = world.refunds.filter((r) => r.tenant === ctx.tenant && r.orderId === orderId).sort((a, b) => b.id - a.id).slice(0, 5).map((r) => ({
    refund_id: r.id, amount: Math.round(r.amount * 100) / 100, status: r.status, status_text: TX.REFUND_STATUS_TEXT[r.status] ?? r.status,
    filed_on: shanghai(r.createdAt),
  }));
  const waiting = [...world.approvals].reverse().find((a) => a.tenant === ctx.tenant && a.tool === "request_refund" && a.status === "pending" && a.args.order_id === orderId);
  const approval = waiting ? { approval_id: waiting.id, amount: Math.round(Number(waiting.args.amount ?? 0) * 100) / 100, status_text: "等待人工审核", filed_on: shanghai(waiting.createdAt) } : null;
  return { refunds, approval };
}

// ---------------------------------------------------------------- read tools
function searchKnowledge(_world: World, ctx: ToolContext, args: Record<string, unknown>): Record<string, unknown> {
  const query = fixTypos(String(args.query ?? "").trim());
  if (!query) throw new ToolError("query 不能为空");
  const topK = Math.max(1, Math.min(Number(args.top_k ?? pack.settings.retrieval_top_k), 8));
  const scope = new Set(isStaff(ctx) ? ["public", "internal"] : ["public"]);
  let hits = search(ctx.tenant, query, scope, topK);
  if (hits.length) {
    const floor = 0.5 * Math.max(...hits.map((h) => h.relevance));
    hits = hits.filter((h) => h.relevance >= floor);
  }
  // A run-on question is several questions: each part gets its own search and its own gate.
  const seen = new Set(hits.map((h) => h.chunk.id));
  for (const hit of clauseHits(ctx.tenant, query, scope)) {
    if (!seen.has(hit.chunk.id) && hits.length < 8) {
      seen.add(hit.chunk.id);
      hits.push(hit);
    }
  }
  const results = hits.map((hit) => {
    const evidence = addEvidence(ctx, "kb", hit.chunk.doc, hit.chunk.text, { heading: hit.chunk.heading, chunk_id: hit.chunk.id, document_id: hit.chunk.doc_id, page: null, relevance: Math.round(hit.relevance * 1000) / 1000 });
    return { ref: evidence.ref, doc: hit.chunk.doc, heading: hit.chunk.heading, text: hit.chunk.text, relevance: Math.round(hit.relevance * 1000) / 1000, page: null };
  });
  const output: Record<string, unknown> = { query, results };
  if (!results.length) output.note = "知识库中没有足够相关的内容";
  return output;
}

function getOrder(world: World, ctx: ToolContext, args: Record<string, unknown>): Record<string, unknown> {
  const order = loadOrder(world, ctx, orderIdOf(args));
  const text = statusText(order);
  let refundable = REFUNDABLE.has(order.status);
  let refundNote = refundable ? "" : `该订单当前状态为“${text}”，不能重复申请退款`;
  const blocked = noOnlineRefund(order);
  if (refundable && blocked) {
    refundable = false;
    refundNote = blocked;
  }
  const live = refundable ? liveRefund(world, ctx, order.id) : undefined;
  if (live) {
    refundable = false;
    refundNote = heldBy(live).replace(/，请勿重复提交$/, "");
  }
  const days = daysSince(order, world.now());
  if (refundable && days !== null) {
    const w = words(order), c = ctx.config;
    if (days > lastRefundDay(c)) {
      refundable = false;
      refundNote = `订单已${w.since} ${days} 天，超过 ${lastRefundDay(c)} 天的售后期限`;
    } else if (days > c.return_window_days) {
      refundNote = `订单已${w.since} ${days} 天，已超过 ${c.return_window_days} 天${w.first}期限，${c.quality_window_days} 天内${w.second}，提交后需要人工核实`;
    }
  }
  const { refunds, approval } = refundProgress(world, ctx, order.id);
  return {
    order_id: order.id, status: order.status, status_text: text, amount: Math.round(order.amount * 100) / 100, items: order.items,
    paid_at: order.paidAt === null ? null : shanghai(order.paidAt), delivered_at: order.deliveredAt === null ? null : shanghai(order.deliveredAt),
    days_since_delivery: days, region: order.region, refundable, refund_note: refundNote, refunds, refund_awaiting_approval: approval,
    summary: `订单 ${order.id} 当前状态为“${text}”，订单金额 ¥${money(order.amount)}`,
  };
}

function listMyOrders(world: World, ctx: ToolContext): Record<string, unknown> {
  return {
    orders: world.ordersOf(ctx.tenant, ctx.user).slice(0, pack.settings.my_orders_limit).map((o) => ({
      order_id: o.id, status: o.status, status_text: statusText(o), amount: Math.round(o.amount * 100) / 100,
      paid_at: o.paidAt === null ? null : shanghai(o.paidAt), items: o.items.map((i) => i.name),
    })),
  };
}

function trackShipment(world: World, ctx: ToolContext, args: Record<string, unknown>): Record<string, unknown> {
  const order = loadOrder(world, ctx, orderIdOf(args));
  const shipment = order.status !== "paid" ? order.shipment : null;
  if (virtual(order)) {
    const names = order.items.map((i) => i.name).join("、");
    return { order_id: order.id, shipped: false, virtual: true, message: `订单 ${order.id}（${names}）是虚拟商品，付款后即时生效，没有物流信息。` };
  }
  if ((!shipment || !shipment.events.length) && order.status === "delivered") {
    const day = order.deliveredAt === null ? null : shanghai(order.deliveredAt);
    return { order_id: order.id, shipped: true, delivered: true, events: [], message: `订单 ${order.id} 已${day ? `于 ${day} ` : ""}签收，暂无物流明细。` };
  }
  if (!shipment || !shipment.events.length) return { order_id: order.id, shipped: false, message: `订单 ${order.id} 目前还未发货。` };
  const latest = shipment.events[shipment.events.length - 1];
  return {
    order_id: order.id, shipped: true, carrier: shipment.carrier, tracking_no: shipment.tracking_no, events: shipment.events,
    message: `订单 ${order.id} 由${shipment.carrier}承运，运单号 ${shipment.tracking_no}。最新进度（${latest.time}）：${latest.desc}。`,
  };
}

// ---------------------------------------------------------------- refunds
function applicationTime(world: World, ctx: ToolContext): number {
  const approval = ctx.approvalId === null ? undefined : world.approvals.find((a) => a.id === ctx.approvalId);
  return approval ? approval.createdAt : world.now();
}

// ---------------------------------------------------------------- the refund gate (policy.ts, backend/policies)
/** One refund call as the policy saw it: the facts, Cedar's decision, and what the refusals and the card need. */
export interface RefundGate {
  decision: Decision;
  order: OrderRow | null;
  amount: number | null;
  error: ToolError | null;
  waiting: ApprovalRow | undefined;
  days: number | null;
  stated: number | null;
  history: [number, number];
  rejected: string | null;
}

const REFUSED_WORDING = "顾客这句话是在咨询退款或说了先不退，并没有申请退款，不能发起退款。请回答顾客的问题；顾客明确要求退款时再申请。";

export function limitsOf(config: TenantConfig): Record<string, number> {
  return {
    auto_refund_limit: fen(config.auto_refund_limit), approval_admin_threshold: fen(config.approval_admin_threshold),
    return_window_days: config.return_window_days, last_refund_day: lastRefundDay(config),
    window_max_count: pack.settings.refund_window_max_count, window_max_total: fen(pack.settings.refund_window_max_amount),
  };
}

function otherReviewers(ctx: ToolContext): { other_admin: boolean; other_staff: boolean } {
  const others = Object.entries(pack.accounts).filter(([email, a]) => a.tenant === ctx.tenant && email !== ctx.user);
  return { other_admin: others.some(([, a]) => a.role === "admin"), other_staff: others.some(([, a]) => a.role !== "customer") };
}

/** The facts in the order the checks were always made, then the policy (app/agent/business_tools._refund_gate). */
function refundGate(world: World, ctx: ToolContext, args: Record<string, unknown>, intent = true, approval = true): RefundGate {
  const reviewed = ctx.approvalId !== null;
  const context: Record<string, unknown> = { reviewed, customer_declined: false, customer_asked: true, claimed_authority: intent && !reviewed && claimsAuthority(ctx.customerText),
    limits: limitsOf(ctx.config), reviewers: { other_admin: true, other_staff: true } };
  const gate: RefundGate = { decision: { action: "request_refund", effect: "deny", reasons: [], errors: [], micros: 0, context }, order: null, amount: null, error: null, waiting: undefined, days: null, stated: null, history: [0, 0], rejected: null };
  const decided = () => {
    gate.decision = decide(world.policy, ["User", ctx.user], "request_refund", ["Order", String(args.order_id ?? "?") || "?"], context, [entity("User", ctx.user, { role: ctx.role })]);
    ctx.decisions?.push(gate.decision);
    return gate;
  };
  // Whatever the planner or the model makes of it, a question or a "先别退款" does not move money.
  if (intent && !reviewed) {
    const declined = refundDeclined(ctx.customerText) || conditionalRefund(ctx.customerText);
    const asked = plainRequest(ctx.customerText);
    if (declined || !asked) {
      Object.assign(context, { customer_declined: declined, customer_asked: asked });
      return decided();
    }
  }
  let order: OrderRow;
  try {
    order = loadOrder(world, ctx, orderIdOf(args));
  } catch (error) {
    if (!(error instanceof ToolError)) throw error;
    gate.error = error;
    return decided();
  }
  const raw = args.amount;
  const amount = Math.round(Number(raw === null || raw === undefined || raw === "" ? order.amount : raw) * 100) / 100;
  if (!Number.isFinite(amount)) {
    gate.error = new ToolError("退款金额格式不正确");
    return decided();
  }
  Object.assign(gate, { order, amount, days: daysSince(order, applicationTime(world, ctx)) });
  if (!reviewed) gate.waiting = world.approvals.find((a) => a.tenant === ctx.tenant && a.tool === "request_refund" && a.status === "pending" && a.args.order_id === order.id);
  const facts: Record<string, unknown> = {
    amount: fen(amount), paid: fen(order.amount), refundable_status: REFUNDABLE.has(order.status), online_refund: !noOnlineRefund(order), awaiting_review: gate.waiting !== undefined,
    unconfirmed: false, item_mismatch: false, refunds_in_window: 0, refunded_in_window: 0, rejected_recently: false, customer_has_account: true,
  };
  if (gate.days !== null) facts.days_since_delivery = gate.days;
  context.refund = facts;
  if (intent && !reviewed) {
    // A clause naming an item names one on this order, or it is not part of a request for this order.
    facts.item_mismatch = !plainRequest(ctx.customerText, itemWords(order.items.map((i) => i.name)));
    facts.unconfirmed = Boolean(ctx.unconfirmedOrder) && order.id === ctx.unconfirmedOrder!.toUpperCase();
    const oneOrder = new Set(findAll(T.ORDER_ID_RE, normalizeWidth(ctx.customerText)).map((m) => m[0].toUpperCase())).size <= 1;
    gate.stated = !isStaff(ctx) && oneOrder ? statedRefundAmount(ctx.customerText) : null;
    if (gate.stated !== null) facts.stated_amount = fen(gate.stated);
  }
  if (approval && !reviewed) {
    if (amount <= ctx.config.auto_refund_limit) {
      const since = world.now() - pack.settings.refund_window_days * DAY;
      const rows = world.refunds.filter((r) => r.tenant === ctx.tenant && r.customer === order.customer && r.createdAt >= since && LIVE.has(r.status));
      gate.history = [rows.length, rows.reduce((n, r) => n + r.amount, 0)];
      Object.assign(facts, { refunds_in_window: gate.history[0], refunded_in_window: fen(gate.history[1]) });
    }
    gate.rejected = rejectedBefore(world, ctx, order.id);
    facts.rejected_recently = gate.rejected !== null;
    if (isStaff(ctx)) context.reviewers = otherReviewers(ctx);
  }
  return decided();
}

/** A deny.* reason in the words the customer (or the planner) reads. */
function refusalFor(world: World, ctx: ToolContext, gate: RefundGate, reason: string): ToolError {
  const order = gate.order!;
  const amount = gate.amount!;
  switch (reason) {
    case "deny.customer_declined":
    case "deny.not_asked":
    case "deny.item_not_on_order":
      return new ToolError(REFUSED_WORDING);
    case "deny.amount_not_positive":
      return new ToolError("退款金额必须大于 0");
    case "deny.amount_above_paid":
      return new ToolError(`退款金额 ¥${money(amount)} 超过订单实付金额 ¥${money(order.amount)}，最多可退 ¥${money(order.amount)}；如需按 ¥${money(order.amount)} 申请，请回复“退${order.amount}元”`);
    case "deny.order_not_refundable":
      return new ToolError(`该订单当前状态为“${statusText(order)}”，不能发起退款`);
    case "deny.no_online_refund":
      return new ToolError(noOnlineRefund(order)!);
    case "deny.after_sales_deadline":
      return new ToolError(`订单已${words(order).since} ${gate.days} 天，超过 ${lastRefundDay(ctx.config)} 天的售后期限，不能在线申请退款；如有特殊情况，可以转人工客服处理`);
    case "deny.already_waiting": {
      const waiting = gate.waiting!;
      const filedAt = shanghai(waiting.createdAt, true);
      const first = Math.round(Number(waiting.args.amount ?? 0) * 100) / 100;
      let message = `您在 ${filedAt} 提交的 ¥${money(first)} 退款申请正在等待人工审核，无需重复申请，审核结果会在这里通知您`;
      if (Math.abs(first - amount) > 0.005) message += `；如需改成 ¥${money(amount)}，可以先回复“撤回退款申请”，再重新申请`;
      return new RefundWaiting(message, { error: message, awaiting_review: { approval_id: waiting.id, amount: money(first), filed_at: filedAt } });
    }
    case "deny.order_unconfirmed": {
      const names = order.items.map((i) => i.name).join("、");
      const note = String(getOrder(world, ctx, { order_id: order.id }).refund_note ?? "");
      const message = `需要为订单 ${order.id}（${names}）申请退款吗？${note ? `${note}。` : ""}回复“是的”或“${order.id} 申请退款”即可提交`;
      return new RefundWaiting(message, { error: message, confirm_order: order.id });
    }
    case "deny.stated_amount_differs": {
      const stated = gate.stated!;
      if (stated > order.amount + 1e-6) {
        return new ToolError(`顾客要退 ¥${money(stated)}，超过订单实付金额 ¥${money(order.amount)}，最多可退 ¥${money(order.amount)}；请先和顾客确认是否按 ¥${money(order.amount)} 申请，顾客确认后再提交`);
      }
      return new ToolError(`顾客要退 ¥${money(stated)}，这次申请的是 ¥${money(amount)}，金额不一致，没有提交；请按顾客说的金额申请，顾客改了金额时以顾客最新说的为准`);
    }
    case "deny.customer_has_no_account":
      return new ToolError("这笔订单的顾客没有本系统的账号，请在订单系统里为其办理退款");
    case "deny.no_other_reviewer":
      return new ToolError(amount > ctx.config.approval_admin_threshold ? "本租户除你之外没有其他管理员可以审批（提交人不能审批自己的申请）。" : "本租户除你之外没有其他员工可以审批（提交人不能审批自己的申请）。");
    default:
      return new ToolError(`按退款策略不能办理（${(world.policy ?? policyEngine())?.label(reason) ?? reason}）`);
  }
}

function enforce(world: World, ctx: ToolContext, gate: RefundGate, stage: "request" | "approval"): void {
  const reason = denial(gate.decision, world.policy, stage);
  if (reason) throw refusalFor(world, ctx, gate, reason);
  if (stage === "request" && gate.decision.effect === "deny" && !gate.decision.reasons.some((r) => r.startsWith("deny."))) {
    throw gate.error ?? new ToolError(gate.decision.errors.includes(NOT_LOADED) ? `${NOT_LOADED}，这次没有提交退款` : "按退款策略不能办理：没有一条策略允许这次调用（默认拒绝）");
  }
}

function validated(world: World, ctx: ToolContext, args: Record<string, unknown>): { order: OrderRow; amount: number } {
  const gate = refundGate(world, ctx, args, false, false);
  enforce(world, ctx, gate, "request");
  return { order: gate.order!, amount: gate.amount! };
}

function refundPrecheck(world: World, ctx: ToolContext, args: Record<string, unknown>): Record<string, unknown> {
  const gate = refundGate(world, ctx, args);
  enforce(world, ctx, gate, "request");
  ctx.refundGate = gate;
  return { order_id: gate.order!.id, amount: gate.amount! };
}

function rejectedBefore(world: World, ctx: ToolContext, orderId: string): string | null {
  const since = world.now() - pack.settings.rejected_before_days * DAY;
  const earlier = [...world.approvals].reverse().find((a) => a.tenant === ctx.tenant && a.tool === "request_refund" && a.args.order_id === orderId && a.status === "rejected" && (a.decidedAt ?? 0) >= since);
  if (!earlier) return null;
  return `该订单 ${shanghai(earlier.decidedAt ?? world.now())} 被${earlier.reviewer ?? "审批人"}驳回过（审批单 #${earlier.id}，意见：${(earlier.comment || "无").slice(0, 60)}），重新申请需人工复核`;
}

/** Who may approve an application of this amount, as the policy says (deny.approver_tier): an agent who did not file it, or an administrator. */
export function reviewerRole(config: TenantConfig, amount: number, approver = "", engine?: PolicyEngine | null): "admin" | "agent" {
  const application = entity("Application", "~probe", { requester: { __entity: uid("User", "~requester") }, amount: fen(amount), approver });
  const decision = decide(engine, ["User", "~agent"], "approve", ["Application", "~probe"], { approval_admin_threshold: fen(config.approval_admin_threshold) },
    [entity("User", "~requester", { role: "customer" }), entity("User", "~agent", { role: "agent" }), application]);
  return decision.effect === "allow" ? "agent" : "admin";
}

function refundPolicy(world: World, ctx: ToolContext, args: Record<string, unknown>): ApprovalNeed | null {
  let gate = ctx.refundGate;
  const amount = Math.round(Number(args.amount) * 100) / 100;
  if (!gate || !gate.order || gate.order.id !== String(args.order_id) || gate.amount !== amount) gate = refundGate(world, ctx, args);
  const order = gate.order!;
  const c = ctx.config;
  const reasons: string[] = [];
  let customerNote = "";
  const blocked = denial(gate.decision, world.policy, "approval");
  if (gate.decision.effect !== "review" && !blocked) return null;
  for (const reason of gate.decision.reasons) {
    if (reason === "review.staff_filed") reasons.push(TX.STAFF_FILED_REASON);
    else if (reason === "review.past_return_window") {
      const w = words(order);
      customerNote = `订单已${w.since} ${gate.days} 天，超过 ${c.return_window_days} 天${w.first}期限，${c.quality_window_days} 天内${w.second}`;
      reasons.push(customerNote);
      addEvidence(ctx, "tool", "after_sales_window", JSON.stringify({ order_id: order.id, days_since_delivery: gate.days, return_window_days: c.return_window_days, quality_window_days: c.quality_window_days }));
    } else if (reason === "review.above_auto_limit") reasons.push(`超过自动审批额度 ¥${money(c.auto_refund_limit)}`);
    else if (reason === "review.velocity_count" || reason === "review.velocity_total") {
      const { refund_window_days: window, refund_window_max_count: maxCount, refund_window_max_amount: maxAmount } = pack.settings;
      const [count, total] = gate.history;
      reasons.push(reason === "review.velocity_count" ? `近 ${window} 天已有 ${count} 笔退款，超过自动审批的次数上限（${maxCount} 笔）` : `近 ${window} 天累计退款将达到 ¥${money(total + amount)}，超过 ¥${money(maxAmount)} 的累计自动审批上限`);
      addEvidence(ctx, "tool", "refund_history", JSON.stringify({ customer: order.customer, window_days: window, refund_count: count, refund_total: Math.round(total * 100) / 100, this_request: amount }));
    } else if (reason === "review.rejected_before") reasons.push(gate.rejected!);
    else if (reason === "review.claimed_authority") reasons.push(TX.CLAIMED_AUTHORITY_REASON);
    else if (reason.startsWith("review.")) reasons.push((world.policy ?? policyEngine())?.label(reason) ?? reason);
  }
  if (blocked) throw refusalFor(world, ctx, gate, blocked);
  return reasons.length ? { reason: reasons.join("；"), customerNote } : null;
}

function requestRefund(world: World, ctx: ToolContext, args: Record<string, unknown>): Record<string, unknown> {
  const { order, amount } = validated(world, ctx, args);
  const status = ctx.approvalId !== null ? "approved" : "auto_approved";
  const refund: RefundRow = { id: world.id(), tenant: ctx.tenant, orderId: order.id, customer: order.customer, amount, status: "processing", createdAt: world.now(),
    reason: String(args.reason ?? "").slice(0, 500), filed: true };
  const live = liveRefund(world, ctx, order.id);
  if (live) throw new ToolError(heldBy(live));
  world.refunds.push(refund);
  order.status = "refunding";
  return { status, refund_status: "processing", refund_id: refund.id, order_id: order.id, amount,
    message: `${SIMULATED}订单 ${order.id} 的退款单 #${refund.id} 已受理，退款金额 ¥${money(amount)} 将按原支付方式退回。` };
}

function withdrawRefund(world: World, ctx: ToolContext, args: Record<string, unknown>): Record<string, unknown> {
  const text = normalizeWidth(ctx.customerText);
  const offer = ctx.withdrawOffer;
  const named = new Set(findAll(T.ORDER_ID_RE, text).map((m) => m[0].toUpperCase()));
  const namedOk = [...named].every((n) => n === offer?.order_id);
  const confirming = offer !== null && namedOk && (withdrawConfirmed(text) || classifyReply(text) === "yes");
  if (offer !== null && classifyReply(text) === "no" && !refundWithdrawRequested(text) && !refundBareDecline(text)) {
    return { withdrawn: false, kept: true, order_id: offer.order_id, message: `好的，不撤回订单 ${offer.order_id} 的退款申请。` };
  }
  const fallback: Record<string, unknown> = confirming ? offer! : named.size === 1 ? { order_id: [...named][0] } : {};
  const order = loadOrder(world, ctx, orderIdOf(args.order_id ? args : fallback));
  if (!refundWithdrawRequested(text) && !confirming && (refundRequested(text) || (T.REFUND_RE.test(text) && !refundBareDecline(text)))) {
    throw new ToolError("顾客这句话没有要求撤回退款申请，不能撤回。请回答顾客的问题。");
  }
  const waiting = world.approvals.filter((a) => a.tenant === ctx.tenant && a.tool === "request_refund" && a.status === "pending" && a.requester === ctx.user && a.args.order_id === order.id);
  if (!waiting.length) {
    const live = liveRefund(world, ctx, order.id);
    return { withdrawn: false, order_id: order.id, refund_id: live?.id ?? null, message: live
      ? `订单 ${order.id} 的退款已提交到支付系统（退款单 #${live.id}），无法在线撤回；如需处理请转人工客服。`
      : `订单 ${order.id} 没有等待审核的退款申请，无需撤回。` };
  }
  const offered = new Set<number>(confirming && offer?.order_id === order.id ? (offer.approval_ids as number[]) ?? [] : []);
  if (!waiting.some((a) => offered.has(a.id))) {
    const amount = Math.round(waiting.reduce((n, a) => n + Number(a.args.amount ?? 0), 0) * 100) / 100;
    return { withdrawn: false, confirm_required: true, order_id: order.id, amount, approval_ids: waiting.map((a) => a.id),
      message: `订单 ${order.id} 的退款申请（¥${money(amount)}）目前仍在等待人工审核，还没有撤回。撤回后这笔款项不会退回，以后如需退款要重新申请、重新排队审核。确定要撤回的话，请回复“确认撤回 ${order.id}”。` };
  }
  const done = waiting.filter((a) => offered.has(a.id));
  const now = world.now();
  for (const a of done) {
    a.status = "withdrawn";
    a.comment = TX.WITHDRAWN_COMMENT;
    a.decidedAt = now;
  }
  const amount = Math.round(done.reduce((n, a) => n + Number(a.args.amount ?? 0), 0) * 100) / 100;
  return { withdrawn: true, order_id: order.id, amount, approval_ids: done.map((a) => a.id),
    message: `${SIMULATED}已为您撤回订单 ${order.id} 待审核的退款申请（¥${money(amount)}），这笔款项不会退回；以后如需退款，可以重新申请。` };
}

// ---------------------------------------------------------------- tickets and handoff
function createTicket(world: World, ctx: ToolContext, args: Record<string, unknown>): Record<string, unknown> {
  const title = String(args.title ?? "").trim().slice(0, 120) || "客户问题";
  const description = String(args.description ?? "").trim().slice(0, 2000);
  let priority = String(args.priority ?? "normal");
  if (!(priority in TX.PRIORITY_TEXT)) priority = "normal";
  const sla = priority === "high" || priority === "urgent" ? 4 : ctx.config.ticket_sla_hours;
  const team = TOOLS._FAULT.test(`${title} ${description}`) ? "技术支持" : "售后专员";
  const ticket: TicketRow = { id: world.id(), tenant: ctx.tenant, customer: ctx.user, conversationId: ctx.conversationId, title, description, priority, kind: "ticket",
    dueAt: world.now() + sla * HOUR, createdAt: world.now() };
  world.tickets.push(ticket);
  return { ticket_id: ticket.id, priority, sla_hours: sla, team,
    message: `${SIMULATED}已为您创建工单 #${ticket.id}（优先级：${TX.PRIORITY_TEXT[priority]}），${team}将在 ${sla} 小时内联系您。` };
}

/** The product says a person replies here. Nobody will in a static page, and the visitor should know how to go on. */
export const queueNotice = (position: number) => `已为您转接人工客服，当前排队第 ${position} 位。这是演示，不会有真人接入；回复“算了 不用人工了”可以回到自动回答。`;

function handoffToHuman(world: World, ctx: ToolContext, args: Record<string, unknown>): Record<string, unknown> {
  const reason = String(args.reason ?? "用户要求转人工").slice(0, 200);
  const live = ctx.conversationId === null ? undefined : world.tickets.find((t) => t.kind === "handoff" && t.conversationId === ctx.conversationId);
  const waitingAhead = world.tickets.filter((t) => t.kind === "handoff" && t.tenant === ctx.tenant).length;
  if (live) {
    const position = world.tickets.filter((t) => t.kind === "handoff" && t.tenant === ctx.tenant && t.id <= live.id).length;
    return { queued: true, already_queued: true, queue_position: position, message: `您已在人工客服队列中，当前排第 ${position} 位，无需重复申请，请稍候。` };
  }
  world.tickets.push({ id: world.id(), tenant: ctx.tenant, customer: ctx.user, conversationId: ctx.conversationId, title: `转人工：${reason}`.slice(0, 60),
    description: ctx.customerText.slice(0, 120), priority: "high", kind: "handoff", dueAt: world.now() + 4 * HOUR, createdAt: world.now() });
  const position = waitingAhead + 1;
  return { queued: true, already_queued: false, queue_position: position,
    message: `${SIMULATED}${queueNotice(position)}` };
}

// ---------------------------------------------------------------- the tenant's own tools (tenant_packs)
export function tenantTools(tenant: string): TenantTool[] {
  return (pack.tools[tenant] ?? []).filter((t) => t.enabled);
}

function voucherNeed(ctx: ToolContext, tool: TenantTool, args: Record<string, unknown>): ApprovalNeed | null {
  const approval = tool.approval;
  if (!approval || approval.mode === "never") return null;
  const amount = Number(args[approval.amount_field ?? "amount"]);
  const note = approval.customer_note ?? "";
  // The tenant's refund tiers bound every money tool, whatever its own policy says (app/tooling/policy.py).
  const adminAbove = Math.min(ctx.config.approval_admin_threshold, approval.admin_above?.CNY ?? ctx.config.approval_admin_threshold);
  let approver: "admin" | "agent" = approval.approver === "admin" ? "admin" : "agent";
  if (Number.isFinite(amount) && amount > adminAbove) approver = "admin";
  const declared = approval.auto_limits?.CNY;
  const limit = declared === undefined ? undefined : Math.min(declared, ctx.config.auto_refund_limit);
  if (!Number.isFinite(amount)) return { reason: `${tool.label}没有金额，无法按额度自动办理`, customerNote: note, approverRole: "admin" };
  if (limit === undefined) return { reason: "CNY 没有设置自动办理额度，需人工审批", customerNote: note, approverRole: approver };
  if (amount > limit) return { reason: `金额 ¥${money(amount)} 超过自动办理额度 ¥${money(limit)}`, customerNote: note, approverRole: approver };
  return null;
}

function runTenantTool(world: World, ctx: ToolContext, tool: TenantTool, args: Record<string, unknown>): Record<string, unknown> {
  if (tool.name === "member_points") {
    const member = (pack.members[ctx.tenant] ?? []).find((m) => m.owner === ctx.user);
    if (!member) throw new ToolError("没有查到您的会员信息");
    return { points: member.points, tier: member.tier, message: `您当前有 ${member.points} 积分，会员等级为${member.tier}。` };
  }
  if (tool.name === "compensation_voucher") {
    const amount = Number(args.amount);
    const most = tool.parameters.properties?.amount?.maximum;
    if (most !== undefined && amount > most) throw new ToolError(`单张补偿券最多 ${most} 元`);
    const voucher = `V-${world.vouchers.length + 1}`;
    world.vouchers.push({ id: voucher, amount, reason: String(args.reason ?? ""), owner: ctx.user });
    return { status: "succeeded", voucher, amount, message: `${SIMULATED}已为您发放 ${money(amount)} 元补偿券，券号 ${voucher}。` };
  }
  throw new ToolError(`工具 ${tool.name} 不存在或当前身份无权使用`);
}

// ---------------------------------------------------------------- the same steps as Runtime._run_tool
export const BUILTIN_ROLES: Record<string, string[]> = {
  search_knowledge: ["admin", "agent", "customer"], get_order: ["admin", "agent", "customer"], list_my_orders: ["customer"],
  track_shipment: ["admin", "agent", "customer"], request_refund: ["admin", "agent", "customer"], withdraw_refund_request: ["customer"],
  create_ticket: ["admin", "agent", "customer"], handoff_to_human: ["admin", "agent", "customer"],
};

/** What changes data and has no gate of its own: decided by the policy before it runs (deny by default). */
const WRITE_TOOLS = new Set(["withdraw_refund_request", "create_ticket", "handoff_to_human"]);

function policyGate(world: World, ctx: ToolContext, action: string, name: string, risk: string): void {
  const decision = decide(world.policy, ["User", ctx.user], action, ["Tool", name], { reviewed: ctx.approvalId !== null }, [entity("User", ctx.user, { role: ctx.role }), entity("Tool", name, { risk })]);
  ctx.decisions?.push(decision);
  if (decision.effect !== "allow") throw new ToolError(decision.errors.includes(NOT_LOADED) ? `${NOT_LOADED}，这次没有办理` : `工具 ${name} 不存在或当前身份无权使用`);
}

export function availableTools(ctx: Pick<ToolContext, "tenant" | "role">): Set<string> {
  const names = Object.entries(BUILTIN_ROLES).filter(([, roles]) => roles.includes(ctx.role)).map(([name]) => name);
  return new Set([...names, ...tenantTools(ctx.tenant).map((t) => t.name)]);
}

export function runTool(world: World, ctx: ToolContext, name: string, rawArgs: Record<string, unknown>): Outcome {
  ctx.decisions = [];
  const outcome: Outcome = { ...gateAndRun(world, ctx, name, rawArgs), decisions: ctx.decisions };
  ctx.decisions = undefined;
  if (outcome.kind === "approval") noteToolCall(name, ctx.role, outcome.args, "approval", null, false, outcome.reason, outcome.approverRole ?? null);
  else noteToolCall(name, ctx.role, rawArgs, "result", outcome.output, outcome.isError);
  return outcome;
}

function gateAndRun(world: World, ctx: ToolContext, name: string, rawArgs: Record<string, unknown>): Outcome {
  const tenantTool = tenantTools(ctx.tenant).find((t) => t.name === name);
  if (!availableTools(ctx).has(name)) return { kind: "result", output: { error: `工具 ${name} 不存在或当前身份无权使用` }, isError: true };
  try {
    let args = rawArgs;
    let need: ApprovalNeed | null = null;
    if (WRITE_TOOLS.has(name) || (tenantTool && tenantTool.risk !== "read")) policyGate(world, ctx, tenantTool ? "call_tenant_tool" : name, name, tenantTool?.risk === "money" ? "high" : "write");
    if (name === "request_refund") {
      args = { ...args, ...refundPrecheck(world, ctx, args) };
      if (ctx.approvalId === null) need = refundPolicy(world, ctx, args);
    } else if (tenantTool?.risk === "money" && ctx.approvalId === null) {
      need = voucherNeed(ctx, tenantTool, args);
    }
    if (need) return { kind: "approval", reason: need.reason, customerNote: need.customerNote, args, approverRole: need.approverRole };
    let output: Record<string, unknown>;
    switch (name) {
      case "search_knowledge": output = searchKnowledge(world, ctx, args); break;
      case "get_order": output = getOrder(world, ctx, args); break;
      case "list_my_orders": output = listMyOrders(world, ctx); break;
      case "track_shipment": output = trackShipment(world, ctx, args); break;
      case "request_refund": output = requestRefund(world, ctx, args); break;
      case "withdraw_refund_request": output = withdrawRefund(world, ctx, args); break;
      case "create_ticket": output = createTicket(world, ctx, args); break;
      case "handoff_to_human": output = handoffToHuman(world, ctx, args); break;
      default: output = runTenantTool(world, ctx, tenantTool!, args);
    }
    if (name !== "search_knowledge") {
      const orderId = args.order_id ? { order_id: args.order_id } : {};
      addEvidence(ctx, "tool", name, JSON.stringify(output), orderId);
    }
    return { kind: "result", output, isError: false };
  } catch (error) {
    if (!(error instanceof ToolError)) throw error;
    const output = error instanceof RefundWaiting ? error.output : { error: error.message };
    const orderId = rawArgs.order_id ? { order_id: rawArgs.order_id } : {};
    addEvidence(ctx, "tool", name, JSON.stringify(output), orderId);
    return { kind: "result", output, isError: !(error instanceof RefundWaiting) };
  }
}
