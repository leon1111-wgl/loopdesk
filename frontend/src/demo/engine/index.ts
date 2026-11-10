/**
 * The browser engine behind the static demo's chat: one in-tab copy of the demo data, the conversations it has seen,
 * and the runs it made, in the shape the console's API returns them. replay.ts sends it every question nobody recorded.
 */
import type { AgentEvent } from "../../types";
import { noteApprovalDecision } from "./decisionlog";
import { pack, type TenantConfig } from "./pack";
import { resumeApproval, runTurn, type EngineConversation, type Message, type ModelChoice, type RunRecord } from "./runtime";
import { isRefusal } from "./text";
import { reviewerRole, World, type ApprovalRow, type TicketRow } from "./tools";

export type { ModelChoice } from "./runtime";

type Body = Record<string, unknown>;

export class EngineError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

export class DemoEngine {
  world = new World();
  conversations = new Map<number, EngineConversation>();
  runs = new Map<string, RunRecord>();

  /** tenantConfig: the tenant's configuration as the console shows it (the recording's /api/tenant). */
  constructor(private tenantConfig: (slug: string) => Partial<TenantConfig> | undefined) {}

  config(slug: string): TenantConfig {
    return { ...pack.tenants[slug], ...this.tenantConfig(slug) };
  }

  /** Whether this account may read the conversation: its owner, or staff of its tenant. */
  mayRead(conversation: EngineConversation, email: string): boolean {
    const account = pack.accounts[email];
    return conversation.email === email || (account.role !== "customer" && pack.accounts[conversation.email]?.tenant === account.tenant);
  }

  /** A conversation of this tab, or a recorded one the visitor goes on with. */
  conversation(id: number, email: string, recorded?: Body[]): EngineConversation {
    let conversation = this.conversations.get(id);
    if (!conversation) {
      const messages = (recorded ?? []) as unknown as Message[];
      const lastUser = [...messages].reverse().find((m) => m.role === "user");
      conversation = { id, email, messages: [...messages], previous: lastUser?.content ?? null, frame: null, withdrawOffer: null, handoff: null };
      this.conversations.set(id, conversation);
    }
    return conversation;
  }

  ask(email: string, conversationId: number, question: string, choice?: ModelChoice | null): AgentEvent[] {
    const account = pack.accounts[email];
    return runTurn({
      world: this.world, conversation: this.conversation(conversationId, email), email, name: account.name, role: account.role, tenant: account.tenant,
      config: this.config(account.tenant), question, choice, onRun: (run) => this.runs.set(run.id, run),
    });
  }

  /** What the rules would answer now, on copies of everything this turn could touch. */
  wouldRefuse(email: string, conversationId: number, question: string): boolean {
    const account = pack.accounts[email];
    const world = new World(this.world.now);
    world.approvals = this.world.approvals.map((a) => ({ ...a }));
    world.refunds = this.world.refunds.map((r) => ({ ...r }));
    for (const [key, order] of this.world.orders) world.orders.set(key, { ...order });
    const events = runTurn({ world, conversation: structuredClone(this.conversation(conversationId, email)), email, name: account.name, role: account.role, tenant: account.tenant, config: this.config(account.tenant), question });
    const final = events.find((e): e is Extract<AgentEvent, { type: "final" }> => e.type === "final");
    return Boolean(final) && final!.status === "completed" && isRefusal(final!.text);
  }

