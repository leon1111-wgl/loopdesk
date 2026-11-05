/**
 * The static demo published on GitHub Pages. The console runs unchanged in the browser; every
 * fetch("/api/…") is answered from responses recorded from the real backend on SYNTHETIC data
 * (backend/app/ops/record_demo.py): by a real model when the committed real-model recording still
 * covers this console (backend/app/ops/demo_fixtures.py), otherwise by the offline model, recorded
 * on each deployment. The banner names the model.
 *
 * Which engine answers a question, and every answer says so:
 * - a scripted question replays its recorded event stream;
 * - anything else goes to the browser engine (demo/engine): rules, retrieval and tools over the same fictional data, in
 *   this tab, no server and no key. A visitor can load a small model on top of it (demo/engine/tier2.ts).
 * Changes only touch the in-tab copy, are labelled 模拟, and go away on reload. Nothing is written anywhere.
 */
import type { AgentEvent } from "../types";
import type { DemoEngine, EngineError as EngineErrorType, ModelChoice } from "./engine";
import type { LoadProgress, ModelClient } from "./engine/tier2";

interface Chat {
  question: string;
  events: AgentEvent[];
}

export interface RecordingModel {
  provider: string;
  strong: string;
  fast: string;
  offline: boolean;
}

interface Fixtures {
  recorded_at: string;
  model?: RecordingModel; // absent in recordings made before W7: the offline model
  commit?: string;
  accounts: unknown;
  users: Record<string, { role: string } & Record<string, unknown>>;
  /** account -> URL -> key into bodies (staff of one tenant share most bodies) */
  responses: Record<string, Record<string, string>>;
  bodies: Record<string, unknown>;
  chats: Record<string, Chat[]>;
}

interface LocalConversation {
  email: string;
  conversation: Record<string, unknown>;
  /** What this conversation said when the browser engine could not load (a very old browser, a failed download). */
  fallback?: Record<string, unknown>[];
}

export const isDemo = import.meta.env.VITE_DEMO === "1";
export const REPO_URL = "https://github.com/leon1111-wgl/loopdesk";
const NOT_SAVED = "这是静态演示，这项修改不会保存。对话页问的问题、提交的申请和对应的审批可以在本页模拟；完整流程请在本地运行：git clone 之后 docker compose up。";
const NO_EXPORT = "静态演示不提供导出。本地运行后可以在这里下载 CSV。";
const TOKEN_PREFIX = "demo:";

let data: Fixtures | null = null;
const local = new Map<number, LocalConversation>();
let nextId = 900_001;

export function recordedAt(): string | null {
  return data?.recorded_at ?? null;
}

/** Which model wrote the recorded answers, when and from which commit: shown in the demo's banner. */
export function recording(): { model: RecordingModel; date: string; commit: string } | null {
  if (!data) return null;
  const model = data.model ?? { provider: "mock", strong: "mock-strong", fast: "mock-fast", offline: true };
  const date = new Date(data.recorded_at).toLocaleDateString("sv-SE", { timeZone: "Asia/Shanghai" }); // YYYY-MM-DD
  return { model, date, commit: (data.commit ?? "").slice(0, 7) };
}

/** The questions an account can replay (they were asked while recording). */
export function demoQuestions(email?: string | null): string[] {
  return email && data ? (data.chats[email] ?? []).map((c) => c.question) : [];
}

// ---------------------------------------------------------------- the browser engine
let engine: DemoEngine | null = null;
let EngineError: typeof EngineErrorType | null = null;
let engineLoading: Promise<DemoEngine> | null = null;

function loadEngine(): Promise<DemoEngine> {
  engineLoading ??= import("./engine").then((module) => {
    EngineError = module.EngineError;
    engine = new module.DemoEngine((slug) => recordedTenant(slug));
    return engine;
  }).catch((error) => {
    engineLoading = null; // a failed download can be tried again on the next question
    throw error;
  });
  return engineLoading;
}

/** The tenant's limits as the console shows them: the recording's /api/tenant, not numbers in the page's code. */
function recordedTenant(slug: string): Record<string, number> | undefined {
  const admin = Object.entries(data?.users ?? {}).find(([, u]) => u.role === "admin" && (u.tenant as { slug?: string } | undefined)?.slug === slug)?.[0];
  const key = admin ? data?.responses[admin]?.["/api/tenant"] : undefined;
  return key ? (data?.bodies[key] as Record<string, number>) : undefined;
}

