import { useState } from "react";
import { Link } from "react-router-dom";
import { Check, X } from "lucide-react";
import { api } from "../api";
import { useAuth } from "../auth";
import { Empty, ErrorBanner, ROLE_TEXT, StatusBadge, TOOL_TEXT, time, useLoad, yuan } from "../components/ui";
import type { Approval } from "../types";

function ApprovalCard({ approval, onDecided }: { approval: Approval; onDecided: (summary: string) => void }) {
  const { user } = useAuth();
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<string | null>(null);
  const own = approval.requested_by?.id === user?.id;
  const needsAdmin = approval.required_role === "admin" && user?.role !== "admin";

  async function decide(decision: "approve" | "reject") {
    setBusy(true);
    setError(null);
    try {
      const r = await api.post<{ final: { text: string }; delivery: { sent: boolean; error?: string } | null }>(
        `/api/approvals/${approval.id}/decide`, { decision, comment });
      const text = r.final?.text ?? "已处理";
      setResult(text);
      const notice = r.delivery && !r.delivery.sent ? `（${r.delivery.error ?? "飞书通知未发出，结果已写入对话"}）` : "";
      onDecided(`审批单 #${approval.id} 已${decision === "approve" ? "通过并执行" : "驳回"}，已回复顾客：${text}${notice}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  const kb = approval.evidence.filter((e) => e.kind === "kb");
  const tools = approval.evidence.filter((e) => e.kind === "tool");
  return (
    <div className="card">
      <div className="card-head">
        <div className="row">
          <b>#{approval.id} {TOOL_TEXT[approval.tool_name] ?? approval.tool_name}</b>
          <StatusBadge status={approval.status} />
        </div>
        <span className="faint">{time(approval.created_at)}</span>
      </div>
      <div className="card-pad grid grid-2">
        <div className="stack">
          <div className="faint">申请人</div>
          <div>{approval.requested_by?.name}（{ROLE_TEXT[approval.requested_by?.role ?? ""]}）</div>
          <div className="faint">原始诉求</div>
          <div>{approval.question}</div>
          <div className="faint">操作参数</div>
          <div className="row">
            <span className="mono">{String(approval.arguments.order_id ?? "")}</span>
            <b style={{ fontSize: 20 }}>{yuan(approval.arguments.amount)}</b>
          </div>
          <div className="badge badge-warning" style={{ width: "fit-content" }}>{approval.reason}</div>
          {approval.required_role === "admin" && (
            <div className="badge badge-info" style={{ width: "fit-content" }}>金额超过租户的审批分级线，需要管理员通过</div>
          )}
        </div>
        <div className="stack">
          <div className="faint">Agent 提交的证据包（{approval.evidence.length} 条）</div>
          {kb.map((e) => (
            <div key={e.ref} className="json small">
              <b>[{e.ref}] 《{e.title}》</b> <span className="faint">{e.meta.heading}</span>
              <div style={{ whiteSpace: "pre-wrap", marginTop: 4 }}>{e.text}</div>
            </div>
          ))}
          {tools.map((e) => (
            <details key={e.ref} className="small">
              <summary style={{ cursor: "pointer" }}>[{e.ref}] 工具结果：{TOOL_TEXT[e.title] ?? e.title}</summary>
              <pre className="json mono">{(() => { try { return JSON.stringify(JSON.parse(e.text), null, 2); } catch { return e.text; } })()}</pre>
            </details>
          ))}
        </div>
      </div>
      {approval.status === "pending" ? (
        <div className="card-pad stack" style={{ borderTop: "1px solid var(--border)" }}>
          <ErrorBanner error={error} />
          {own && <div className="notice">这是你自己发起的申请，按四眼原则需要其他同事审批。</div>}
          {needsAdmin && !own && <div className="notice">这笔金额需要管理员通过；你可以驳回（需写明原因），或留给管理员处理。</div>}
          <div className="row decision-row">
            <input className="input" placeholder="审批意见（会告知顾客），驳回时必填" value={comment} onChange={(e) => setComment(e.target.value)} />
            <button className="btn btn-danger" disabled={busy || own || !comment.trim()} title={comment.trim() ? undefined : "驳回需要写明原因"}
              onClick={() => decide("reject")}><X size={15} /> 驳回</button>
            <button className="btn btn-success" disabled={busy || own || needsAdmin} onClick={() => decide("approve")}><Check size={15} /> 通过并执行</button>
          </div>
          {result && <div className="notice">已通知顾客：{result}</div>}
        </div>
      ) : (
        <div className="card-pad faint" style={{ borderTop: "1px solid var(--border)" }}>
          {approval.status === "withdrawn"
            ? `顾客于 ${time(approval.decided_at)} 撤回了这笔申请，未执行`
            : <>{approval.reviewer?.name} 于 {time(approval.decided_at)} 处理{approval.comment && `，意见：${approval.comment}`}</>} ·{" "}
          <Link to={`/traces/${approval.run_id}`}>查看链路</Link>
        </div>
      )}
    </div>
  );
}

export default function ApprovalsPage() {
  const [tab, setTab] = useState<"pending" | "all">("pending");
  const { data, error, loading, reload } = useLoad(() => api.get<Approval[]>(`/api/approvals?status_filter=${tab}`), [tab]);
  const [lastDecision, setLastDecision] = useState<string | null>(null);
  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>审批队列</h1>
          <p>超出租户自动审批额度的退款会暂停在这里。通过后 Agent 会重新校验订单状态再执行，并把结果回复给顾客。</p>
        </div>
        <div className="tabs">
          <button className={tab === "pending" ? "active" : ""} onClick={() => setTab("pending")}>待处理</button>
          <button className={tab === "all" ? "active" : ""} onClick={() => setTab("all")}>全部</button>
        </div>
      </div>
      <ErrorBanner error={error} />
      {lastDecision && <div className="notice" style={{ marginBottom: 12 }}>{lastDecision}</div>}
      {loading && !data ? <Empty>加载中…</Empty> : data?.length === 0 ? (
        <Empty>暂无审批单。用顾客账号在对话里输入“SO20261008002 我要退款”即可产生一条。</Empty>
      ) : (
        <div className="stack" style={{ gap: 16 }}>
          {data?.map((a) => <ApprovalCard key={a.id} approval={a} onDecided={(summary) => { setLastDecision(summary); reload(); }} />)}
        </div>
      )}
    </div>
  );
}
