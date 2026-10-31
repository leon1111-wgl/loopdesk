import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Bot, Clock, Search, Send, Undo2 } from "lucide-react";
import { api } from "../api";
import { useAuth } from "../auth";
import {
  CHANNEL_TEXT, Empty, ErrorBanner, PageHeader, Segmented, Skeleton, StatusBadge, TOOL_TEXT, VerdictBadge, fullTime, time, useLoad, yuan,
} from "../components/ui";
import type { Approval, ChatMessage, Handoff, Refund } from "../types";

// The agent workbench (docs/DESIGN.md): the queue | the conversation | the customer's orders, approvals,
// refunds and tickets. Everything shown comes from the same APIs the other pages use.

interface TicketRow {
  id: number;
  title: string;
  status: "open" | "in_progress" | "closed";
  conversation_id: number | null;
  priority: string;
  overdue: boolean;
}

const PRIORITY: Record<string, string> = { low: "低", normal: "普通", high: "高", urgent: "紧急" };
const TICKET_STATUS: Record<string, [string, string]> = {
  open: ["待接手", "badge-warning"], in_progress: ["处理中", "badge-primary"], closed: ["已处理", "badge-success"],
};
const REFUND_STATUS: Record<string, [string, string]> = {
  claiming: ["登记未完成", "badge-danger"], pending: ["待确认", "badge-warning"], processing: ["处理中", "badge-info"],
  succeeded: ["已退款", "badge-success"], failed: ["失败", "badge-danger"],
};
const ORDER_ID = /SO\d{11}/g;

