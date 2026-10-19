import { useCallback, useEffect, useState } from "react";
import { Send, Undo2 } from "lucide-react";
import { api } from "../api";
import { CHANNEL_TEXT, Empty, ErrorBanner, time, useLoad } from "../components/ui";
import type { ChatMessage, Handoff } from "../types";

function waited(seconds: number) {
  if (seconds < 60) return "不到 1 分钟";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟`;
  return `${Math.floor(seconds / 3600)} 小时 ${Math.floor((seconds % 3600) / 60)} 分钟`;
}

function HandoffPanel({ handoff, onChanged }: { handoff: Handoff; onChanged: () => void }) {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const load = useCallback(async () => {
    const r = await api.get<{ messages: ChatMessage[] }>(`/api/conversations/${handoff.conversation_id}/messages`);
    setMessages(r.messages);
  }, [handoff.conversation_id]);

  // The customer can keep writing while waiting; refresh so new messages show up.
  useEffect(() => {
    load().catch((e) => setError(e.message));
    const timer = window.setInterval(() => load().catch(() => undefined), 4000);
    return () => window.clearInterval(timer);
  }, [load]);

  async function act(path: "reply" | "close") {
    setBusy(true);
    setError(null);
    try {
      await api.post(`/api/handoffs/${handoff.ticket_id}/${path}`, path === "reply" ? { content: text } : {});
      if (path === "reply") {
        setText("");
        await load();
      }
      onChanged();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const speaker = (m: ChatMessage) =>
    m.role === "user" ? handoff.customer : m.role === "agent" ? `人工客服 ${m.meta.agent ?? ""}` : m.role === "system" ? "系统" : "智能客服";
  return (
    <div className="card handoff-panel">
      <div className="card-head">
        <div className="row">
          <b>{handoff.customer}</b>
          <span className="badge">{CHANNEL_TEXT[handoff.channel ?? ""] ?? handoff.channel}</span>
          {handoff.assignee ? <span className="badge badge-info">{handoff.assignee} 跟进中</span> : <span className="badge badge-warning">等待接入</span>}
        </div>
        <button className="btn btn-sm" disabled={busy} onClick={() => act("close")}>
          <Undo2 size={14} /> 结束人工服务
        </button>
      </div>
      <div className="card-pad handoff-transcript" aria-live="polite">
        {messages.map((m) => (
          <div key={m.id} className={`transcript-line ${m.role}`}>
            <div className="faint small">{speaker(m)} · {time(m.created_at)}</div>
            <div className="bubble-text">{m.content}</div>
          </div>
        ))}
      </div>
      <div className="card-pad stack" style={{ borderTop: "1px solid var(--border)" }}>
        <ErrorBanner error={error} />
        <div className="row decision-row">
          <textarea className="textarea" rows={2} maxLength={2000} placeholder="以人工客服身份回复顾客" value={text}
            onChange={(e) => setText(e.target.value)} />
          <button className="btn btn-primary" disabled={busy || !text.trim()} onClick={() => act("reply")}>
            <Send size={15} /> 回复顾客
          </button>
        </div>
        <div className="faint small">第一个回复的同事负责跟进。结束人工服务后，对话交还给智能客服，顾客会收到提示。</div>
      </div>
    </div>
  );
}

export default function HandoffsPage() {
  const { data, error, loading, reload } = useLoad(() => api.get<Handoff[]>("/api/handoffs"), []);
  const [selected, setSelected] = useState<number | null>(null);

  useEffect(() => {
    const timer = window.setInterval(() => void reload(), 5000);
    return () => window.clearInterval(timer);
  }, [reload]);

  const current = data?.find((h) => h.ticket_id === selected) ?? data?.[0] ?? null;
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>人工接待</h1>
          <p>顾客转人工后，Agent 不再回答这个对话，顾客继续发的消息会出现在这里。按等待时间先后接待；处理完后结束人工服务，对话交还给智能客服。</p>
        </div>
      </div>
      <ErrorBanner error={error} />
      {loading && !data ? <Empty>加载中…</Empty> : !data?.length ? (
        <Empty>暂时没有等待人工的顾客。用顾客账号在对话里输入“转人工”即可产生一条。</Empty>
      ) : (
        <div className="handoff-layout">
          <div className="stack handoff-list" role="list" aria-label="等待人工的对话">
            {data.map((h) => (
              <button key={h.ticket_id} role="listitem" className={`card handoff-item ${h === current ? "active" : ""}`}
                aria-current={h === current} onClick={() => setSelected(h.ticket_id)}>
                <div className="row">
                  <b>{h.customer}</b>
                  <span className="spacer" />
                  {h.position ? <span className="badge badge-warning">排队第 {h.position} 位</span> : <span className="badge badge-info">{h.assignee}</span>}
                </div>
                <div className="faint small ellipsis">{h.last_message || h.reason}</div>
                <div className="faint small">已等待 {waited(h.waiting_seconds)}</div>
              </button>
            ))}
          </div>
          {current && <HandoffPanel key={current.ticket_id} handoff={current} onChanged={() => void reload()} />}
        </div>
      )}
    </div>
  );
}
