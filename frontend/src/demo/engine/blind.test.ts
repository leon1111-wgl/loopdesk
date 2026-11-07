import { readFileSync, writeFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { scoreCase, type GateCase } from "./gate-harness";

/**
 * The blind sets (backend/evals/tier1_blind*.jsonl): written by another model that read only the documents and orders, and
 * committed before they were measured. All three have been read since, so they are floors against regressions now: what they
 * measured the first time is in docs/EVALUATION.md. BLIND_REPORT=dir writes the per-case results.
 */
const FLOORS = { "tier1_blind.jsonl": { all: 65, money: 23 }, "tier1_blind_v2.jsonl": { all: 69, money: 29 }, "tier1_blind_v3.jsonl": { all: 71, money: 28 }, "tier1_money_v4.jsonl": { all: 39, money: 39 }, "money_fresh_v5.jsonl": { all: 29, money: 29 } };

type BlindCase = GateCase & { tenant: string; kind: string };

describe.each(["tier1_blind.jsonl", "tier1_blind_v2.jsonl", "tier1_blind_v3.jsonl", "tier1_money_v4.jsonl", "money_fresh_v5.jsonl"])("the blind set %s", (file) => {
  const cases: BlindCase[] = readFileSync(new URL(`../../../../backend/evals/${file}`, import.meta.url), "utf-8").trim().split("\n").map((line: string) => JSON.parse(line));
  const results = cases.map((c) => ({ ...scoreCase(c.tenant, c), kind: c.kind, question: c.question, moves_money: c.moves_money }));
  if (process.env.BLIND_REPORT) writeFileSync(`${process.env.BLIND_REPORT}/${file}.json`, JSON.stringify(results, null, 1));
  const passed = (list: typeof results) => list.filter((r) => r.passed).length;
  const money = results.filter((r) => r.moves_money !== undefined);
  // A refund filed that should not have been is the costliest mistake there is: never tolerated.
  const wronglyMoved = money.filter((r) => r.moves_money === false && r.failed.includes("moves_money"));

  it(`stays at or above its floor (${passed(results)}/${results.length}, money ${passed(money)}/${money.length})`, () => {
    const floor = FLOORS[file as keyof typeof FLOORS];
    expect(passed(results)).toBeGreaterThanOrEqual(floor.all);
    expect(passed(money)).toBeGreaterThanOrEqual(floor.money);
  });

  it("files no money on anything that is not a request", () => {
    expect(wronglyMoved.map((r) => `${r.key}: ${r.answer}`)).toEqual([]);
  });
});
