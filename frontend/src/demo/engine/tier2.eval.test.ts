import { readFileSync, writeFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import { defaultUser, scoreCase, type GateCase } from "./gate-harness";
import { pack } from "./pack";
import type { ModelChoice } from "./runtime";
import { isRefusal } from "./text";
import { buildPrompt, validateChoice } from "./tier2";

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
      const prompt = buildPrompt(c.tenant, pack.accounts[email].role, c.question);
      return { id: id(c), messages: prompt.messages, schema: prompt.schema };
    });
    writeFileSync(promptsFile!, JSON.stringify(rows));
    expect(rows.length).toBeGreaterThan(100);
  });
});

describe.runIf(outputsFile)("the model tier's answers", () => {
  it("are scored against the rules alone", () => {
    const outputs: Record<string, string> = JSON.parse(readFileSync(outputsFile!, "utf-8"));
    const summary: Record<string, unknown> = {};
    const details: unknown[] = [];
    for (const set of [...new Set(CASES.map((c) => c.set))]) {
      const cases = CASES.filter((c) => c.set === set && id(c) in outputs);
      let rules = 0, model = 0, asked = 0, used = 0, invalid = 0, won = 0, lost = 0, quotedWhereRefusalExpected = 0, wronglyFiled = 0;
      for (const c of cases) {
        const tier1 = scoreCase(c.tenant, c);
        // As on the page: the model is asked only when the rules are about to refuse.
        const refused = isRefusal(tier1.answer);
        let choice: ModelChoice | null = null;
        const tier2 = !refused ? tier1 : scoreCase(c.tenant, c, (_, email) => {
          const prompt = buildPrompt(c.tenant, pack.accounts[email].role, c.question);
          choice = validateChoice(outputs[id(c)], prompt, c.question);
          return choice;
        });
        rules += Number(tier1.passed);
        model += Number(tier2.passed);
        asked += Number(refused);
        used += Number(Boolean(choice));
        invalid += Number(refused && !choice && !/^\s*\{/.test(outputs[id(c)]));
        won += Number(tier2.passed && !tier1.passed);
        lost += Number(tier1.passed && !tier2.passed);
        if (c.expect_refusal && choice) quotedWhereRefusalExpected += 1;
        if (c.moves_money === false && tier2.failed.includes("moves_money")) wronglyFiled += 1;
        if (refused) details.push({ id: id(c), question: c.question, raw: outputs[id(c)], used: Boolean(choice), rules: tier1.passed, model: tier2.passed, failed: tier2.failed, answer: tier2.answer.slice(0, 300) });
      }
      summary[set] = { cases: cases.length, rules, model, won, lost, asked, used, invalid, quoted_where_refusal_expected: quotedWhereRefusalExpected, wrongly_filed: wronglyFiled };
    }
    writeFileSync(`${outputsFile}.report.json`, JSON.stringify({ summary, details }, null, 1));
    console.log(JSON.stringify(summary, null, 1));
    // The model reaches no tool: nothing it says can move money. This holds that to be true of the whole path.
    expect(Object.values(summary).map((s) => (s as { wrongly_filed: number }).wrongly_filed).reduce((a, b) => a + b, 0)).toBe(0);
  });
});
