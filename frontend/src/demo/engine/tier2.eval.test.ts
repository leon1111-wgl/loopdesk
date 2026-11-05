import { readFileSync, writeFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { defaultUser, scoreCase, type GateCase } from "./gate-harness";
import { pack } from "./pack";
import { buildPrompt, validateChoice } from "./tier2";
import { World } from "./tools";

/**
 * Measuring the model tier, in two passes around a real model that runs on GitHub Actions, never on a laptop
 * (.github/workflows/tier2-eval.yml, backend/app/evals/tier2_run.py):
 *   TIER2_PROMPTS=prompts.json  writes what the model would be asked on every case;
 *   TIER2_OUTPUTS=outputs.json  scores its answers with the program's validation and the same tools.
 * Without either, nothing runs here.
 */
interface EvalCase extends GateCase {
  set: string;
  tenant: string;
}

function load(file: string, set: string, tenant?: string): EvalCase[] {
  const url = new URL(`../../../../backend/evals/${file}`, import.meta.url);
  return readFileSync(url, "utf-8").trim().split("\n").map((line: string) => ({ tenant, ...JSON.parse(line), set }));
}

const CASES: EvalCase[] = [
  ...load("tier1_blind.jsonl", "blind1"), ...load("tier1_blind_v2.jsonl", "blind2"),
  ...load("leon_default.jsonl", "leon", "leon"), ...load("echo_default.jsonl", "echo", "echo"), ...load("iris_default.jsonl", "iris", "iris"),
];
const id = (c: EvalCase) => `${c.set}/${c.key}`;
const promptsFile = process.env.TIER2_PROMPTS;
const outputsFile = process.env.TIER2_OUTPUTS;

describe.runIf(promptsFile)("the model tier's prompts", () => {
  it("are written for the runner", () => {
    const rows = CASES.map((c) => {
      const email = c.as_user ?? defaultUser(c.tenant, c.role);
      const prompt = buildPrompt(new World(), c.tenant, email, pack.accounts[email].role, c.question);
      return { id: id(c), messages: prompt.messages, schema: prompt.schema };
    });
    writeFileSync(promptsFile!, JSON.stringify(rows));
    expect(rows.length).toBeGreaterThan(100);
  });
});

describe.runIf(outputsFile)("the model tier's answers", () => {
  it("are scored against the rules alone", () => {
    const outputs: Record<string, string> = JSON.parse(readFileSync(outputsFile!, "utf-8"));
    const sets = [...new Set(CASES.map((c) => c.set))];
    const summary: Record<string, unknown> = {};
    const details: unknown[] = [];
    for (const set of sets) {
      const cases = CASES.filter((c) => c.set === set && id(c) in outputs);
      let rules = 0, model = 0, invalid = 0, wronglyFiled = 0, wronglyFiledByRules = 0, won = 0, lost = 0;
      for (const c of cases) {
        const tier1 = scoreCase(c.tenant, c);
        const tier2 = scoreCase(c.tenant, c, (world, email) => {
          const prompt = buildPrompt(world, c.tenant, email, pack.accounts[email].role, c.question);
          const choice = validateChoice(outputs[id(c)], prompt, c.question);
          if (!choice) invalid += 1;
          return choice;
        });
        rules += Number(tier1.passed);
        model += Number(tier2.passed);
        won += Number(tier2.passed && !tier1.passed);
        lost += Number(tier1.passed && !tier2.passed);
        if (c.moves_money === false && tier2.failed.includes("moves_money")) wronglyFiled += 1;
        if (c.moves_money === false && tier1.failed.includes("moves_money")) wronglyFiledByRules += 1;
        details.push({ id: id(c), question: c.question, raw: outputs[id(c)], rules: tier1.passed, model: tier2.passed, failed: tier2.failed, answer: tier2.answer.slice(0, 300) });
      }
      summary[set] = { cases: cases.length, rules, model, won, lost, invalid, wrongly_filed_model: wronglyFiled, wrongly_filed_rules: wronglyFiledByRules };
    }
    writeFileSync(`${outputsFile}.report.json`, JSON.stringify({ summary, details }, null, 1));
    console.log(JSON.stringify(summary, null, 1));
    // The one thing a model must never change: money does not move on what is not a request.
    expect(Object.values(summary).map((s) => (s as { wrongly_filed_model: number }).wrongly_filed_model).reduce((a, b) => a + b, 0)).toBe(0);
  });
});
