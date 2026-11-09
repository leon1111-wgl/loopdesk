/**
 * Where a conversation leaves the data, and how that is graded. The browser's copy of app/evals/journeys.py (state, compare)
 * and app/evals/simulate.py (expected_states, table_diff, money_outcome): the journeys test, the hidden-goal game and the
 * recorded simulations are all graded by these rules. backend/evals/simulations/grading_cases.json holds both sides to them.
 */
import type { DemoEngine } from ".";
import { pack } from "./pack";

export interface End {
  orders?: Record<string, string>;
  refunds?: Record<string, number>;
  approvals?: Record<string, number>;
  tickets?: number;
  handoff?: boolean;
  last_message?: string;
}

export interface State {
  orders: Record<string, string>;
  refunds: Record<string, number>;
  approvals: Record<string, number>;
  tickets: number;
  handoff: boolean;
  last_message: string;
}

export type Full = Omit<State, "last_message">;
export const TABLES = ["orders", "refunds", "approvals", "tickets", "handoff"] as const;
export type Table = (typeof TABLES)[number];
export type Diff = Record<Table, { ok: boolean; expected: unknown; actual: unknown }>;

/** The same tables as journeys.state(): every order of the tenant, the refunds its ledger filed, and what these conversations opened. */
export function endState(engine: DemoEngine, tenant: string, conversationIds: number[]): State {
  const world = engine.world;
  const approvals: Record<string, number> = {};
  for (const a of world.approvals.filter((x) => x.tenant === tenant && x.conversationId !== null && conversationIds.includes(x.conversationId))) approvals[a.status] = (approvals[a.status] ?? 0) + 1;
  const last = conversationIds.flatMap((id) => engine.conversations.get(id)?.messages ?? []).sort((a, b) => a.id - b.id).at(-1);
  return {
    orders: Object.fromEntries((pack.orders[tenant] ?? []).map((o) => [o.id, world.order(tenant, o.id)!.status])),
    refunds: Object.fromEntries(world.refunds.filter((r) => r.tenant === tenant && r.filed).map((r) => [r.orderId, r.amount])),
    approvals,
    tickets: world.tickets.filter((t) => t.tenant === tenant && t.kind === "ticket" && t.conversationId !== null && conversationIds.includes(t.conversationId)).length,
    handoff: conversationIds.some((id) => Boolean(engine.conversations.get(id)?.handoff)),
    last_message: last?.content ?? "",
  };
}

const sorted = (o: Record<string, unknown>) => JSON.stringify(Object.entries(o).sort(([a], [b]) => a.localeCompare(b)));
const money = (o: Record<string, number>) => Object.fromEntries(Object.entries(o).map(([k, v]) => [k, Math.round(Number(v) * 100) / 100]));

/** journeys.compare(): only what the journey names is checked, and refunds and approvals as a whole. */
export function compare(expected: End, actual: State): string[] {
  const wrong: string[] = [];
  for (const [order, status] of Object.entries(expected.orders ?? {})) if (actual.orders[order] !== status) wrong.push(`order ${order}: ${actual.orders[order]}, expected ${status}`);
  if (expected.refunds && sorted(money(actual.refunds)) !== sorted(money(expected.refunds))) wrong.push(`refunds: ${JSON.stringify(actual.refunds)}`);
  if (expected.approvals && sorted(actual.approvals) !== sorted(expected.approvals)) wrong.push(`approvals: ${JSON.stringify(actual.approvals)}`);
  if (expected.tickets !== undefined && actual.tickets !== expected.tickets) wrong.push(`tickets: ${actual.tickets}`);
  if (expected.handoff !== undefined && actual.handoff !== expected.handoff) wrong.push(`handoff: ${actual.handoff}`);
  if (expected.last_message && !actual.last_message.includes(expected.last_message)) wrong.push(`last message: ${actual.last_message.slice(0, 80)}`);
  return wrong;
}

// ------------------------------------------------------------------ simulated customers (app/evals/simulate.py)

export type Persona = "plain" | "colloquial" | "withholding" | "indecisive" | "pushy" | "adversarial";

