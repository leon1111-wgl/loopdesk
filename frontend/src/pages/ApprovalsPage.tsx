import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { Check, FileSearch, X } from "lucide-react";
import { api } from "../api";
import { useAuth } from "../auth";
import {
  ConfirmDialog, Drawer, Empty, ErrorBanner, PageHeader, ROLE_TEXT, Segmented, Skeleton, StatusBadge, TOOL_TEXT, fullTime, pressable, time, useLoad, yuan,
} from "../components/ui";
import type { Approval } from "../types";

/** The evidence package and the decision, in a side sheet: a reviewer cannot approve without it in view. */
function ApprovalDrawer({ approval, onClose, onDecided }: { approval: Approval; onClose: () => void; onDecided: (summary: string) => void }) {
  const { user } = useAuth();
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<"approve" | "withdraw" | null>(null);
  const own = approval.requested_by?.id === user?.id;
  const needsAdmin = approval.required_role === "admin" && user?.role !== "admin";
  const orderId = String(approval.arguments.order_id ?? "");
  const amount = yuan(approval.arguments.amount);

  async function decide(decision: "approve" | "reject") {
    setBusy(true);
    setError(null);
    try {
      const r = await api.post<{ final: { text: string }; delivery: { sent: boolean; error?: string } | null }>(
        `/api/approvals/${approval.id}/decide`, { decision, comment });
      const text = r.final?.text ?? "已处理";
      const notice = r.delivery && !r.delivery.sent ? `（${r.delivery.error ?? "飞书通知未发出，结果已写入对话"}）` : "";
      onDecided(`审批单 #${approval.id} 已${decision === "approve" ? "通过并执行" : "驳回"}，已回复顾客：${text}${notice}`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setConfirm(null);
    } finally {
      setBusy(false);
    }
  }

  async function withdraw() {
    setBusy(true);
    setError(null);
    try {
      await api.post(`/api/approvals/${approval.id}/withdraw`, {});
      onDecided(`审批单 #${approval.id} 已撤回，未执行`);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setConfirm(null);
    } finally {
      setBusy(false);
    }
  }

  const history = approval.history ?? [];
  const kb = approval.evidence.filter((e) => e.kind === "kb");
  const tools = approval.evidence.filter((e) => e.kind === "tool");
  const pending = approval.status === "pending";
  return (
    <>
      <Drawer width={640} onClose={onClose}
        title={<div className="stack" style={{ gap: 4 }}>
          <div className="row"><h2>审批单 #{approval.id} · {TOOL_TEXT[approval.tool_name] ?? approval.tool_name}</h2><StatusBadge status={approval.status} /></div>
          <div className="faint">{fullTime(approval.created_at)} 提交 · <Link to={`/traces/${approval.run_id}`}>查看链路</Link></div>
        </div>}
        footer={pending ? (
          <>
            <ErrorBanner error={error} />
            {own && (
              <div className="notice">
                <span>这是你自己发起的申请，按四眼原则需要其他同事审批。填错了或没人能审批时可以撤回。</span>
                <span className="spacer" />
                <button className="btn btn-sm" disabled={busy} onClick={() => setConfirm("withdraw")}>撤回申请</button>
              </div>
            )}
            {needsAdmin && !own && <div className="notice">这笔金额需要管理员通过；你可以驳回（需写明原因），或留给管理员处理。</div>}
            <label className="field">审批意见
              <input className="input" placeholder="审批意见（会告知顾客），驳回时必填" value={comment} onChange={(e) => setComment(e.target.value)} />
            </label>
            <div className="row decision-row" style={{ justifyContent: "flex-end" }}>
              <button className="btn btn-danger" disabled={busy || own || !comment.trim()} title={comment.trim() ? undefined : "驳回需要写明原因"}
                onClick={() => decide("reject")}><X size={15} /> 驳回</button>
              <button className="btn btn-primary" disabled={busy || own || needsAdmin} onClick={() => setConfirm("approve")}><Check size={15} /> 通过并执行</button>
            </div>
          </>
        ) : (
          <div className="faint">
            {approval.status === "withdrawn"
              ? `${approval.withdrawn_by === "requester" ? `${approval.requested_by?.name ?? "提交人"}（提交人）` : "顾客"}于 ${time(approval.decided_at)} 撤回了这笔申请，未执行`
              : <>{approval.reviewer?.name} 于 {time(approval.decided_at)} 处理{approval.comment && `，意见：${approval.comment}`}</>}
          </div>
        )}>
        <div className="stack" style={{ gap: 20 }}>
          <div className="money-line">
            <span className="stack" style={{ gap: 2 }}><span className="faint">订单</span><span className="mono">{orderId}</span></span>
            <span className="stack" style={{ gap: 2, alignItems: "flex-end" }}><span className="faint">退款金额</span><b>{amount}</b></span>
          </div>
          <section className="sheet-section">
            <h3>申请信息</h3>
            <dl className="desc-list">
              <dt>申请人</dt><dd>{approval.requested_by?.name}（{ROLE_TEXT[approval.requested_by?.role ?? ""]}）</dd>
              <dt>原始诉求</dt><dd>{approval.question}</dd>
              <dt>需要审批</dt><dd><span className="badge badge-warning">{approval.reason}</span></dd>
              <dt>审批级别</dt><dd>{approval.required_role === "admin" ? "管理员（金额超过租户的审批分级线）" : "客服坐席或管理员"}</dd>
            </dl>
          </section>
          {history.length > 0 && (
            <section className="sheet-section">
              <h3>该订单的历史申请（{history.length} 条）</h3>
              <ul className="history-list">
                {history.map((h) => (
                  <li key={h.id}>
                    <span className="mono">#{h.id}</span>
                    <StatusBadge status={h.status} />
                    <span>{yuan(h.amount)}</span>
                    <span className="faint">
                      {h.status === "pending" ? `${time(h.created_at)} 提交`
                        : h.status === "withdrawn" ? `${time(h.decided_at)} 撤回` : `${h.reviewer ?? "审批人"} ${time(h.decided_at)}`}
                      {h.status === "rejected" && h.comment && `，意见：${h.comment}`}
                    </span>
                  </li>
                ))}
              </ul>
            </section>
          )}
          <section className="sheet-section">
            <h3>Agent 提交的证据包（{approval.evidence.length} 条）</h3>
            {kb.map((e) => (
              <div key={e.ref} className="evidence-card">
                <div className="row"><span className="cite">{e.ref}</span><b>《{e.title}》</b><span className="faint ellipsis">{e.meta.heading}</span></div>
                <div className="evidence-text">{e.text}</div>
              </div>
            ))}
            {tools.map((e) => (
              <details key={e.ref} className="evidence-card">
                <summary><span className="cite">{e.ref}</span> 工具结果：{TOOL_TEXT[e.title] ?? e.title}</summary>
                <pre className="json mono">{(() => { try { return JSON.stringify(JSON.parse(e.text), null, 2); } catch { return e.text; } })()}</pre>
              </details>
            ))}
            {approval.evidence.length === 0 && <div className="faint">没有附带证据</div>}
          </section>
        </div>
      </Drawer>
      {confirm === "approve" && (
        <ConfirmDialog title="确认通过并退款" confirmText={`确认退款 ${amount}`} tone="danger" busy={busy}
          onClose={() => setConfirm(null)} onConfirm={() => void decide("approve")}>
          <div className="money-line">
            <span className="mono">{orderId}</span>
            <b>{amount}</b>
          </div>
          <p className="small muted">通过后 Agent 会重新校验订单状态，再向支付系统发起这笔退款，并把结果回复给顾客。退款发出后不能撤回。</p>
        </ConfirmDialog>
      )}
      {confirm === "withdraw" && (
        <ConfirmDialog title={`撤回审批单 #${approval.id}`} confirmText="撤回申请" busy={busy}
          onClose={() => setConfirm(null)} onConfirm={() => void withdraw()}>
          <p>撤回后不会执行，也不会通知顾客。</p>
        </ConfirmDialog>
      )}
    </>
  );
}

export default function ApprovalsPage() {
  const [params, setParams] = useSearchParams();
  const requested = Number(params.get("open")) || null; // from the workbench's "去审批"
  const [tab, setTab] = useState<"pending" | "all">("pending");
  const { data, error, loading, reload } = useLoad(() => api.get<Approval[]>(`/api/approvals?status_filter=${tab}`), [tab]);
  const [openId, setOpenId] = useState<number | null>(requested);
  const [lastDecision, setLastDecision] = useState<string | null>(null);
  const open = data?.find((a) => a.id === openId) ?? null;

  // A link to an approval that is no longer pending looks in "全部".
  useEffect(() => {
    if (requested && tab === "pending" && data && !data.some((a) => a.id === requested)) setTab("all");
  }, [requested, tab, data]);

  function close() {
    setOpenId(null);
    if (params.has("open")) setParams({}, { replace: true });
  }

  return (
    <div className="page stack" style={{ gap: 16 }}>
      <PageHeader title="审批队列"
        description="超出租户自动审批额度的退款会暂停在这里。打开审批单查看 Agent 提交的证据包；通过后 Agent 会重新校验订单状态再执行，并把结果回复给顾客。" />
      <ErrorBanner error={error} />
      {lastDecision && <div className="notice" role="status">{lastDecision}</div>}
      <div className="card">
        <div className="card-toolbar">
          <Segmented label="审批单范围" value={tab} onChange={setTab} options={[["pending", "待处理"], ["all", "全部"]]} />
          <span className="spacer" />
          {data && <span className="faint">{data.length} 条</span>}
        </div>
        {loading && !data ? <Skeleton rows={4} /> : data?.length === 0 ? (
          <Empty>{tab === "pending" ? "没有待处理的审批单。用顾客账号在对话里输入“SO20261008002 我要退款”即可产生一条。" : "暂无审批单"}</Empty>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr><th>审批单</th><th>订单</th><th className="num">金额</th><th className="hide-compact">申请人</th><th className="hide-compact">需要审批的原因</th><th>状态</th><th className="hide-compact">提交时间</th><th><span className="sr-only">操作</span></th></tr>
              </thead>
              <tbody>
                {data?.map((a) => (
                  <tr key={a.id} className={`clickable ${a.id === openId ? "row-focus" : ""}`} {...pressable(() => setOpenId(a.id))}>
                    <td className="nowrap"><b>#{a.id}</b> <span className="faint">{TOOL_TEXT[a.tool_name] ?? a.tool_name}</span></td>
                    <td className="mono nowrap">{String(a.arguments.order_id ?? "")}</td>
                    <td className="num nowrap"><b>{yuan(a.arguments.amount)}</b></td>
                    <td className="nowrap hide-compact">{a.requested_by?.name}<div className="faint">{ROLE_TEXT[a.requested_by?.role ?? ""]}</div></td>
                    <td className="hide-compact" style={{ minWidth: 200 }}>
                      <div className="clamp-2">{a.reason}</div>
                      {a.required_role === "admin" && <span className="badge badge-info" style={{ marginTop: 4 }}>需管理员</span>}
                    </td>
                    <td className="nowrap"><StatusBadge status={a.status} /></td>
                    <td className="nowrap faint hide-compact">{time(a.created_at)}</td>
                    <td className="actions">
                      <button className="btn btn-sm" onClick={(e) => { e.stopPropagation(); setOpenId(a.id); }}>
                        <FileSearch size={14} /> {a.status === "pending" ? "审核" : "查看"}
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      {open && (
        <ApprovalDrawer key={open.id} approval={open} onClose={close}
          onDecided={(summary) => { close(); setLastDecision(summary); void reload(); }} />
      )}
    </div>
  );
}
