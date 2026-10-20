import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { ClipboardCheck } from "lucide-react";
import { api, ApiError } from "../api";
import { useAuth } from "../auth";
import { Empty, ErrorBanner, Modal, duration, fullTime, time, useLoad, yuan } from "../components/ui";
import type { Refund } from "../types";

// Refunds the payment system has not settled (GET /api/refunds), and the administrator's
// "确认结果" (POST /api/refunds/{id}/settle) once someone has checked its back office.

const STATUS: Record<Refund["status"], [string, string]> = {
  claiming: ["登记未完成", "badge-danger"],
  pending: ["待确认", "badge-warning"],
  processing: ["处理中", "badge-info"],
  succeeded: ["已退款", "badge-success"],
  failed: ["失败", "badge-danger"],
};
const LIMIT = 200;
const REFRESH_MS = 30_000; // ages and the 24-hour window move on while the page is open
const settleable = (r: Refund) => r.follow_up === "resend" || r.follow_up === "follow" || r.follow_up === "person";

/** The ledger keeps the payment system's refusal code, or "unanswered: …" when nothing usable came back. */
function ErrorText({ error }: { error: string }) {
  const unanswered = error.startsWith("unanswered: ");
  return (
    <div className="faint">
      {unanswered ? "无回音：" : "最近错误："}
      <span className="mono">{unanswered ? error.slice("unanswered: ".length) : error}</span>
    </div>
  );
}

function NextStep({ refund }: { refund: Refund }) {
  switch (refund.follow_up) {
    case "resend":
      return (
        <>
          <div>自动重发中</div>
          <div className="faint">同一幂等键，还剩 {duration(new Date(refund.resend_until ?? 0).getTime() - Date.now())}</div>
        </>
      );
    case "follow":
      return (
        <>
          <div>支付系统已受理</div>
          <div className="faint">对账任务在查询结果</div>
        </>
      );
    case "person":
      return (
        <>
          <span className="badge badge-danger">需要人工确认</span>
          <div className="faint">{refund.status === "pending" ? "超过 24 小时，不再自动重发" : "对账任务不再跟进"}</div>
        </>
      );
    case "stuck":
      return (
        <>
          <span className="badge badge-danger">需要排查</span>
          <div className="faint">占着订单但没有提交给支付系统（程序缺陷），见对账任务的 ERROR 日志</div>
        </>
      );
    case "demo":
      return <span className="faint">演示订单表，不经过支付系统</span>;
    default:
      return <span className="faint">{refund.updated_at ? `${time(refund.updated_at)} 有结果` : "—"}</span>;
  }
}

