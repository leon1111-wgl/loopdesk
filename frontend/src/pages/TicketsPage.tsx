import { useEffect, useRef, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Check, Hand } from "lucide-react";
import { api } from "../api";
import { Empty, ErrorBanner, Modal, time, useLoad } from "../components/ui";

interface Ticket {
  id: number;
  title: string;
  description: string;
  priority: "low" | "normal" | "high" | "urgent";
  status: "open" | "in_progress" | "closed";
  conversation_id: number | null;
  resolution: string;
  customer: { id: number; name: string } | null;
  assignee: { id: number; name: string } | null;
  created_at: string;
  due_at: string;
  closed_at: string | null;
  overdue: boolean;
}

const PRIORITY: Record<Ticket["priority"], string> = { low: "低", normal: "普通", high: "高", urgent: "紧急" };
const STATUS: Record<Ticket["status"], string> = { open: "待接手", in_progress: "处理中", closed: "已处理" };

function remaining(ticket: Ticket) {
  if (ticket.status === "closed") return <span className="faint">{time(ticket.closed_at)} 处理完成</span>;
  const minutes = Math.round((new Date(ticket.due_at).getTime() - Date.now()) / 60000);
  const span = Math.abs(minutes) < 60 ? `${Math.abs(minutes)} 分钟` : `${Math.floor(Math.abs(minutes) / 60)} 小时 ${Math.abs(minutes) % 60} 分钟`;
  return ticket.overdue
    ? <span className="badge badge-danger">已超时 {span}</span>
    : <span className={minutes < 60 ? "badge badge-warning" : "faint"}>剩余 {span}</span>;
}

function CloseModal({ ticket, onClose, onDone }: { ticket: Ticket; onClose: () => void; onDone: () => void }) {
  const [resolution, setResolution] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  async function submit() {
    setBusy(true);
    setError(null);
    try {
      await api.post(`/api/tickets/${ticket.id}/close`, { resolution });
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }
  return (
    <Modal title={`处理工单 #${ticket.id}`} onClose={onClose}
      footer={<><button className="btn" onClick={onClose}>取消</button><button className="btn btn-primary" disabled={busy || !resolution.trim()} onClick={submit}>完成并告知顾客</button></>}>
      <div className="small faint">{ticket.title}</div>
      <label className="field">处理结果（会发到顾客的对话里）
        <textarea className="textarea" style={{ minHeight: 120 }} value={resolution} onChange={(e) => setResolution(e.target.value)}
          placeholder="例如：已安排上门检修，明天上午到达" />
      </label>
      <ErrorBanner error={error} />
    </Modal>
  );
}

export default function TicketsPage() {
  const [params] = useSearchParams();
  const focus = Number(params.get("ticket")) || null; // linked from another page, e.g. the refund console
  const [showClosed, setShowClosed] = useState(false);
  const { data, error, reload } = useLoad(() => api.get<Ticket[]>(`/api/tickets${showClosed ? "?status_filter=all" : ""}`), [showClosed]);
  const [closing, setClosing] = useState<Ticket | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  // Once per link: show the full list if the ticket is closed, then scroll to it. Later reloads
  // (claiming, closing, the checkbox) must not jump back to it or re-tick the checkbox.
  const focused = useRef<number | null>(null);
  useEffect(() => {
    if (!focus || !data || focused.current === focus) return;
    if (data.some((t) => t.id === focus)) {
      focused.current = focus;
      document.getElementById(`ticket-${focus}`)?.scrollIntoView({ block: "center" });
    } else if (!showClosed) {
      setShowClosed(true); // a closed ticket is only on the full list
    } else {
      focused.current = focus; // not among the recent ones either: give up
    }
  }, [focus, data, showClosed]);

  async function claim(ticket: Ticket) {
    setActionError(null);
    try {
      await api.post(`/api/tickets/${ticket.id}/claim`);
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    }
    reload();
  }

  return (
    <div className="page stack" style={{ gap: 16 }}>
      <div className="page-head">
        <div>
          <h1>工单</h1>
          <p>Agent 为顾客创建的工单，按对顾客承诺的响应时限排序。接手后处理，处理结果会发到顾客的对话里。</p>
        </div>
        <label className="row small"><input type="checkbox" checked={showClosed} onChange={(e) => setShowClosed(e.target.checked)} /> 显示已处理</label>
      </div>
      <ErrorBanner error={error ?? actionError} />
      <div className="card table-wrap">
        <table className="table">
          <thead><tr><th>工单</th><th>顾客</th><th>优先级</th><th>状态</th><th>承诺时限</th><th>接手人</th><th /></tr></thead>
          <tbody>
            {data?.map((t) => (
              <tr key={t.id} id={`ticket-${t.id}`} className={t.id === focus ? "row-focus" : undefined}
                aria-current={t.id === focus ? "true" : undefined}>
                <td><b>#{t.id} {t.title}</b>
                  {/* the agent often files the customer's own sentence as both title and description */}
                  {t.description && t.description.trim() !== t.title.trim() && <div className="faint small">{t.description}</div>}
                  {t.resolution && <div className="small">处理结果：{t.resolution}</div>}</td>
                <td className="nowrap">{t.customer?.name ?? "—"}</td>
                <td className="nowrap">{t.priority === "high" || t.priority === "urgent" ? <span className="badge badge-warning">{PRIORITY[t.priority]}</span> : PRIORITY[t.priority]}</td>
                <td className="nowrap">{STATUS[t.status]}</td>
                <td className="nowrap">{remaining(t)}</td>
                <td className="nowrap">{t.assignee?.name ?? <span className="faint">—</span>}</td>
                <td>
                  <div className="row">
                    {t.status === "open" && <button className="btn btn-sm" onClick={() => claim(t)}><Hand size={14} /> 接手</button>}
                    {t.status !== "closed" && <button className="btn btn-sm btn-primary" onClick={() => setClosing(t)}><Check size={14} /> 处理完成</button>}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        {data && data.length === 0 && <Empty>{showClosed ? "还没有工单" : "没有待处理的工单"}</Empty>}
      </div>
      {closing && <CloseModal ticket={closing} onClose={() => setClosing(null)} onDone={() => { setClosing(null); reload(); }} />}
    </div>
  );
}
