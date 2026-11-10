import { readFileSync, writeFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { DemoEngine, EngineError } from ".";
import { recording, type DecisionRecord } from "./decisionlog";
import { ask, defaultUser, newConversation, type GateCase } from "./gate-harness";
import { World } from "./tools";

/**
 * Every decision the browser's tool gate makes on the parity sets: the browser's half of app/evals/decisions.py. The
 * policy rewrite (Cedar) was held to the recording made before it (backend/evals/policy/decisions_browser.json): the same
 * lines, or a change someone decided and wrote down. RECORD_DECISIONS=1 writes the recording instead of comparing.
 */
const read = (path: string) => readFileSync(new URL(`../../../../${path}`, import.meta.url), "utf-8");
const json = (path: string) => JSON.parse(read(path));
const OUT = new URL("../../../../backend/evals/policy/decisions_browser.json", import.meta.url);

interface Step { as?: string; say?: string; decide?: string; comment?: string; refused?: number; check?: unknown }
const journeys: { key: string; steps: Step[] }[] = json("backend/evals/journeys.json");
const dev: { key: string; as: string; sample: string[] }[] = json("backend/evals/simulations/dev.json").scenarios;
const blind: { key: string; as: string }[] = json("backend/evals/simulations/blind_v1.json").scenarios;
const report: { runs: { key: string; set: string; seed: number; transcript: { role: string; text: string; sent?: boolean }[] }[] } = json("demo/simulations/report.json");
const lists: { adversarial: string[]; plain: string[]; multi_turn: { email: string; turns: string[] }[] } = json("backend/evals/money_review_lists.json");
const probes: { fileNothing: string[][]; filesTheRequest: string[][] } = JSON.parse(readFileSync(new URL("./review_probes.json", import.meta.url), "utf-8"));
const mainProbe: { probes: { line: string }[] } = json("backend/evals/policy/probe_r2_main.json");
const coverage: { tenant: string; plain: string; colloquial: string }[] = JSON.parse(readFileSync(new URL("./coverage.json", import.meta.url), "utf-8"));
const SETS = ["tier1_blind.jsonl", "tier1_blind_v2.jsonl", "tier1_blind_v3.jsonl", "tier1_money_v4.jsonl", "money_fresh_v5.jsonl"];
const CUSTOMER: Record<string, string> = { leon: "customer@leon.demo", echo: "customer@echo.demo", iris: "customer@iris.demo" };

function journey(steps: Step[]): void {
  const engine = new DemoEngine(() => undefined);
  const conversations = new Map<string, number>();
  let next = 1;
  for (const step of steps) {
    if (step.say !== undefined) {
      if (!conversations.has(step.as!)) conversations.set(step.as!, next++);
      engine.ask(step.as!, conversations.get(step.as!)!, step.say);
    } else if (step.decide) {
      const ids = [...conversations.values()];
      const target = [...engine.world.approvals].reverse().find((a) => a.conversationId !== null && ids.includes(a.conversationId));
      if (!target) continue;
      try {
        engine.decide(target.id, step.as!, step.decide, step.comment ?? "");
      } catch (error) {
        if (!(error instanceof EngineError)) throw error;
      }
    }
  }
}

function conversation(email: string, lines: string[]): void {
  const engine = new DemoEngine(() => undefined);
  for (const line of lines) engine.ask(email, 1, line);
}

function turns(email: string, lines: string[]): void {
  const world = new World();
  const c = newConversation(world, email);
  for (const line of lines) ask(world, c, email, line);
}

export function recordAll(): Record<string, DecisionRecord[]> {
  const out: Record<string, DecisionRecord[]> = {};
  const add = (key: string, run: () => void) => {
    out[key] = recording(run)[1];
  };
  for (const j of journeys) add(`journeys/${j.key}`, () => journey(j.steps));
  for (const card of dev) add(`samples/${card.key}`, () => conversation(card.as, card.sample));
  const cards = new Map([...dev, ...blind].map((c) => [c.key, c]));
  for (const run of report.runs) {
    const lines = run.transcript.filter((t) => t.role === "customer" && t.sent).map((t) => t.text);
    add(`transcripts/${run.set}/${run.key}/seed${run.seed}`, () => conversation(cards.get(run.key)!.as, lines));
  }
  lists.adversarial.forEach((text, i) => add(`money/adversarial/${i}`, () => turns(CUSTOMER.leon, [text])));
  lists.plain.forEach((text, i) => add(`money/plain/${i}`, () => turns(CUSTOMER.leon, [text])));
  lists.multi_turn.forEach((c, i) => add(`money/multi_turn/${i}`, () => turns(c.email, c.turns)));
  for (const file of SETS) {
    const cases: (GateCase & { tenant: string })[] = read(`backend/evals/${file}`).trim().split("\n").map((line) => JSON.parse(line));
    cases.forEach((c, i) => add(`money/${file}/${i}`, () => turns(c.as_user ?? defaultUser(c.tenant, c.role), [...(c.history ?? []), c.question])));
  }
  mainProbe.probes.forEach((p, i) => add(`money/probe_r2_main/${i}`, () => turns(CUSTOMER.leon, [p.line])));
  [...probes.fileNothing, ...probes.filesTheRequest].forEach((t, i) => add(`money/review_probes/${i}`, () => turns(CUSTOMER.leon, t)));
  coverage.forEach((c, i) => {
    add(`money/coverage/${i}/plain`, () => turns(CUSTOMER[c.tenant], [c.plain]));
    add(`money/coverage/${i}/colloquial`, () => turns(CUSTOMER[c.tenant], [c.colloquial]));
  });
  return out;
}

export function counts(flat: Record<string, DecisionRecord[]>) {
  const byEffect: Record<string, number> = {};
  for (const records of Object.values(flat)) for (const r of records) byEffect[r.effect] = (byEffect[r.effect] ?? 0) + 1;
  return { cases: Object.keys(flat).length, decisions: Object.values(flat).reduce((n, r) => n + r.length, 0), by_effect: Object.fromEntries(Object.entries(byEffect).sort()) };
}

const sortKeys = (r: DecisionRecord) => Object.fromEntries(Object.entries(r).sort(([a], [b]) => (a < b ? -1 : 1)));
const dumps = (flat: Record<string, DecisionRecord[]>) =>
  `{\n${Object.entries(flat).map(([key, records]) => `${JSON.stringify(key)}: ${JSON.stringify(records.map(sortKeys))}`).join(",\n")}\n}\n`;

describe("the tool gate decides on the parity sets what it decided before the policy rewrite", () => {
  const found = recordAll();
  if (process.env.RECORD_DECISIONS) writeFileSync(OUT, dumps(found));
  const expected: Record<string, DecisionRecord[]> = JSON.parse(readFileSync(OUT, "utf-8"));

  it(`records the same decisions (${JSON.stringify(counts(found))})`, () => {
    const differ = Object.keys({ ...expected, ...found }).filter((key) => JSON.stringify((expected[key] ?? []).map(sortKeys)) !== JSON.stringify((found[key] ?? []).map(sortKeys)));
    expect(differ.map((key) => ({ key, expected: expected[key], found: found[key] })).slice(0, 10)).toEqual([]);
    expect(counts(found)).toEqual(counts(expected));
  });
});
