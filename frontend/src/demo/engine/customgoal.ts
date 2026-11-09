/**
 * A goal the visitor writes themselves (pages/SimulationsPage.tsx, 自己写目标). The visitor picks an order and the outcome they
 * think is right; the product's rules may say otherwise (a ¥1299 refund waits for a person, a 30-day-old order cannot be
 * refunded online). Grading the agent against an outcome the rules never produce would call a correct refusal "动钱错误", so
 * before the round starts the rules are asked what a plain request on that order does. Not a copy of them: the engine itself
 * answers "SO… 申请退款" in a fresh copy of the data, and its end state is what the rules do.
 */
import type { AgentEvent } from "../../types";
import type { DemoEngine } from ".";
import { endState, type End, type Scenario } from "./endstate";

export type RefundRule =
  | { kind: "auto"; amount: number; note: string }
  | { kind: "review"; note: string }
  | { kind: "refused"; note: string };

export type CustomOutcome = "refund" | "review" | "ticket" | "handoff" | "nothing";

/** What a plain refund request on this order does under the rules, told the way the customer would be told it. */
export function rulesForRefund(engine: DemoEngine, email: string, orderId: string, tenant: string): RefundRule {
  const events = engine.ask(email, 1, `${orderId} 申请退款`);
  const text = events.find((e): e is Extract<AgentEvent, { type: "final" }> => e.type === "final")?.text ?? "";
  const state = endState(engine, tenant, [1]);
  const amount = state.refunds[orderId];
  if (amount !== undefined) return { kind: "auto", amount, note: `自动退款 ¥${amount.toFixed(2)}，钱直接退回` };
  if (state.approvals.pending) return { kind: "review", note: "提交人工审核，先不退钱" };
  // The refusal as the customer hears it: the sentence that says why (after-sales window, App Store, already refunded…).
  const why = text.split(/(?<=[。！？])/).find((s) => /不能|不支持|需要向|无法|暂不/.test(s))?.trim();
  return { kind: "refused", note: why ? `不能在线退款（${why.replace(/。$/, "")}）` : "不能在线退款" };
}

/** Whether the rules can end where the visitor's outcome says. Outcomes that move no money are always possible. */
export function fitsRules(outcome: CustomOutcome, rule: RefundRule): boolean {
  if (outcome === "refund") return rule.kind === "auto";
  if (outcome === "review") return rule.kind === "review";
  return true;
}

const RULE_END = (orderId: string, rule: RefundRule): End =>
  rule.kind === "auto" ? { orders: { [orderId]: "refunding" }, refunds: { [orderId]: rule.amount } } : rule.kind === "review" ? { approvals: { pending: 1 } } : {};

/** The card for a custom goal. When the outcome is one the rules never produce, the card carries what they do instead, and the
 * grade says so (endstate.grade: "rule_mismatch") rather than blaming the agent. */
export function customScenario(choice: { email: string; tenant: string; name: string; order?: { id: string; amount: number; items: string }; outcome: CustomOutcome; goal: string; label: string }, rule: RefundRule | null): Scenario {
  const { email, tenant, name, order, outcome } = choice;
  const end: End = outcome === "refund" && order ? { orders: { [order.id]: "refunding" }, refunds: { [order.id]: order.amount } }
    : outcome === "review" ? { approvals: { pending: 1 } } : outcome === "ticket" ? { tickets: 1 } : outcome === "handoff" ? { handoff: true } : {};
  const scenario: Scenario = {
    key: "custom", tenant, as: email, name, persona: "plain", card: choice.goal.trim() || `你想要的结果：${choice.label}。`,
    facts: order ? [`订单号 ${order.id}（${order.items}，¥${order.amount}）`] : [], end,
  };
  if (order && rule && !fitsRules(outcome, rule)) {
    scenario.rule = { note: rule.note, end: RULE_END(order.id, rule), chosen: choice.label };
  }
  return scenario;
}