export type ModelState =
  | { status: "off" }
  | { status: "loading"; progress: number; text: string }
  | { status: "ready" }
  | { status: "failed"; error: string };

let modelClient: ModelClient | null = null;
let modelLoads = 0; // a load the visitor cancelled says nothing when it fails
let modelState: ModelState = { status: "off" };
const modelListeners = new Set<(state: ModelState) => void>();

function setModelState(state: ModelState) {
  modelState = state;
  modelListeners.forEach((listener) => listener(state));
}

export function currentModelState(): ModelState {
  return modelState;
}

export function watchModel(listener: (state: ModelState) => void): () => void {
  modelListeners.add(listener);
  return () => modelListeners.delete(listener);
}

/** Whether to offer the model at all: a browser with WebGPU, not a phone. Nothing is downloaded by asking. */
export async function modelAvailable(): Promise<boolean> {
  const { webgpuAvailable } = await import("./engine/tier2");
  return webgpuAvailable();
}

/** Only ever called from the visitor's click. The rules keep answering while it loads. */
export async function loadModel(): Promise<void> {
  const { ModelClient } = await import("./engine/tier2");
  modelClient ??= new ModelClient();
  const load = ++modelLoads;
  setModelState({ status: "loading", progress: 0, text: "正在准备下载" });
  try {
    await modelClient.load((p: LoadProgress) => {
      if (load === modelLoads) setModelState({ status: "loading", progress: p.progress ?? 0, text: p.text ?? "" });
    });
    if (load === modelLoads) setModelState({ status: "ready" });
  } catch (error) {
    if (load === modelLoads) setModelState({ status: "failed", error: error instanceof Error ? error.message : String(error) });
  }
}

export function stopModel(): void {
  modelLoads += 1;
  modelClient?.teardown();
  setModelState({ status: "off" });
}

/** What the model chooses for this question, validated; null: the rules answer alone. */
async function modelChoice(email: string, conversationId: number, question: string, notes: AgentEvent[]): Promise<ModelChoice | null> {
  if (modelState.status !== "ready" || !modelClient || !engine) return null;
  if (!engine.wouldRefuse(email, conversationId, question)) return null; // the rules have an answer: no need to ask
  const { buildPrompt, validateChoice } = await import("./engine/tier2");
  const user = data!.users[email];
  const prompt = buildPrompt((user.tenant as { slug: string }).slug, user.role, question);
  if (!prompt.candidates.length) return null;
  try {
    const raw = await modelClient.choose(prompt);
    const choice = validateChoice(raw, prompt, question);
    notes.push({ type: "model_choice", raw: raw.slice(0, 400), note: choice ? choice.note : "模型认为资料里没有答案，或输出不合法：由规则引擎回答", accepted: Boolean(choice) });
    return choice;
  } catch (error) {
    notes.push({ type: "model_choice", raw: "", note: error instanceof Error ? error.message : String(error), accepted: false });
    if (!modelClient.loaded) setModelState({ status: "failed", error: error instanceof Error ? error.message : String(error) });
    return null;
  }
}

export async function installReplay(): Promise<void> {
  const real = window.fetch.bind(window);
  const res = await real(`${import.meta.env.BASE_URL}demo/fixtures.json`);
  data = (await res.json()) as Fixtures;
  window.fetch = (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const path = url.startsWith("http") ? new URL(url).pathname + new URL(url).search : url;
    return path.startsWith("/api/") ? answer(path, init ?? {}) : real(input, init);
  };
}

function json(body: unknown, status = 200): Promise<Response> {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
}

function caller(init: RequestInit): string | null {
  const auth = new Headers(init.headers).get("Authorization") ?? "";
  const email = auth.startsWith(`Bearer ${TOKEN_PREFIX}`) ? auth.slice(`Bearer ${TOKEN_PREFIX}`.length) : null;
  return email && data?.users[email] ? email : null;
}

function body(init: RequestInit): Record<string, unknown> {
  try {
    return typeof init.body === "string" ? JSON.parse(init.body) : {};
  } catch {
    return {};
  }
}

