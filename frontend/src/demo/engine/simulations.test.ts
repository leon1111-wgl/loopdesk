import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import type { AgentEvent } from "../../types";
import { DemoEngine } from ".";
import { customScenario, rulesForRefund } from "./customgoal";
import { endState, expectedStates, grade, moneyOutcome, tableDiff, TABLES, type End, type Scenario } from "./endstate";
import { pack } from "./pack";

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
    "SO20261008002 到哪了，抱枕想退 SO20260918001 可以吗",
    "SO20261008002 到哪了，抱枕想退，订单号SO20260918001 谢谢",
    "SO20261008002 到哪了，抱枕想退，订单号是 SO20260918001 对吧",
    "SO20261008002 到哪了，抱枕想退 SO20260918001 这单",
    "SO20261008002到哪了,抱枕想退,订单号SO20260918001.",
  ])("%s", (line) => {
    const engine = new DemoEngine(() => undefined);
    const events = engine.ask("customer@leon.demo", 1, line);
    const text = events.find((e): e is Extract<AgentEvent, { type: "final" }> => e.type === "final")?.text ?? "";
    expect(text).not.toContain("SO20261008002 申请退款");
    expect(text).toContain("SO20260918001 申请退款");
    expect(text).toContain("AT1402938475");
  });
});

describe("refund words between two numbers get a question, not a guess", () => {
  it.each(["SO20261008002 到哪了，抱枕想退，SO20260918001 帮忙看看", "SO20260918001 到哪了，杯子想退，SO20261008010 帮忙看下"])("%s", (line) => {
    const engine = new DemoEngine(() => undefined);
    const events = engine.ask("customer@leon.demo", 1, line);
    const text = events.find((e): e is Extract<AgentEvent, { type: "final" }> => e.type === "final")?.text ?? "";
    expect(text).not.toContain("申请退款”即可");
    expect(text).toContain("没有写是哪一单");
    expect(events.some((e) => e.type === "approval_required")).toBe(false);
  });
});

/** The main session's probe of PR #4: a custom goal whose outcome the rules never produce graded the correct agent "动钱错误". */
describe("a custom goal is held to what the rules do with that order", () => {
  const EMMA = "customer@leon.demo";
  function play(orderId: string, outcome: "refund" | "review", label: string, line: string) {
    const order = pack.orders.leon.find((o) => o.id === orderId)!;
    const rule = rulesForRefund(new DemoEngine(() => undefined), EMMA, orderId, "leon");
    const card = customScenario({ email: EMMA, tenant: "leon", name: "Emma", order: { id: orderId, amount: order.amount, items: order.items.map((i) => i.name).join("、") }, outcome, goal: "", label }, rule);
    const engine = new DemoEngine(() => undefined);
    const start = endState(engine, "leon", [1]);
    const final = engine.ask(EMMA, 1, line).find((e): e is Extract<AgentEvent, { type: "final" }> => e.type === "final")?.text ?? "";
    return { rule, card, result: grade(card, start, endState(engine, "leon", [1]), [final]) };
  }

  it("the robot (¥1299) with 自动退款: the agent files it for review, which the rules require: not wrong money", () => {
    const { rule, result } = play("SO20261008002", "refund", "自动退款，钱退回", "扫地机器人不要了 SO20261008002 申请退款");
    expect(rule.kind).toBe("review");
    expect(result.outcome).toBe("rule_mismatch");
    expect(result.ruleKept).toBe(true);
  });

  it("the pillow (¥39.90) with 自动退款: the rules allow it, and filing it passes", () => {
    const { rule, card, result } = play("SO20260918001", "refund", "自动退款，钱退回", "SO20260918001 申请退款");
    expect(rule).toMatchObject({ kind: "auto", amount: 39.9 });
    expect(card.rule).toBeUndefined();
    expect(result.outcome).toBe("pass");
  });

  it("the 30-day tote with 自动退款: the rules refuse it, and the grade says the goal does not fit them", () => {
    const { rule, result } = play("SO20260918011", "refund", "自动退款，钱退回", "SO20260918011 申请退款");
    expect(rule.kind).toBe("refused");
    expect(rule.note).toContain("15 天");
    expect(result.outcome).toBe("rule_mismatch");
    expect(result.ruleKept).toBe(true);
  });

  it("the pillow with 提交人工审核: the rules refund it automatically, so review does not fit them either", () => {
    const { rule, result } = play("SO20260918001", "review", "提交人工审核（先不退钱）", "SO20260918001 申请退款");
    expect(rule.kind).toBe("auto");
    expect(result.outcome).toBe("rule_mismatch");
  });
});