  /** A scripted question replayed in this conversation: remembered, and what it did holds for the turns after it. */
  noteReplay(email: string, conversationId: number, question: string, events: AgentEvent[]): void {
    const conversation = this.conversation(conversationId, email);
    const final = events.find((e): e is Extract<AgentEvent, { type: "final" }> => e.type === "final");
    const at = new Date(this.world.now()).toISOString();
    conversation.messages.push({ id: this.world.id(), role: "user", content: question, run_id: null, meta: {}, created_at: at, feedback: null });
    if (final) conversation.messages.push({ id: this.world.id(), role: "assistant", content: final.text, run_id: final.run_id || null, meta: { ...final.meta, engine: "recorded" }, created_at: at, feedback: null });
    conversation.previous = question;
    conversation.frame = null;
    const account = pack.accounts[email];
    for (const event of events) {
      if (event.type === "approval_required" && event.approval_id !== null) {
        const waiting = this.world.approvals.some((a) => a.tenant === account.tenant && a.tool === event.tool && a.status === "pending" && a.args.order_id === event.arguments.order_id);
        if (waiting) continue; // the same question replayed again
        const pending: ApprovalRow = { id: this.world.id(), tenant: account.tenant, runId: "", conversationId, tool: event.tool, callId: "", args: event.arguments, reason: event.reason, evidence: [], status: "pending",
          requester: email, reviewer: null, question, comment: "", createdAt: this.world.now(), decidedAt: null, recorded: true };
        this.world.approvals.push(pending);
      }
      if (event.type === "tool_result" && event.name === "handoff_to_human" && !event.is_error) {
        const ticket: TicketRow = { id: this.world.id(), tenant: account.tenant, customer: email, conversationId, title: "转人工", description: question.slice(0, 120), priority: "high", kind: "handoff", dueAt: this.world.now(), createdAt: this.world.now() };
        this.world.tickets.push(ticket);
        conversation.handoff = { ticket_id: ticket.id, status: "open", position: Number(event.output.queue_position ?? 1), assignee: null };
      }
      if (event.type === "tool_result" && event.name === "request_refund" && !event.is_error && event.output.refund_id) {
        const order = this.world.order(account.tenant, String(event.output.order_id));
        if (order) {
          order.status = "refunding";
          this.world.refunds.push({ id: this.world.id(), tenant: account.tenant, orderId: order.id, customer: order.customer, amount: Number(event.output.amount), status: "processing", createdAt: this.world.now(), reason: question, filed: true });
        }
      }
    }
  }

  // ---------------------------------------------------------------- what the console reads
  conversationRow(conversation: EngineConversation, title: string): Body {
    const updated = conversation.messages.at(-1)?.created_at ?? new Date(this.world.now()).toISOString();
    return { id: conversation.id, title, channel: "web", status: conversation.handoff ? "handoff" : "bot", created_at: conversation.messages[0]?.created_at ?? updated, updated_at: updated };
  }

  private person(email: string): Body | null {
    const account = pack.accounts[email];
    return account ? { id: account.id, name: account.name, role: account.role } : null;
  }

  approvalBody(a: ApprovalRow): Body {
    const iso = (ms: number | null) => (ms === null ? null : new Date(ms).toISOString());
    const reviewer = Object.values(pack.accounts).find((x) => x.name === a.reviewer && x.tenant === a.tenant);
    return {
      id: a.id, run_id: a.runId, conversation_id: a.conversationId, tool_name: a.tool, arguments: a.args, reason: a.reason, evidence: a.evidence, status: a.status,
      requested_by: this.person(a.requester), reviewer: reviewer ? { id: reviewer.id, name: reviewer.name } : null, question: a.question, comment: a.comment,
      required_role: this.requiredRole(a), withdrawn_by: a.status === "withdrawn" ? "customer" : null, created_at: iso(a.createdAt), decided_at: iso(a.decidedAt), refund: null, history: [], tool_off: false, simulated: true,
    };
  }

  private requiredRole(a: ApprovalRow): "admin" | "agent" {
    return a.approverRole ?? reviewerRole(this.config(a.tenant), Number(a.args.amount ?? 0));
  }

  approvals(tenant: string, filter: string): Body[] {
    return this.world.approvals.filter((a) => a.tenant === tenant && !a.recorded && (filter === "all" || a.status === filter)).sort((x, y) => y.id - x.id).map((a) => this.approvalBody(a));
  }

