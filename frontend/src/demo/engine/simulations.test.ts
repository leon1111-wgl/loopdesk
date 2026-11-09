import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import type { AgentEvent } from "../../types";
import { DemoEngine } from ".";
import { endState, expectedStates, grade, moneyOutcome, tableDiff, TABLES, type End, type Scenario } from "./endstate";

/** The hidden-goal cards (backend/evals/simulations) graded by the browser's copy of app/evals/simulate.py. */
const read = (name: string) => JSON.parse(readFileSync(new URL(`../../../../backend/evals/simulations/${name}`, import.meta.url), "utf-8"));
const dev: (Scenario & { sample: string[] })[] = read("dev.json").scenarios;
const grading: { start: { orders: Record<string, string> }; cases: { name: string; scenario: { end: End; also_ok?: End[] }; actual: End; money: string; passed: boolean }[] } = read("grading_cases.json");

describe("grading cases shared with the backend", () => {
  it.each(grading.cases.map((c) => [c.name, c] as const))("%s", (_, c) => {
    const actual = { orders: { ...grading.start.orders, ...(c.actual.orders ?? {}) }, refunds: c.actual.refunds ?? {}, approvals: c.actual.approvals ?? {}, tickets: c.actual.tickets ?? 0, handoff: c.actual.handoff ?? false };
    const accepted = expectedStates(c.scenario, grading.start);
    expect(moneyOutcome(accepted, actual)).toBe(c.money);
    expect(accepted.some((e) => TABLES.every((t) => tableDiff(e, actual, grading.start.orders)[t].ok))).toBe(c.passed);
  });
});

/** What the backend reaches with each card's sample lines (tests/test_simulate.py), the browser engine reaches too. */
describe("dev cards reach their end state in the browser engine", () => {
  it.each(dev.map((s) => [s.key, s] as const))("%s", (_, scenario) => {
    const engine = new DemoEngine(() => undefined);
    const start = endState(engine, scenario.tenant, [1]);
    const said: string[] = [];
    for (const line of scenario.sample) {
      const events = engine.ask(scenario.as, 1, line);
      said.push(events.find((e): e is Extract<AgentEvent, { type: "final" }> => e.type === "final")?.text ?? "");
    }
    const result = grade(scenario, start, endState(engine, scenario.tenant, [1]), said);
    expect({ key: scenario.key, outcome: result.outcome, failed: TABLES.filter((t) => !result.tables[t].ok) }).toEqual({ key: scenario.key, outcome: "pass", failed: [] });
  });
});

/** Simulations (dev/leon_two_things, run 42664792823) found the refund offered on the wrong order when each number ends its sentence. */
describe("a number at the end of its sentence owns that sentence", () => {
  it.each([
    "我的扫地机器人物流到哪了？订单号SO20261008002。另外想退三天前收到的抱枕，订单号SO20260918001。",
    "扫地机器人到哪了 单号是SO20261008002，抱枕想退，订单号SO20260918001",
    "SO20261008002 物流到哪了，另外想退抱枕，订单号SO20260918001",
  ])("%s", (line) => {
    const engine = new DemoEngine(() => undefined);
    const events = engine.ask("customer@leon.demo", 1, line);
    const text = events.find((e): e is Extract<AgentEvent, { type: "final" }> => e.type === "final")?.text ?? "";
    expect(text).not.toContain("SO20261008002 申请退款");
    expect(text).toContain("SO20260918001 申请退款");
    expect(text).toContain("AT1402938475");
  });
});