async function answer(path: string, init: RequestInit): Promise<Response> {
  const fixtures = data!;
  const method = (init.method ?? "GET").toUpperCase();
  if (path === "/api/auth/demo-accounts") return json(fixtures.accounts);
  if (path === "/api/auth/login" && method === "POST") {
    const email = String(body(init).email ?? "");
    const user = fixtures.users[email];
    return user ? json({ token: `${TOKEN_PREFIX}${email}`, user }) : json({ detail: "演示里没有这个账号" }, 401);
  }
  const email = caller(init);
  if (!email) return json({ detail: "登录已失效，请重新登录" }, 401);
  const refs = fixtures.responses[email] ?? {};
  const recorded = (url: string): unknown => (url in refs ? fixtures.bodies[refs[url]] : undefined);
  const user = fixtures.users[email];
  const tenant = (user.tenant as { slug: string }).slug;
  const staff = user.role !== "customer";
  const run = engine;

  if (method === "GET") {
    if (path.startsWith("/api/export/")) return json({ detail: NO_EXPORT }, 403);
    const own = /^\/api\/conversations\/(\d+)\/messages$/.exec(path);
    if (own) {
      const id = Number(own[1]);
      const live = run?.conversations.get(id);
      if (live && !run!.mayRead(live, email)) return json({ detail: "对话不存在" }, 404);
      if (live) {
        const row = local.get(id)?.conversation ?? recordedRow(recorded, id) ?? { id, title: "新对话" };
        return json({ conversation: { ...run!.conversationRow(live, String(row.title ?? "新对话")), ...(live.handoff ? { status: "handoff" } : {}) }, handoff: live.handoff, messages: live.messages });
      }
      if (local.has(id)) return json({ conversation: local.get(id)!.conversation, handoff: null, messages: local.get(id)!.fallback ?? [] });
    }
    if (path === "/api/conversations") {
      const mine = [...local.values()].filter((c) => c.email === email).map((c) => {
        const live = run?.conversations.get(Number(c.conversation.id));
        return live ? run!.conversationRow(live, String(c.conversation.title)) : c.conversation;
      }).reverse();
      const shown = new Set(mine.map((c) => c.id));
      return json([...mine, ...((recorded(path) as { id: unknown }[]) ?? []).filter((c) => !shown.has(c.id))]);
    }
    // What this tab simulated, on top of what was recorded: the staff pages see it like anything else.
    const merged = staff && run ? mergedPage(path, run, tenant, recorded) : undefined;
    if (merged !== undefined) return json(merged);
    const hit = recorded(path) ?? recorded(path.split("?")[0]);
    return hit !== undefined ? json(hit) : json({ detail: "静态演示没有录制这一页的数据" }, 404);
  }

  if (method === "POST" && path === "/api/auth/logout") return json({ signed_out: true });
  if (method === "POST" && path === "/api/conversations") {
    const now = new Date().toISOString();
    const conversation = { id: nextId++, title: "新对话", channel: "web", status: "bot", created_at: now, updated_at: now };
    local.set(conversation.id, { email, conversation });
    return json(conversation);
  }
  const stream = /^\/api\/conversations\/(\d+)\/stream$/.exec(path);
  if (method === "POST" && stream) return replay(email, Number(stream[1]), String(body(init).content ?? ""), recorded);
  const decide = /^\/api\/approvals\/(\d+)\/(decide|withdraw)$/.exec(path);
  if (method === "POST" && decide && run?.world.approvals.some((a) => a.id === Number(decide[1]))) {
    try {
      const form = body(init);
      return json(decide[2] === "decide" ? run.decide(Number(decide[1]), email, String(form.decision ?? ""), String(form.comment ?? "")) : run.withdrawByStaff(Number(decide[1]), email));
    } catch (error) {
      if (EngineError && error instanceof EngineError) return json({ detail: error.message }, error.status);
      throw error;
    }
  }
  return json({ detail: NOT_SAVED }, 403);
}

const NO_ENGINE = "这个浏览器没能加载演示的回答引擎（版本过旧，或下载失败）。可以换一个新版浏览器或刷新后重试；录制好的示例问题不受影响。";

