/**
 * What the tool gate decided, call by call, for the policy parity check (decisions.test.ts): the browser's copy of
 * app/agent/decision_log.py. Nothing is recorded unless a recording is open. Ids and clock times become placeholders,
 * so two runs of the same code record the same lines.
 */
export interface DecisionRecord {
  tool: string;
  role: string;
  effect: string;
  detail: string;
  order?: string | null;
  amount?: string | null;
  approver?: string | null;
  own?: boolean;
  required?: string;
}

const sinks: DecisionRecord[][] = [];
const CLOCK = /\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?|\d{1,2}:\d{2}/g;
const IDS = /#\d+|V-\d+/g;

export const stable = (text: unknown): string => String(text ?? "").replace(CLOCK, "<t>").replace(IDS, "#<id>");

const amountOf = (value: unknown): string | null => {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n.toFixed(2) : String(value);
};

export function recording<T>(run: () => T): [T, DecisionRecord[]] {
  const records: DecisionRecord[] = [];
  sinks.push(records);
  try {
    return [run(), records];
  } finally {
    sinks.splice(sinks.indexOf(records), 1);
  }
}

export function noteToolCall(tool: string, role: string, args: Record<string, unknown>, kind: "result" | "approval", output: Record<string, unknown> | null, isError: boolean, reason = "", approver: string | null = null): void {
  const sink = sinks.at(-1);
  if (!sink) return;
  const out = output ?? {};
  let effect = "allow";
  let detail: unknown = "";
  if (kind === "approval") [effect, detail] = ["review", reason];
  else if (out.confirm_order) [effect, detail] = ["ask", out.error];
  else if (out.awaiting_review) [effect, detail] = ["waiting", out.error];
  else if (isError) [effect, detail] = ["deny", out.error];
  sink.push({ tool, role, order: args.order_id ? String(args.order_id) : null, amount: amountOf(args.amount), effect, detail: stable(detail), approver });
}

export function noteApprovalDecision(role: string, own: boolean, decision: string, required: string, status: number, detail = ""): void {
  sinks.at(-1)?.push({ tool: `approval.${decision}`, role, own, required, effect: status === 200 ? "allow" : `deny:${status}`, detail: stable(detail) });
}
