import type { AgentEvent } from "./types";

const TOKEN_KEY = "loopdesk.token";

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string | null) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* storage unavailable: token lives only in memory for this tab */
  }
}

export class ApiError extends Error {
  /** body: the parsed JSON error, for endpoints that return more than a message (e.g. a conflict list). */
  constructor(public status: number, message: string, public body?: Record<string, unknown>, cause?: unknown) {
    super(message, cause === undefined ? undefined : { cause });
  }
}

/** status 0: the request never got an answer (offline, connection reset, a proxy that dropped it). */
export const NETWORK_ERROR = "网络连接失败，请检查网络后重试。";

/** fetch() rejects with a bare TypeError ("Failed to fetch") when there is no response at all;
 * turn that into a Chinese message. A cancelled request (AbortError) is passed through as it is. */
function request(input: string, init?: RequestInit): Promise<Response> {
  return fetch(input, init).catch((e: unknown) => {
    if (e instanceof DOMException && e.name === "AbortError") throw e;
    throw new ApiError(0, NETWORK_ERROR, undefined, e);
  });
}

function headers(extra?: HeadersInit): HeadersInit {
  const token = getToken();
  return { ...(token ? { Authorization: `Bearer ${token}` } : {}), ...extra };
}

async function parse<T>(res: Response): Promise<T> {
  if (res.ok) return (await res.json()) as T;
  let message = `请求失败（${res.status}）`;
  let body: Record<string, unknown> | undefined;
  try {
    body = await res.json();
    if (typeof body?.detail === "string") message = body.detail;
    else if (Array.isArray(body?.detail)) message = (body.detail as { msg: string }[]).map((d) => d.msg).join("；");
  } catch {
    /* non-JSON error body */
  }
  if (res.status === 401) {
    setToken(null);
    window.dispatchEvent(new Event("loopdesk:logout"));
  }
  throw new ApiError(res.status, message, body);
}

export const api = {
  get: <T,>(path: string) => request(path, { headers: headers() }).then((r) => parse<T>(r)),
  post: <T,>(path: string, body?: unknown) =>
    request(path, {
      method: "POST",
      headers: headers({ "Content-Type": "application/json" }),
      body: JSON.stringify(body ?? {}),
    }).then((r) => parse<T>(r)),
  patch: <T,>(path: string, body: unknown) =>
    request(path, {
      method: "PATCH",
      headers: headers({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
    }).then((r) => parse<T>(r)),
  put: <T,>(path: string, body: unknown) =>
    request(path, {
      method: "PUT",
      headers: headers({ "Content-Type": "application/json" }),
      body: JSON.stringify(body),
    }).then((r) => parse<T>(r)),
  del: <T,>(path: string) => request(path, { method: "DELETE", headers: headers() }).then((r) => parse<T>(r)),
  upload: <T,>(path: string, form: FormData) =>
    request(path, { method: "POST", headers: headers(), body: form }).then((r) => parse<T>(r)),
  /** Authenticated file download (a plain link cannot send the Authorization header). */
  download: async (path: string, fallbackName: string) => {
    const res = await request(path, { headers: headers() });
    if (!res.ok) await parse(res);
    const name = /filename="([^"]+)"/.exec(res.headers.get("content-disposition") ?? "")?.[1] ?? fallbackName;
    const url = URL.createObjectURL(await res.blob());
    const a = document.createElement("a");
    a.href = url;
    a.download = name;
    a.click();
    URL.revokeObjectURL(url);
  },
};

/** POST + Server-Sent Events. EventSource cannot send a body or an Authorization
 * header, so the stream is read from fetch() and split on blank lines. */
export async function streamChat(
  conversationId: number,
  content: string,
  options: { inject_hallucination?: boolean },
  onEvent: (event: AgentEvent) => void,
  signal?: AbortSignal,
  onAccepted?: () => void,
): Promise<void> {
  const res = await request(`/api/conversations/${conversationId}/stream`, {
    method: "POST",
    headers: headers({ "Content-Type": "application/json" }),
    body: JSON.stringify({ content, options }),
    signal,
  });
  if (!res.ok || !res.body) {
    await parse(res);
    return;
  }
  // A 200 means the run was admitted and has started on the server: the message will be stored.
  onAccepted?.();
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    let chunk: ReadableStreamReadResult<Uint8Array>;
    try {
      chunk = await reader.read();
    } catch (e) {
      if (e instanceof DOMException && e.name === "AbortError") throw e;
      throw new ApiError(0, NETWORK_ERROR, undefined, e); // the connection dropped part-way through the answer
    }
    const { value, done } = chunk;
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let boundary = buffer.indexOf("\n\n");
    while (boundary !== -1) {
      const block = buffer.slice(0, boundary);
      buffer = buffer.slice(boundary + 2);
      for (const line of block.split("\n")) {
        if (line.startsWith("data:")) onEvent(JSON.parse(line.slice(5).trim()) as AgentEvent);
      }
      boundary = buffer.indexOf("\n\n");
    }
  }
}
