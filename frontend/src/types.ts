export type Role = "admin" | "agent" | "customer";

export interface User {
  id: number;
  email: string;
  name: string;
  role: Role;
  tenant: { id: number; slug: string; name: string; industry: string };
}

export interface Conversation {
  id: number;
  title: string;
  channel: string;
  status: string;
  updated_at: string;
}

export interface EvidenceItem {
  ref: number;
  kind: "kb" | "tool";
  title: string;
  text: string;
  meta: { heading?: string; relevance?: number; page?: number | null; chunk_id?: number };
}

export interface SentenceCheck {
  text: string;
  factual: boolean;
  supported: boolean;
  score: number;
  cited: number[];
  unverified_numbers: number[];
  unknown_ids: string[];
  contradicts?: string | null;
}

export interface Verification {
  verdict: "pass" | "warn" | "blocked";
  coverage: number;
  sentences: SentenceCheck[];
  invalid_citations: number[];
  method: string;
}

export interface MessageMeta {
  citations?: EvidenceItem[];
  verification?: Verification | null;
  tools?: string[];
  status?: string;
  approval_id?: number;
  agent?: string; // name of the person who wrote an "agent" message
}

export interface ChatMessage {
  id: number;
  role: "user" | "assistant" | "agent" | "system"; // agent: a person on the support team
  content: string;
  run_id: string | null;
  meta: MessageMeta;
  created_at: string;
  feedback?: { rating: string; correction: string } | null;
}

/** A conversation a person has taken over. position is null once someone is on it. */
export interface HandoffState {
  ticket_id: number;
  status: string;
  position: number | null;
  assignee: string | null;
}

export interface Handoff extends HandoffState {
  conversation_id: number;
  channel: string | null;
  customer: string | null;
  reason: string;
  priority: string;
  created_at: string;
  waiting_seconds: number;
  last_message: string;
}

export type AgentEvent =
  | { type: "run_started"; run_id: string }
  | { type: "guardrail"; name: string; matches?: string[] } // matched rules: staff only
  | { type: "rewrite"; standalone: string; method: string }
  | ({ type: "handoff" } & Partial<HandoffState>)
  | { type: "token"; step: number; text: string }
  | { type: "step_done"; step: number }
  | { type: "tool_call"; id: string; name: string; arguments: Record<string, unknown> }
  | { type: "tool_result"; id: string; name: string; output: Record<string, unknown>; is_error: boolean }
  | { type: "approval_required"; approval_id: number | null; tool: string; arguments: Record<string, unknown>; reason: string }
  | ({ type: "verification" } & Verification)
  | { type: "answer_replaced"; text: string; reason: string }
  | { type: "final"; run_id: string; message_id: number | null; text: string; status: string; meta: MessageMeta }
  | {
      type: "done";
      run_id: string;
      status: string;
      latency_ms: number;
      // Operational data, sent to staff only.
      prompt_tokens?: number;
      completion_tokens?: number;
      cost?: number;
      model?: string;
      route_tier?: string;
    }
  | { type: "error"; message: string };

export interface Approval {
  id: number;
  run_id: string;
  conversation_id: number | null;
  tool_name: string;
  arguments: Record<string, unknown>;
  reason: string;
  evidence: EvidenceItem[];
  status: "pending" | "approved" | "rejected";
  requested_by: { id: number; name: string; role: Role } | null;
  reviewer: { id: number; name: string } | null;
  question: string;
  comment: string;
  required_role: "admin" | "agent";
  created_at: string;
  decided_at: string | null;
}

/** A row of the refund ledger (GET /api/refunds). follow_up: what the reconciliation job still does
 * about it; "person" means nothing, and someone settles it from the payment system's back office;
 * "stuck" is a row left in "claiming" (a bug the job logs). */
export interface Refund {
  id: number;
  order_id: string;
  amount: number;
  status: "claiming" | "pending" | "processing" | "succeeded" | "failed";
  attempts: number;
  error: string;
  external_id: string | null;
  idempotency_key: string | null;
  ticket_id: number | null;
  approval_id: number | null;
  conversation_id: number | null;
  customer: { id: number; name: string } | null;
  created_at: string;
  updated_at: string | null;
  follow_up: "resend" | "follow" | "person" | "stuck" | "demo" | null;
  resend_until: string | null;
}

export interface DocumentRow {
  id: number;
  title: string;
  source: string;
  visibility: "public" | "internal";
  version: number;
  chunk_count: number;
  flagged_chunks: number;
  conflicts: number;
  updated_at: string;
}

export interface RunRow {
  id: string;
  status: string;
  model: string;
  route_tier: string;
  input_text: string;
  output_text: string;
  steps: number;
  prompt_tokens: number;
  completion_tokens: number;
  cost: number;
  latency_ms: number;
  evidence_coverage: number | null;
  verdict: string | null;
  is_eval: boolean;
  error: string | null;
  created_at: string;
  user: { id: number; name: string; role: Role } | null;
}

export interface SpanRow {
  id: number;
  kind: string;
  name: string;
  input: Record<string, unknown>;
  output: Record<string, unknown>;
  status: string;
  latency_ms: number;
  prompt_tokens: number;
  completion_tokens: number;
}

export interface EvalCase {
  id: number;
  key: string;
  question: string;
  role: string;
  expected_tools: string[];
  must_include: string[];
  must_not_include: string[];
  expect_refusal: boolean | null;
  expect_approval: boolean | null;
  expected_doc: string | null;
  tags: string[];
  source: string;
  status: string;
}

export interface EvalRunRow {
  id: number;
  total: number;
  passed: number;
  status: string;
  model: string;
  triggered_by: string;
  created_at: string;
  metrics: {
    pass_rate: number;
    prompt_version?: string;
    checks: Record<string, number | null>;
    by_tag: Record<string, number | null>;
    avg_evidence_coverage: number | null;
    latency_p50_ms: number;
    latency_p95_ms: number;
    total_tokens: number;
    estimated_cost: number;
    diff: { against_run: number; regressions: string[]; fixes: string[]; new_cases: string[] } | null;
  };
}