/** The scripted questions need nothing to replay, and anything else says what went wrong instead of failing the send. */
function withoutEngine(email: string, conversationId: number, question: string, recordedEvents: AgentEvent[] | undefined, conversation: LocalConversation | undefined): Response {
  const at = new Date().toISOString();
  const events: AgentEvent[] = recordedEvents ?? [
    { type: "final", run_id: "", message_id: null, text: NO_ENGINE, status: "completed", meta: { citations: [], status: "completed" } },
    { type: "done", run_id: "", status: "completed", latency_ms: 0 },
  ];
  const final = events.find((e): e is Extract<AgentEvent, { type: "final" }> => e.type === "final");
  const own = conversation ?? { email, conversation: { id: conversationId, title: question.slice(0, 30), channel: "web", status: "bot", created_at: at, updated_at: at } };
  local.set(conversationId, own);
  own.fallback = [...(own.fallback ?? []),
    { id: nextId++, role: "user", content: question, run_id: null, meta: {}, created_at: at, feedback: null },
    { id: nextId++, role: "assistant", content: final?.text ?? "", run_id: null, meta: { ...final?.meta, engine: recordedEvents ? "recorded" : "unavailable" }, created_at: at, feedback: null }];
  return sse(events);
}

function sse(events: AgentEvent[]): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    async start(controller) {
      for (const event of events) {
        await sleep(event.type === "token" ? 15 : 90);
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(event)}\n\n`));
      }
      controller.close();
    },
  });
  return new Response(stream, { headers: { "Content-Type": "text/event-stream" } });
}

function recordedRow(recorded: (url: string) => unknown, id: number): Record<string, unknown> | undefined {
  return ((recorded("/api/conversations") as Record<string, unknown>[]) ?? []).find((c) => c.id === id);
}

function mergedPage(path: string, run: DemoEngine, tenant: string, recorded: (url: string) => unknown): unknown {
  const [page, query = ""] = path.split("?");
  const params = new URLSearchParams(query);
  const list = (extra: unknown[]) => [...extra, ...((recorded(path) as unknown[]) ?? [])];
  if (page === "/api/approvals") return list(run.approvals(tenant, params.get("status_filter") ?? "pending"));
  if (page === "/api/tickets") return list(run.ticketsOf(tenant));
  if (page === "/api/handoffs") return list(run.handoffsOf(tenant));
  if (page === "/api/runs") {
    const items = run.runsOf(tenant, params.get("status") ?? "");
    const base = recorded(path) as { items?: unknown[] } | undefined;
    return { ...base, items: [...items, ...(base?.items ?? [])] };
  }
  const detail = /^\/api\/runs\/([0-9a-f]{32})$/.exec(page);
  if (detail) return run.runDetail(detail[1]) ?? undefined;
  return undefined;
}

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function replay(email: string, conversationId: number, content: string, recorded: (url: string) => unknown): Promise<Response> {
  const question = content.trim();
  const chat = data!.chats[email]?.find((c) => c.question === question);
  const conversation = local.get(conversationId);
  if (conversation && conversation.conversation.title === "新对话") conversation.conversation = { ...conversation.conversation, title: question.slice(0, 30) };
  let run: DemoEngine;
  try {
    run = await loadEngine();
  } catch {
    return withoutEngine(email, conversationId, question, chat?.events, conversation);
  }
  // A recorded conversation the visitor goes on with keeps what it said; it becomes this tab's from here on.
  if (!run.conversations.has(conversationId)) {
    const body = recorded(`/api/conversations/${conversationId}/messages`) as { messages?: Record<string, unknown>[]; conversation?: Record<string, unknown> } | undefined;
    run.conversation(conversationId, email, body?.messages);
    if (!local.has(conversationId) && body?.conversation) local.set(conversationId, { email, conversation: body.conversation });
  }
  const notes: AgentEvent[] = [];
  let events: AgentEvent[];
  if (chat) {
    run.noteReplay(email, conversationId, question, chat.events);
    events = chat.events.map((e) => (e.type === "final" ? { ...e, meta: { ...e.meta, engine: "recorded" } } : e));
  } else {
    const choice = await modelChoice(email, conversationId, question, notes);
    events = [...notes, ...run.ask(email, conversationId, question, choice)];
  }
  return sse(events);
}