  /** The same refusals as app/api/approvals.py, in the same order. */
  decide(id: number, email: string, decision: string, comment: string): Body {
    const account = pack.accounts[email];
    if (account.role === "customer") throw new EngineError(403, "只有员工可以审批");
    const approval = this.world.approvals.find((a) => a.id === id && a.tenant === account.tenant && !a.recorded);
    if (!approval) throw new EngineError(404, "审批单不存在");
    if (approval.status !== "pending") throw new EngineError(409, "这笔申请已经处理过了");
    const needed = this.requiredRole(approval);
    const refuse = (status: number, text: string) => {
      noteApprovalDecision(account.role, approval.requester === email, decision, needed, status, text);
      return new EngineError(status, text);
    };
    if (approval.requester === email) throw refuse(403, "不能审批自己发起的申请（四眼原则）");
    if (decision === "reject" && !comment.trim()) throw refuse(400, "驳回必须填写原因：顾客会看到这条意见");
    const config = this.config(account.tenant);
    if (decision === "approve" && needed === "admin" && account.role !== "admin") {
      throw refuse(403, approval.approverRole ? "按这个工具的审批策略，需要管理员审批（坐席可以驳回）" : `金额超过 ¥${Math.round(config.approval_admin_threshold)}，需要管理员审批（坐席可以驳回）`);
    }
    noteApprovalDecision(account.role, false, decision, needed, 200);
    const conversation = approval.conversationId === null ? undefined : this.conversations.get(approval.conversationId);
    const { text } = resumeApproval(this.world, approval, decision === "approve" ? "approve" : "reject", { email, name: account.name, role: account.role as "admin" | "agent" }, comment, config, conversation, this.runs.get(approval.runId));
    return { approval: this.approvalBody(approval), final: { text }, delivery: null };
  }

  withdrawByStaff(id: number, email: string): Body {
    const account = pack.accounts[email];
    if (account.role === "customer") throw new EngineError(403, "只有员工可以在审批队列里撤回");
    const approval = this.world.approvals.find((a) => a.id === id && a.tenant === account.tenant && !a.recorded);
    if (!approval) throw new EngineError(404, "审批单不存在");
    if (approval.requester !== email) throw new EngineError(403, "只有提交人可以撤回这笔申请");
    if (approval.status !== "pending") throw new EngineError(409, "这笔申请已经处理过了");
    approval.status = "withdrawn";
    approval.comment = "提交人已撤回";
    approval.decidedAt = this.world.now();
    return { approval: this.approvalBody(approval) };
  }

  private runBody(run: RunRecord): Body {
    return { id: run.id, status: run.status, model: run.model, route_tier: run.route_tier, input_text: run.input_text, output_text: run.output_text, steps: run.steps,
      prompt_tokens: 0, completion_tokens: 0, cost: 0, latency_ms: run.latency_ms, evidence_coverage: run.evidence_coverage, verdict: run.verdict, is_eval: false, error: null, created_at: run.created_at,
      user: { id: pack.accounts[run.email]?.id ?? 0, name: run.name, role: run.role }, simulated: true };
  }

  runsOf(tenant: string, status: string): Body[] {
    return [...this.runs.values()].filter((r) => r.tenant === tenant && (!status || r.status === status)).reverse().map((r) => this.runBody(r));
  }

  runDetail(id: string): Body | null {
    const run = this.runs.get(id);
    return run ? { ...this.runBody(run), spans: run.spans, evidence: run.evidence, approvals: run.approvals } : null;
  }

  private ticketBody(t: TicketRow): Body {
    const customer = pack.accounts[t.customer];
    return { id: t.id, title: t.title, description: t.description, priority: t.priority, status: "open", conversation_id: t.conversationId, resolution: "", customer: customer ? { id: customer.id, name: customer.name } : null, assignee: null,
      created_at: new Date(t.createdAt).toISOString(), due_at: new Date(t.dueAt).toISOString(), closed_at: null, overdue: false, simulated: true };
  }

  ticketsOf(tenant: string): Body[] {
    return this.world.tickets.filter((t) => t.tenant === tenant && t.kind === "ticket").reverse().map((t) => this.ticketBody(t));
  }

  handoffsOf(tenant: string): Body[] {
    const queue = this.world.tickets.filter((t) => t.tenant === tenant && t.kind === "handoff");
    return queue.map((t, i) => {
      const conversation = t.conversationId === null ? undefined : this.conversations.get(t.conversationId);
      const lastUser = conversation ? [...conversation.messages].reverse().find((m) => m.role === "user") : undefined;
      return { ticket_id: t.id, status: "open", position: i + 1, assignee: null, conversation_id: t.conversationId, channel: "web", customer: pack.accounts[t.customer]?.name ?? null, summary: t.title, reason: t.description,
        priority: t.priority, created_at: new Date(t.createdAt).toISOString(), waiting_seconds: Math.max(0, Math.round((this.world.now() - t.createdAt) / 1000)), last_message: lastUser?.content ?? "", subject_staff: null, blocked: null, simulated: true };
    });
  }
}