function waited(seconds: number) {
  if (seconds < 60) return "不到 1 分钟";
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟`;
  return `${Math.floor(seconds / 3600)} 小时 ${Math.floor((seconds % 3600) / 60)} 分钟`;
}

/** Waiting-time SLA: under 5 minutes fine, under 15 a warning, after that overdue. */
function SlaTimer({ handoff }: { handoff: Handoff }) {
  const level = handoff.waiting_seconds < 300 ? "ok" : handoff.waiting_seconds < 900 ? "soon" : "late";
  return (
    <span className={`sla sla-${level}`} title={`转人工于 ${fullTime(handoff.created_at)}`}>
      <Clock size={12} aria-hidden="true" />
      {handoff.position ? "已等待" : "已接待"} {waited(handoff.waiting_seconds)}
    </span>
  );
}

/** Canned phrases an agent sends most; {name} is the agent's own name. */
const QUICK_REPLIES = [
  "您好，我是人工客服 {name}，这就帮您看。",
  "麻烦提供一下订单号，我帮您核实。",
  "已经帮您提交处理，结果会在这里通知您。",
  "还有其他可以帮您的吗？",
];

function ContextPanel({ handoff, messages }: { handoff: Handoff; messages: ChatMessage[] }) {
  // The whole tenant's lists, filtered to this conversation: the same requests the other pages make.
  const approvals = useLoad(() => api.get<Approval[]>("/api/approvals?status_filter=all"), [handoff.conversation_id]);
  const refunds = useLoad(() => api.get<Refund[]>("/api/refunds?status=all"), [handoff.conversation_id]);
  const tickets = useLoad(() => api.get<TicketRow[]>("/api/tickets?status_filter=all"), [handoff.conversation_id]);
  const mine = <T extends { conversation_id: number | null }>(rows: T[] | null) => (rows ?? []).filter((r) => r.conversation_id === handoff.conversation_id);
  const ownApprovals = mine(approvals.data);
  const ownRefunds = mine(refunds.data);
  const ownTickets = mine(tickets.data).filter((t) => t.id !== handoff.ticket_id);
  const orders = useMemo(
    () => [...new Set(messages.flatMap((m) => m.content.match(ORDER_ID) ?? []).concat(ownApprovals.map((a) => String(a.arguments.order_id ?? "")).filter(Boolean)))],
    [messages, ownApprovals],
  );
  const loading = approvals.loading && !approvals.data;
  return (
    <aside className="wb-context" aria-label="顾客与业务">
      <section className="ctx-section">
        <h3>顾客信息</h3>
        <dl className="desc-list">
          <dt>顾客</dt><dd>{handoff.customer ?? "—"}</dd>
          <dt>渠道</dt><dd>{CHANNEL_TEXT[handoff.channel ?? ""] ?? handoff.channel ?? "—"}</dd>
          <dt>会话</dt><dd className="mono">#{handoff.conversation_id}</dd>
          <dt>转人工</dt><dd>{time(handoff.created_at)}</dd>
          <dt>优先级</dt><dd>{PRIORITY[handoff.priority] ?? handoff.priority}</dd>
          <dt>接待人</dt><dd>{handoff.assignee ?? <span className="faint">尚未接入</span>}</dd>
        </dl>
      </section>
      <section className="ctx-section">
        <h3>提到的订单</h3>
        {orders.length === 0 ? <div className="faint">对话里还没有出现订单号</div> : (
          <ul className="ctx-list">
            {orders.map((o) => {
              const refund = ownRefunds.find((r) => r.order_id === o);
              return (
                <li key={o}>
                  <span className="mono">{o}</span>
                  {refund && <span className={`badge ${REFUND_STATUS[refund.status]?.[1] ?? ""}`}>退款{REFUND_STATUS[refund.status]?.[0] ?? refund.status}</span>}
                </li>
              );
            })}
          </ul>
        )}
      </section>
      <section className="ctx-section">
        <h3>售后与审批</h3>
        {loading ? <Skeleton rows={2} /> : ownApprovals.length + ownRefunds.length === 0 ? (
          <div className="faint">这个会话没有退款申请</div>
        ) : (
          <ul className="ctx-list">
            {ownApprovals.map((a) => (
              <li key={`a${a.id}`}>
                <div className="ctx-line">
                  <span>审批 #{a.id} · {TOOL_TEXT[a.tool_name] ?? a.tool_name}</span>
                  <StatusBadge status={a.status} />
                </div>
                <div className="ctx-line faint">
                  <span className="mono">{String(a.arguments.order_id ?? "")}</span>
                  <b className="ctx-amount">{yuan(a.arguments.amount)}</b>
                </div>
                <Link className="btn btn-sm" to={`/approvals?open=${a.id}`}>{a.status === "pending" ? "去审批" : "查看审批"}</Link>
              </li>
            ))}
            {ownRefunds.map((r) => (
              <li key={`r${r.id}`}>
                <div className="ctx-line">
                  <span>退款 #{r.id}</span>
                  <span className={`badge ${REFUND_STATUS[r.status]?.[1] ?? ""}`}>{REFUND_STATUS[r.status]?.[0] ?? r.status}</span>
                </div>
                <div className="ctx-line faint"><span className="mono">{r.order_id}</span><b className="ctx-amount">{yuan(r.amount)}</b></div>
                {(r.status === "pending" || r.status === "processing") && <Link className="btn btn-sm" to="/refunds">去对账</Link>}
              </li>
            ))}
          </ul>
        )}
      </section>
      <section className="ctx-section">
        <h3>工单</h3>
        {ownTickets.length === 0 ? <div className="faint">没有其他工单</div> : (
          <ul className="ctx-list">
            {ownTickets.map((t) => (
              <li key={t.id}>
                <div className="ctx-line">
                  <span className="ellipsis">#{t.id} {t.title}</span>
                  <span className={`badge ${TICKET_STATUS[t.status]?.[1] ?? ""}`}>{TICKET_STATUS[t.status]?.[0] ?? t.status}</span>
                </div>
                {t.overdue && <div className="ctx-line"><span className="badge badge-danger">已超时</span></div>}
                <Link className="btn btn-sm" to={`/tickets?ticket=${t.id}`}>查看工单</Link>
              </li>
            ))}
          </ul>
        )}
      </section>
    </aside>
  );
}

function HandoffPanel({ handoff, onChanged }: { handoff: Handoff; onChanged: () => void }) {
  const { user } = useAuth();
  const [loaded, setLoaded] = useState<ChatMessage[] | null>(null);
  const messages = loaded ?? [];
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const box = useRef<HTMLTextAreaElement>(null);
  const transcript = useRef<HTMLDivElement>(null);
  const load = useCallback(async () => {
    const r = await api.get<{ messages: ChatMessage[] }>(`/api/conversations/${handoff.conversation_id}/messages`);
    setLoaded(r.messages);
  }, [handoff.conversation_id]);

  // The customer can keep writing while waiting; refresh so new messages show up.
  useEffect(() => {
    load().catch((e) => setError(e.message));
    const timer = window.setInterval(() => load().catch(() => undefined), 4000);
    return () => window.clearInterval(timer);
  }, [load]);
  // Scroll the transcript only: scrollIntoView would also scroll the page (on phones the panes stack).
  useEffect(() => {
    const el = transcript.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages.length]);

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

  function insert(phrase: string) {
    setText((t) => (t.trim() ? `${t.trimEnd()}\n${phrase}` : phrase));
    box.current?.focus();
  }

  // The bot's last answer, offered as a draft: it already carries checked citations.
  const lastAnswer = [...messages].reverse().find((m) => m.role === "assistant" && m.content.trim());
  const speaker = (m: ChatMessage) =>
    m.role === "user" ? handoff.customer : m.role === "agent" ? `人工客服 ${m.meta.agent ?? ""}` : m.role === "system" ? "系统" : "智能客服";
  return (
    <>
      <section className="wb-chat handoff-panel" aria-label={`与 ${handoff.customer ?? "顾客"} 的对话`}>
        <div className="wb-chat-head">
          <span className="avatar human" aria-hidden="true">{(handoff.customer ?? "顾").slice(0, 1)}</span>
          <div className="wb-chat-title">
            <div className="row" style={{ gap: 6, minWidth: 0 }}>
              <b className="ellipsis">{handoff.customer}</b>
              <span className="badge">{CHANNEL_TEXT[handoff.channel ?? ""] ?? handoff.channel}</span>
              {handoff.assignee ? <span className="badge badge-info">{handoff.assignee} 跟进中</span> : <span className="badge badge-warning">等待接入</span>}
            </div>
            <div className="faint ellipsis">{handoff.summary}</div>
          </div>
          <span className="spacer" />
          <button className="btn btn-sm" disabled={busy} onClick={() => act("close")}>
            <Undo2 size={14} /> 结束人工服务
          </button>
        </div>
        <div ref={transcript} className="wb-transcript" aria-live="polite">
          {handoff.reason && (
            <div className="ai-brief handoff-brief" aria-label="转人工摘要">
              <div className="ai-brief-head"><Bot size={14} aria-hidden="true" /> 智能客服的转人工摘要</div>
              {handoff.reason.split("\n").map((line, i) => <div key={i}>{line}</div>)}
            </div>
          )}
          {loaded === null && <Skeleton rows={4} />}
          {messages.map((m) => m.role === "system" ? (
            <div key={m.id} className="msg-system">{m.content}</div>
          ) : (
            <div key={m.id} className={`wb-msg ${m.role === "user" ? "theirs" : "ours"} ${m.role}`}>
              <div className="wb-msg-meta">{speaker(m)} · {time(m.created_at)}</div>
              <div className="wb-bubble">
                <div className="bubble-text">{m.content}</div>
                {m.role === "assistant" && ((m.meta.citations?.length ?? 0) > 0 || m.meta.verification) && (
                  <div className="cite-chips">
                    {m.meta.verification && <VerdictBadge verdict={m.meta.verification.verdict} coverage={m.meta.verification.coverage} />}
                    {(m.meta.citations ?? []).filter((c) => c.kind === "kb").map((c) => (
                      <span key={c.ref} className="cite-chip" title={c.text}>[{c.ref}] 《{c.title}》</span>
                    ))}
                  </div>
                )}
              </div>
            </div>
          ))}
        </div>
        <div className="wb-composer">
          <ErrorBanner error={error} />
          <div className="quick-replies" role="group" aria-label="常用语">
            {QUICK_REPLIES.map((q) => {
              const phrase = q.replace("{name}", user?.name ?? "");
              return <button key={q} type="button" className="chip" onClick={() => insert(phrase)}>{phrase}</button>;
            })}
          </div>
          {lastAnswer && (
            <div className="ai-draft">
              <Bot size={14} aria-hidden="true" />
              <span className="ellipsis">参考智能客服的回答：{lastAnswer.content.replace(/\[\d{1,2}\]/g, "")}</span>
              <button type="button" className="btn btn-text btn-sm" onClick={() => insert(lastAnswer.content.replace(/\[\d{1,2}\]/g, ""))}>插入并编辑</button>
            </div>
          )}
          <div className="wb-compose-row">
            <textarea ref={box} className="textarea" rows={3} maxLength={2000} placeholder="以人工客服身份回复顾客" value={text}
              aria-label="回复内容" onChange={(e) => setText(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && text.trim() && !busy) {
                  e.preventDefault();
                  void act("reply");
                }
              }} />
          </div>
          <div className="row">
            <span className="faint">第一个回复的同事负责跟进 · ⌘/Ctrl + Enter 发送</span>
            <span className="spacer" />
            <button className="btn btn-primary" disabled={busy || !text.trim()} onClick={() => act("reply")}>
              <Send size={15} /> 回复顾客
            </button>
          </div>
        </div>
      </section>
      <ContextPanel handoff={handoff} messages={messages} />
    </>
  );
}

export default function HandoffsPage() {
  const { data, error, loading, reload } = useLoad(() => api.get<Handoff[]>("/api/handoffs"), []);
  const [selected, setSelected] = useState<number | null>(null);
  const [filter, setFilter] = useState<"all" | "queued" | "serving">("all");
  const [query, setQuery] = useState("");

  useEffect(() => {
    const timer = window.setInterval(() => void reload(), 5000);
    return () => window.clearInterval(timer);
  }, [reload]);

  const rows = data ?? [];
  const queued = rows.filter((h) => h.position).length;
  const shown = rows.filter((h) =>
    (filter === "all" || (filter === "queued" ? h.position : !h.position)) &&
    (!query.trim() || `${h.customer ?? ""} ${h.summary} ${h.last_message}`.includes(query.trim())));
  // The open conversation stays open while the filter, the search or a refresh changes the list (a
  // reply being typed is not lost); a new pick is made only when it has left the queue.
  const current = rows.find((h) => h.ticket_id === selected) ?? null;
  useEffect(() => {
    if (data && !data.some((h) => h.ticket_id === selected) && data.length) setSelected(data[0].ticket_id);
  }, [data, selected]);
  const hidden = current !== null && !shown.includes(current);
  return (
    <div className="page workbench-page">
      <PageHeader title="人工接待"
        description="顾客转人工后 Agent 不再回答，顾客的新消息会出现在这里。按等待时间先后接待，处理完后结束人工服务、交还智能客服。"
        actions={rows.length > 0 && (
          <div className="wb-stats" aria-label="队列概况">
            <span><b>{queued}</b> 排队中</span>
            <span><b>{rows.length - queued}</b> 接待中</span>
          </div>
        )} />
      <ErrorBanner error={error} />
      {loading && !data ? <div className="card"><Skeleton rows={5} /></div> : !rows.length ? (
        <div className="card"><Empty>暂时没有等待人工的顾客。用顾客账号在对话里输入“转人工”即可产生一条。</Empty></div>
      ) : (
        <div className="workbench">
          <section className="wb-list" aria-label="会话列表">
            <div className="wb-list-head">
              <Segmented label="按状态筛选" value={filter} onChange={setFilter} options={[
                ["all", <>全部<span className="count">{rows.length}</span></>],
                ["queued", <>排队中<span className="count">{queued}</span></>],
                ["serving", <>接待中<span className="count">{rows.length - queued}</span></>],
              ]} />
              <label className="search-input">
                <Search size={14} aria-hidden="true" />
                <input className="input" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="搜索顾客或留言" aria-label="搜索会话" />
              </label>
            </div>
            <ul className="wb-items" aria-label="等待人工的对话">
              {shown.map((h) => (
                <li key={h.ticket_id}>
                  <button className={`wb-item ${h === current ? "active" : ""}`} aria-current={h === current} onClick={() => setSelected(h.ticket_id)}>
                    <span className="avatar human" aria-hidden="true">{(h.customer ?? "顾").slice(0, 1)}</span>
                    <span className="wb-item-main">
                      <span className="row" style={{ gap: 6 }}>
                        <b className="ellipsis">{h.customer}</b>
                        <span className="badge">{CHANNEL_TEXT[h.channel ?? ""] ?? h.channel}</span>
                        <span className="spacer" />
                        {h.position ? <span className="badge badge-warning">排队第 {h.position} 位</span> : <span className="badge badge-info">{h.assignee}</span>}
                      </span>
                      <span className="small ellipsis">{h.summary}</span>
                      {h.last_message && <span className="faint ellipsis">最新留言：{h.last_message}</span>}
                      <SlaTimer handoff={h} />
                    </span>
                  </button>
                </li>
              ))}
              {shown.length === 0 && <li><Empty icon={null}>没有符合条件的会话</Empty></li>}
              {hidden && <li className="faint wb-hidden-note">正在接待的 {current.customer} 不在筛选结果中</li>}
            </ul>
          </section>
          {current ? <HandoffPanel key={current.ticket_id} handoff={current} onChanged={() => void reload()} />
            : <section className="wb-chat"><Empty>选择左侧的会话开始接待</Empty></section>}
        </div>
      )}
    </div>
  );
}