function SettleModal({ refund, onClose, onDone }: {
  refund: Refund;
  onClose: () => void;
  onDone: (summary: string) => void;
}) {
  const [outcome, setOutcome] = useState<"succeeded" | "failed" | null>(null);
  const [externalId, setExternalId] = useState(refund.external_id ?? "");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const ready = outcome !== null && note.trim().length >= 2;
  // "Failed" frees the order for another refund; until the job hands a refund over, a re-send or the
  // payment system finishing it could still pay the first one (the server refuses it too).
  const canFail = refund.follow_up === "person";

  async function submit() {
    if (!ready) return;
    setBusy(true);
    setError(null);
    try {
      await api.post(`/api/refunds/${refund.id}/settle`, { outcome, external_id: externalId.trim() || null, note: note.trim() });
      onDone(
        `退款 #${refund.id}（订单 ${refund.order_id}）已记为${outcome === "succeeded" ? "已退款" : "失败，订单已释放"}。` +
          "顾客不会自动收到通知；需要告知时，在关联工单里写明处理结果。",
      );
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        onDone(`退款 #${refund.id}：${e.message}（列表已刷新）`); // settled meanwhile, or not yet for a person
        return;
      }
      setError(e instanceof Error ? e.message : String(e));
      setBusy(false);
    }
  }

  return (
    <Modal title={`确认退款 #${refund.id} 的结果`} onClose={onClose}
      footer={<><button className="btn" onClick={onClose}>取消</button>
        <button className="btn btn-primary" disabled={busy || !ready} onClick={submit}>确认结果</button></>}>
      <div>
        <div className="small muted">
          订单 <span className="mono">{refund.order_id}</span> · {yuan(refund.amount)} · {refund.customer?.name ?? "—"} ·
          已发送 {refund.attempts} 次
        </div>
        {refund.error && <ErrorText error={refund.error} />}
      </div>
      <div className="refund-key">
        <span className="label">在支付后台查找用的幂等键</span>
        <code className="mono">{refund.idempotency_key ?? "—"}</code>
        {refund.external_id && <span className="faint">支付系统退款号 <span className="mono">{refund.external_id}</span></span>}
      </div>
      <div className="small muted">先在支付系统后台按幂等键（或支付系统退款号）核实，再记下看到的结果。只能确认一次，会记入操作审计。</div>
      <fieldset className="choices">
        <legend className="label">支付后台显示</legend>
        <label className="choice">
          <input type="radio" name="outcome" value="succeeded" autoFocus checked={outcome === "succeeded"}
            onChange={() => setOutcome("succeeded")} />
          <span><b>已退款</b><span className="faint"> · 订单保留这一次线上退款</span></span>
        </label>
        <label className={`choice ${canFail ? "" : "disabled"}`}>
          <input type="radio" name="outcome" value="failed" disabled={!canFail} checked={outcome === "failed"}
            onChange={() => setOutcome("failed")} aria-describedby={canFail ? undefined : "settle-why-not-failed"} />
          <span><b>没有这笔退款</b><span className="faint"> · 记为失败，订单释放，顾客可以重新申请</span></span>
        </label>
        {!canFail && (
          <div id="settle-why-not-failed" className="small muted">
            {refund.follow_up === "resend"
              ? `对账任务还在用原来的幂等键重发（还剩 ${duration(new Date(refund.resend_until ?? 0).getTime() - Date.now())}），之后的重发仍可能退款成功。现在只能记为已退款；记为失败要等它停止重发，否则顾客再申请可能退两次。`
              : "支付系统已经受理，对账任务在查询结果，它之后仍可能退款成功。现在只能记为已退款；没有结果时请等支付系统回复。"}
          </div>
        )}
      </fieldset>
      <label className="field">支付系统退款号（可选）
        <input className="input mono" maxLength={128} value={externalId} onChange={(e) => setExternalId(e.target.value)}
          placeholder="例如：RF20261020001" />
      </label>
      <label className="field">核实说明（必填，记入操作审计）
        <textarea className="textarea" maxLength={500} value={note} onChange={(e) => setNote(e.target.value)}
          placeholder="例如：支付后台查到 10-20 14:05 已原路退回" />
      </label>
      <ErrorBanner error={error} />
    </Modal>
  );
}

