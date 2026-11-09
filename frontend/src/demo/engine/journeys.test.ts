import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";
import type { AgentEvent } from "../../types";
import { DemoEngine, EngineError } from ".";
import { compare, endState as endStateOf, type End } from "./endstate";

/** The same journeys as the backend (backend/evals/journeys.json, app/evals/journeys.py): the same turns must end in the same state. */
interface Journey {
  key: string;
  tenant: string;
  steps: { as?: string; say?: string; says?: string; approval?: boolean; decide?: string; comment?: string; refused?: number; check?: End }[];
  end: End;
}

const journeys: Journey[] = JSON.parse(readFileSync(new URL("../../../../backend/evals/journeys.json", import.meta.url), "utf-8"));
const says = (answer: string, expected: string) => expected.split("|").some((option) => answer.replace(/\s/g, "").includes(option.replace(/\s/g, "")));
const endState = (engine: DemoEngine, tenant: string, conversations: Map<string, number>) => endStateOf(engine, tenant, [...conversations.values()]);

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