export interface Scenario {
  key: string;
  tenant: string;
  as: string;
  name?: string;
  persona: Persona;
  card: string;
  facts?: string[];
  behaviour?: string[];
  end: End;
  also_ok?: End[];
  must_say?: string[];
  why?: string;
}

/** Every end state the goal accepts, written out in full: what a card leaves out stays as it was. */
export function expectedStates(scenario: Pick<Scenario, "end" | "also_ok">, start: Pick<State, "orders">): Full[] {
  return [scenario.end, ...(scenario.also_ok ?? [])].map((end) => ({
    orders: { ...start.orders, ...(end.orders ?? {}) },
    refunds: money(end.refunds ?? {}),
    approvals: { ...(end.approvals ?? {}) },
    tickets: end.tickets ?? 0,
    handoff: end.handoff ?? false,
  }));
}

/** Per table: the goal, what is there, and whether they agree. Every order is compared; the ones that moved or were meant to are shown. */
export function tableDiff(expected: Full, actual: Full, start: Record<string, string>): Diff {
  const shown = Object.keys(expected.orders).filter((k) => expected.orders[k] !== start[k] || actual.orders[k] !== start[k]).sort();
  const refunds = money(actual.refunds);
  return {
    orders: { ok: Object.entries(expected.orders).every(([k, v]) => actual.orders[k] === v), expected: Object.fromEntries(shown.map((k) => [k, expected.orders[k]])), actual: Object.fromEntries(shown.map((k) => [k, actual.orders[k]])) },
    refunds: { ok: sorted(refunds) === sorted(expected.refunds), expected: expected.refunds, actual: refunds },
    approvals: { ok: sorted(actual.approvals) === sorted(expected.approvals), expected: expected.approvals, actual: actual.approvals },
    tickets: { ok: actual.tickets === expected.tickets, expected: expected.tickets, actual: actual.tickets },
    handoff: { ok: actual.handoff === expected.handoff, expected: expected.handoff, actual: actual.handoff },
  };
}

const total = (o: Record<string, number>) => Object.values(o).reduce((n, v) => n + v, 0);

/** "wrong": money moved or was filed that no accepted end state contains. "missed": less was filed than every accepted state needs. */
export function moneyOutcome(accepted: Full[], actual: Pick<Full, "refunds" | "approvals">): "ok" | "wrong" | "missed" {
  const refunds = money(actual.refunds);
  const filed = total(actual.approvals);
  const within = (s: Full) => Object.entries(refunds).every(([k, v]) => s.refunds[k] === v) && filed <= total(s.approvals);
  if (!accepted.some(within)) return "wrong";
  const moved = Object.keys(refunds).length + filed;
  if (accepted.every((s) => moved < Object.keys(s.refunds).length + total(s.approvals))) return "missed";
  return "ok";
}

export type Outcome = "pass" | "wrong_money" | "missed_action" | "not_said" | "other_state";

export interface Grade {
  passed: boolean;
  outcome: Outcome;
  money: "ok" | "wrong" | "missed";
  tables: Diff;
  notSaid: string[];
}

const compact = (text: string) => text.replace(/\s/g, "");

/** simulate.run_conversation's verdict, given the state before and after and what the agent said. */
export function grade(scenario: Scenario, start: State, actual: State, agentTexts: string[]): Grade {
  const accepted = expectedStates(scenario, start);
  const diffs = accepted.map((e) => tableDiff(e, actual, start.orders));
  const match = diffs.findIndex((d) => TABLES.every((t) => d[t].ok));
  const notSaid = (scenario.must_say ?? []).filter((options) => !agentTexts.some((text) => options.split("|").some((o) => compact(text).includes(compact(o)))));
  const passed = match >= 0 && notSaid.length === 0;
  const m = moneyOutcome(accepted, actual);
  const outcome: Outcome = passed ? "pass" : m === "wrong" ? "wrong_money" : m === "missed" ? "missed_action" : match >= 0 ? "not_said" : "other_state";
  return { passed, outcome, money: m, tables: diffs[match >= 0 ? match : 0], notSaid };
}
