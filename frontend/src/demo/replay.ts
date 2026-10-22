/**
 * The static demo published on GitHub Pages. The console runs unchanged in the browser; every
 * fetch("/api/…") is answered from responses recorded from the real backend on SYNTHETIC data
 * (backend/app/ops/record_demo.py): by a real model when the committed real-model recording still
 * covers this console (backend/app/ops/demo_fixtures.py), otherwise by the offline model, recorded
 * on each deployment. The banner names the model. The scripted questions replay their recorded event
 * streams; anything that would change data is refused with a note, so nothing on the page is made up
 * and nothing is written anywhere.
 */
import type { AgentEvent } from "../types";

interface Chat {
  question: string;
  events: AgentEvent[];
  messages: { conversation: Record<string, unknown>; [key: string]: unknown } | null;
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
  messages: unknown;
}

export const isDemo = import.meta.env.VITE_DEMO === "1";
export const REPO_URL = "https://github.com/leon1111-wgl/loopdesk";
const NOT_SAVED = "这是静态演示，不会保存修改。完整流程（审批、人工接待、工单、知识库编辑……）请在本地运行：git clone 之后 docker compose up。";
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

function answer(path: string, init: RequestInit): Promise<Response> {
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

  if (method === "GET") {
    if (path.startsWith("/api/export/")) return json({ detail: NO_EXPORT }, 403);
    const own = /^\/api\/conversations\/(\d+)\/messages$/.exec(path);
    if (own && local.has(Number(own[1]))) return json(local.get(Number(own[1]))!.messages);
    if (path === "/api/conversations") {
      const mine = [...local.values()].filter((c) => c.email === email).map((c) => c.conversation).reverse();
      return json([...mine, ...((recorded(path) as unknown[]) ?? [])]);
    }
    const hit = recorded(path) ?? recorded(path.split("?")[0]);
    return hit !== undefined ? json(hit) : json({ detail: "静态演示没有录制这一页的数据" }, 404);
  }

  if (method === "POST" && path === "/api/auth/logout") return json({ signed_out: true });
  if (method === "POST" && path === "/api/conversations") {
    const now = new Date().toISOString();
    const conversation = { id: nextId++, title: "新对话", channel: "web", status: "bot", created_at: now, updated_at: now };
    local.set(conversation.id, { email, conversation, messages: { conversation, handoff: null, messages: [] } });
    return json(conversation);
  }
  const stream = /^\/api\/conversations\/(\d+)\/stream$/.exec(path);
  if (method === "POST" && stream) return replay(email, Number(stream[1]), String(body(init).content ?? ""));
  if (method === "POST" && /^\/api\/messages\/\d+\/feedback$/.test(path)) return json({ detail: NOT_SAVED }, 403);
  return json({ detail: NOT_SAVED }, 403);
}

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function replay(email: string, conversationId: number, content: string): Promise<Response> {
  const question = content.trim();
  const chat = data!.chats[email]?.find((c) => c.question === question);
  const conversation = local.get(conversationId);
  const events: AgentEvent[] = chat ? chat.events : unrecorded(email, question);
  if (conversation) {
    conversation.conversation = { ...conversation.conversation, title: question.slice(0, 30) };
    conversation.messages = chat?.messages
      ? { ...chat.messages, conversation: { ...chat.messages.conversation, id: conversationId } }
      : {
          conversation: conversation.conversation,
          handoff: null,
          messages: [
            { id: nextId++, role: "user", content: question, run_id: null, meta: {}, created_at: new Date().toISOString(), feedback: null },
            { id: nextId++, role: "assistant", content: (events[0] as { text?: string }).text ?? "", run_id: null,
              meta: { demo: true }, created_at: new Date().toISOString(), feedback: null },
          ],
        };
  }
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
  return Promise.resolve(new Response(stream, { headers: { "Content-Type": "text/event-stream" } }));
}

function unrecorded(email: string, question: string): AgentEvent[] {
  const choices = demoQuestions(email).map((q) => `· ${q}`).join("\n");
  const text = `这是静态演示，只能回放录制好的问题（“${question.slice(0, 20)}”没有录制）。可以试试：\n${choices}\n\n`
    + "完整版本在本地运行，可以问任何问题。";
  return [
    { type: "final", run_id: null, message_id: null, text, status: "completed", meta: { citations: [], demo: true } },
    { type: "done", run_id: null, status: "completed", latency_ms: 0 },
  ] as unknown as AgentEvent[];
}
