import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import type { AgentEvent } from "../../types";
import { DemoEngine, EngineError } from ".";
import { pack } from "./pack";

/** The same journeys as the backend (backend/evals/journeys.json, app/evals/journeys.py): the same turns must end in the same state. */
interface Journey {
  key: string;
  tenant: string;
  steps: { as?: string; say?: string; says?: string; approval?: boolean; decide?: string; comment?: string; refused?: number; check?: End }[];
  end: End;
}
interface End {
  orders?: Record<string, string>;
  refunds?: Record<string, number>;
  approvals?: Record<string, number>;
  tickets?: number;
  handoff?: boolean;
  last_message?: string;
}

const journeys: Journey[] = JSON.parse(readFileSync(new URL("../../../../backend/evals/journeys.json", import.meta.url), "utf-8"));
const says = (answer: string, expected: string) => expected.split("|").some((option) => answer.replace(/\s/g, "").includes(option.replace(/\s/g, "")));

function endState(engine: DemoEngine, tenant: string, conversations: Map<string, number>): Required<End> {
  const world = engine.world;
  const ids = [...conversations.values()];
  const approvals: Record<string, number> = {};
  for (const a of world.approvals.filter((x) => x.tenant === tenant && x.conversationId !== null && ids.includes(x.conversationId))) approvals[a.status] = (approvals[a.status] ?? 0) + 1;
  const last = ids.flatMap((id) => engine.conversations.get(id)?.messages ?? []).sort((a, b) => a.id - b.id).at(-1);
  return {
    orders: Object.fromEntries((pack.orders[tenant] ?? []).map((o) => [o.id, world.order(tenant, o.id)!.status])),
    refunds: Object.fromEntries(world.refunds.filter((r) => r.tenant === tenant && r.filed).map((r) => [r.orderId, r.amount])),
    approvals,
    tickets: world.tickets.filter((t) => t.tenant === tenant && t.kind === "ticket" && t.conversationId !== null && ids.includes(t.conversationId)).length,
    handoff: ids.some((id) => engine.conversations.get(id)?.handoff),
    last_message: last?.content ?? "",
  };
}

function compare(expected: End, actual: Required<End>): string[] {
  const wrong: string[] = [];
  for (const [order, status] of Object.entries(expected.orders ?? {})) if (actual.orders[order] !== status) wrong.push(`order ${order}: ${actual.orders[order]}, expected ${status}`);
  if (expected.refunds && JSON.stringify(sorted(actual.refunds)) !== JSON.stringify(sorted(expected.refunds))) wrong.push(`refunds: ${JSON.stringify(actual.refunds)}`);
  if (expected.approvals && JSON.stringify(sorted(actual.approvals)) !== JSON.stringify(sorted(expected.approvals))) wrong.push(`approvals: ${JSON.stringify(actual.approvals)}`);
  if (expected.tickets !== undefined && actual.tickets !== expected.tickets) wrong.push(`tickets: ${actual.tickets}`);
  if (expected.handoff !== undefined && actual.handoff !== expected.handoff) wrong.push(`handoff: ${actual.handoff}`);
  if (expected.last_message && !actual.last_message.includes(expected.last_message)) wrong.push(`last message: ${actual.last_message.slice(0, 80)}`);
  return wrong;
}

const sorted = (o: Record<string, unknown>) => Object.entries(o).sort(([a], [b]) => a.localeCompare(b));

describe("journeys end where the backend's do", () => {
  it.each(journeys.map((j) => [j.key, j] as const))("%s", (_, journey) => {
    const engine = new DemoEngine(() => undefined);
    const conversations = new Map<string, number>();
    let next = 1;
    const wrong: string[] = [];
    journey.steps.forEach((step, i) => {
      const where = `step ${i + 1}`;
      if (step.check) {
        wrong.push(...compare(step.check, endState(engine, journey.tenant, conversations)).map((w) => `${where}: ${w}`));
      } else if (step.say !== undefined) {
        if (!conversations.has(step.as!)) conversations.set(step.as!, next++);
        const events = engine.ask(step.as!, conversations.get(step.as!)!, step.say);
        const answer = events.find((e): e is Extract<AgentEvent, { type: "final" }> => e.type === "final")?.text ?? "";
        if (step.says && !says(answer, step.says)) wrong.push(`${where}: said ${answer.slice(0, 120)}`);
        if (step.approval !== undefined && events.some((e) => e.type === "approval_required") !== step.approval) wrong.push(`${where}: approval ${!step.approval}`);
      } else {
        const ids = [...conversations.values()];
        const target = [...engine.world.approvals].reverse().find((a) => a.conversationId !== null && ids.includes(a.conversationId));
        if (!target) {
          wrong.push(`${where}: nothing to decide`);
          return;
        }
        try {
          engine.decide(target.id, step.as!, step.decide!, step.comment ?? "");
          if (step.refused) wrong.push(`${where}: not refused`);
        } catch (error) {
          if (!(error instanceof EngineError) || error.status !== step.refused) throw error;
        }
      }
    });
    wrong.push(...compare(journey.end, endState(engine, journey.tenant, conversations)).map((w) => `end: ${w}`));
    expect(wrong).toEqual([]);
  });
});