export default function RefundsPage() {
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const [tab, setTab] = useState<"open" | "all">("open");
  const { data, error, loading, reload } = useLoad(() => api.get<Refund[]>(`/api/refunds?status=${tab}`), [tab]);
  useEffect(() => {
    const timer = window.setInterval(() => void reload(), REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [reload]);
  const [settling, setSettling] = useState<Refund | null>(null);
  const [summary, setSummary] = useState<string | null>(null);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const notice = useRef<HTMLDivElement>(null);
  const now = Date.now();

  const close = () => {
    setSettling(null);
    trigger.current?.focus();
  };
  const done = (text: string) => {
    setSettling(null);
    setSummary(text);
    void reload();
  };
  // The settled row leaves the list, so focus goes to the result instead of the vanished button.
  useEffect(() => {
    if (summary) notice.current?.focus();
  }, [summary]);

  return (
    <div className="page stack" style={{ gap: 16 }}>
      <div className="page-head">
        <div>
          <h1>退款对账</h1>
          <p>
            支付系统还没有给出结果的退款，需要人工确认的排在前面。对账任务（按部署的定时任务运行，建议每 5 分钟）用同一个幂等键重发
            24 小时、超过 1 小时开工单；停止自动处理的，请在支付后台核实后{admin ? "" : "由管理员"}确认结果。
          </p>
        </div>
        <div className="tabs">
          <button className={tab === "open" ? "active" : ""} aria-pressed={tab === "open"} onClick={() => setTab("open")}>未结清</button>
          <button className={tab === "all" ? "active" : ""} aria-pressed={tab === "all"} onClick={() => setTab("all")}>全部</button>
        </div>
      </div>
      <ErrorBanner error={error} />
      {summary && <div ref={notice} className="notice" role="status" tabIndex={-1}>{summary}</div>}
      <div className="card table-wrap">
        <table className="table">
          <thead>
            <tr>
              <th>退款</th><th className="num">金额</th><th>状态</th><th>已等待</th><th>发送与最近错误</th><th>下一步</th>
              <th>工单</th>{admin && <th><span className="sr-only">操作</span></th>}
            </tr>
          </thead>
          <tbody>
            {data?.map((r) => {
              const [label, cls] = STATUS[r.status] ?? [r.status, ""];
              const waiting = settleable(r) || r.follow_up === "stuck"; // the demo tables' refunds wait for nothing
              return (
                <tr key={r.id}>
                  <td className="nowrap">
                    <b>#{r.id}</b> <span className="mono">{r.order_id}</span>
                    <div className="faint">
                      {r.customer?.name ?? "—"}
                      {r.external_id && <> · 支付退款号 <span className="mono">{r.external_id}</span></>}
                    </div>
                  </td>
                  <td className="nowrap num">{yuan(r.amount)}</td>
                  <td className="nowrap"><span className={`badge ${cls}`}>{label}</span></td>
                  <td className="nowrap" title={`提交于 ${fullTime(r.created_at)}`}>
                    {waiting ? duration(now - new Date(r.created_at).getTime()) : <span className="faint">{time(r.created_at)} 提交</span>}
                  </td>
                  <td className="refund-error">
                    <div>发送 {r.attempts} 次</div>
                    {r.error && <ErrorText error={r.error} />}
                    {waiting && r.idempotency_key && (
                      <div className="faint">幂等键 <code className="mono refund-key-inline">{r.idempotency_key}</code></div>
                    )}
                  </td>
                  <td className="refund-next"><NextStep refund={r} /></td>
                  <td className="nowrap">
                    {r.ticket_id ? <Link to={`/tickets?ticket=${r.ticket_id}`} aria-label={`查看工单 #${r.ticket_id}`}>#{r.ticket_id}</Link>
                      : <span className="faint">—</span>}
                  </td>
                  {admin && (
                    <td className="nowrap">
                      {settleable(r) && (
                        <button className="btn btn-sm" aria-label={`确认退款 #${r.id} 的结果`}
                          onClick={(e) => { trigger.current = e.currentTarget; setSettling(r); }}>
                          <ClipboardCheck size={14} /> 确认结果
                        </button>
                      )}
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
        {loading && !data && <Empty>加载中…</Empty>}
        {data && data.length === 0 && (
          <Empty>
            {tab === "open"
              ? "没有等待支付系统结果的退款。支付系统超时、报错或回复不合约定时，退款会停在“待确认”并出现在这里；演示订单表的退款不经过支付系统，可在“全部”里查看。"
              : "还没有退款记录"}
          </Empty>
        )}
      </div>
      {data && data.length >= LIMIT && (
        <div className="faint">
          {tab === "open" ? `最多显示 ${LIMIT} 笔：需要人工确认的排在前面，其余只显示最近的。` : `只显示最近 ${LIMIT} 笔。`}
        </div>
      )}
      {settling && <SettleModal refund={settling} onClose={close} onDone={done} />}
    </div>
  );
}
