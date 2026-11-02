import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { Pencil, Power, PowerOff, Wrench } from "lucide-react";
import { api, ApiError } from "../api";
import { useAuth } from "../auth";
import { ConfirmDialog, Drawer, Empty, ErrorBanner, PageHeader, Skeleton, TOOL_TEXT, time, useLoad } from "../components/ui";

// The tools a tenant declares (GET /api/admin/tools): HTTP bindings to its own systems and synced MCP tools,
// each with a risk tier and an approval policy, next to the built-in ones. Administrators edit the policy
// (PUT) and switch a tool on or off (PATCH); agents read. A money tool is never looser than the tenant's
// refund tiers: the server refuses it (N-228) and this page shows the tiers it is held to.

type Risk = "read" | "write" | "money";
type Approval = {
  mode?: "never" | "threshold" | "always";
  approver?: "agent" | "admin";
  amount_field?: string;
  auto_limits?: Record<string, number>;
  admin_above?: Record<string, number>;
  customer_note?: string;
};
type Stats = { calls: number; errors: number; last_called_at: string | null; last_error: string | null; last_error_at: string | null };
type Tool = {
  id: number; name: string; label: string; description: string; kind: "http" | "mcp"; parameters: Record<string, unknown>;
  binding: { method?: string; url?: string; [key: string]: unknown }; risk: Risk; approval: Approval; roles: string[];
  flags: Record<string, unknown>; hints: Record<string, unknown>; enabled: boolean; status: string; version: number;
  updated_at: string | null; stats: Stats; pending_approvals: number;
};
type Builtin = { name: string; label: string; description: string; risk: Risk; roles: string[]; stats: Stats; pending_approvals: number };
type Tiers = { currency: string; auto_refund_limit: number; approval_admin_threshold: number };
type Listing = { tools: Tool[]; builtin: Builtin[]; tool_hosts: string[]; tiers: Tiers; stats_days: number };

const RISK: Record<Risk, [string, string]> = { read: ["只读", "badge-info"], write: ["写入", "badge-warning"], money: ["资金", "badge-danger"] };
const APPROVER: Record<string, string> = { agent: "坐席", admin: "管理员" };
const money = (v: number | undefined, currency: string) =>
  v == null ? "—" : `${currency === "CNY" ? "¥" : `${currency} `}${Number(v).toLocaleString("zh-CN", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

function policyText(risk: Risk, a: Approval, tiers: Tiers): string {
  if (risk === "read" || a.mode === "never") return "无需审批";
  const cur = tiers.currency;
  const approver = APPROVER[a.approver ?? "agent"];
  const admin = a.admin_above?.[cur];
  const top = admin != null && a.approver !== "admin" ? `；超过 ${money(admin, cur)} 仅管理员` : "";
  if (a.mode === "always") return `每笔由${approver}审批${top}`;
  return `${money(a.auto_limits?.[cur], cur)} 以内自动办理；超过由${approver}审批${top}`;
}

function builtinPolicy(b: Builtin, tiers: Tiers): string {
  if (b.name === "request_refund") {
    return `按退款分级：${money(tiers.auto_refund_limit, tiers.currency)} 以内自动；超过由坐席审批；超过 ${money(tiers.approval_admin_threshold, tiers.currency)} 仅管理员`;
  }
  return b.risk === "read" ? "无需审批" : "执行后记入链路，不动钱";
}

function host(url?: string): string {
  try {
    return url ? new URL(url).host : "";
  } catch {
    return url ?? "";
  }
}

function Calls({ stats, days }: { stats: Stats; days: number }) {
  if (!stats.calls) return <span className="faint">近 {days} 天无调用</span>;
  return (
    <>
      <div>{stats.calls} 次{stats.errors > 0 && <span className="tool-errors"> · 失败 {stats.errors}</span>}</div>
      <div className="faint">最近 {time(stats.last_called_at)}</div>
    </>
  );
}

function LastError({ stats }: { stats: Stats }) {
  if (!stats.last_error) return <span className="faint">—</span>;
  return (
    <div className="tool-last-error">
      <div title={stats.last_error}>{stats.last_error}</div>
      <div className="faint">{time(stats.last_error_at)}</div>
    </div>
  );
}

/** The policy form: what an administrator may change without touching the binding (which stays API-only). */
function EditDrawer({ tool, tiers, onClose, onSaved }: {
  tool: Tool; tiers: Tiers; onClose: () => void; onSaved: (t: Tool, note: string) => void;
}) {
  const cur = tiers.currency;
  const [label, setLabel] = useState(tool.label);
  const [description, setDescription] = useState(tool.description);
  const [risk, setRisk] = useState<Risk>(tool.risk);
  const [mode, setMode] = useState(tool.approval.mode ?? (tool.risk === "read" ? "never" : "always"));
  const [approver, setApprover] = useState(tool.approval.approver ?? "agent");
  const [auto, setAuto] = useState(String(tool.approval.auto_limits?.[cur] ?? ""));
  const [adminAbove, setAdminAbove] = useState(String(tool.approval.admin_above?.[cur] ?? ""));
  const [note, setNote] = useState(tool.approval.customer_note ?? "");
  const [enabled, setEnabled] = useState(tool.enabled);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mcp = tool.kind === "mcp";
  const amounted = mode === "threshold" || (risk === "money" && mode !== "never");

  /** The tenant's refund tiers, checked here too so the form says it at once (the server refuses the same). */
  function looser(): string | null {
    if (risk !== "money" || mode === "never") return null;
    if (mode === "threshold" && auto.trim() !== "" && Number(auto) > tiers.auto_refund_limit) {
      return `资金类工具的自动办理额度不能高于本租户的自动退款额度 ${money(tiers.auto_refund_limit, cur)}（现在是 ${money(Number(auto), cur)}）`;
    }
    if (adminAbove.trim() !== "" && Number(adminAbove) > tiers.approval_admin_threshold) {
      return `资金类工具超过 ${money(tiers.approval_admin_threshold, cur)} 必须由管理员审批，和本租户的退款分级一致（现在是 ${money(Number(adminAbove), cur)}）`;
    }
    return null;
  }

  async function save() {
    const problem = looser();
    if (problem) {
      setError(problem);
      return;
    }
    setBusy(true);
    setError(null);
    const approval: Approval = { ...tool.approval, mode: risk === "read" ? "never" : mode, approver, customer_note: note.trim() };
    if (risk === "read" || mode === "never") {
      delete approval.auto_limits;
      delete approval.admin_above;
    } else {
      approval.auto_limits = mode === "threshold" && auto.trim() !== "" ? { [cur]: Number(auto) } : undefined;
      approval.admin_above = adminAbove.trim() !== "" ? { [cur]: Number(adminAbove) } : undefined;
      if (!approval.auto_limits) delete approval.auto_limits;
      if (!approval.admin_above) delete approval.admin_above;
    }
    try {
      const saved = await api.put<Tool>(`/api/admin/tools/${tool.id}`, {
        name: tool.name, label: label.trim(), description: description.trim(), kind: tool.kind, parameters: tool.parameters,
        binding: tool.binding, risk, approval, roles: tool.roles, flags: tool.flags, hints: tool.hints, enabled,
      });
      onSaved(saved, `已保存 ${saved.label}（版本 ${saved.version}），改动记入操作审计。`);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
      setBusy(false);
    }
  }

  return (
    <Drawer title={<><b>编辑工具</b><div className="faint mono">{tool.name} · 版本 {tool.version}</div></>} onClose={onClose} width={520}
      footer={<><button className="btn" onClick={onClose}>取消</button>
        <button className="btn btn-primary" disabled={busy || !label.trim()} onClick={save}>保存</button></>}>
      <div className="stack tool-form" style={{ gap: 14 }}>
        <div className="tool-binding">
          <span className="label">{mcp ? "MCP 工具" : "调用"}</span>
          <code className="mono">{mcp ? tool.binding.url ?? tool.name : `${tool.binding.method ?? "GET"} ${tool.binding.url ?? ""}`}</code>
          <span className="faint">{mcp ? "定义来自 MCP Server，只能改风险和审批策略。" : "地址、鉴权和返回字段通过接口声明（POST /api/admin/tools），这里不改。"}</span>
        </div>
        <label className="field">显示名称（顾客和审批人看到的名字）
          <input className="input" maxLength={32} value={label} onChange={(e) => setLabel(e.target.value)} />
        </label>
        <label className="field">说明（模型据此判断何时调用）
          <textarea className="textarea" rows={3} maxLength={1000} value={description} disabled={mcp}
            onChange={(e) => setDescription(e.target.value)} />
        </label>
        <div className="grid grid-2">
          <label className="field">风险等级
            <select className="select" value={risk} onChange={(e) => setRisk(e.target.value as Risk)}>
              <option value="read">只读</option><option value="write">写入</option><option value="money">资金</option>
            </select>
          </label>
          <label className="field">审批方式
            <select className="select" value={risk === "read" ? "never" : mode} disabled={risk === "read"}
              onChange={(e) => setMode(e.target.value as NonNullable<Approval["mode"]>)}>
              {risk !== "money" && <option value="never">无需审批</option>}
              <option value="threshold">按金额：额度内自动</option>
              <option value="always">每笔人工审批</option>
            </select>
          </label>
        </div>
        {risk !== "read" && mode !== "never" && (
          <div className="grid grid-2">
            {mode === "threshold" && (
              <label className="field">自动办理上限（{cur}）
                <input className="input num" type="number" min={0} step="0.01" value={auto} onChange={(e) => setAuto(e.target.value)} />
              </label>
            )}
            <label className="field">审批人
              <select className="select" value={approver} onChange={(e) => setApprover(e.target.value as "agent" | "admin")}>
                <option value="agent">坐席</option><option value="admin">管理员</option>
              </select>
            </label>
            {amounted && (
              <label className="field">超过此金额仅管理员（{cur}）
                <input className="input num" type="number" min={0} step="0.01" value={adminAbove}
                  placeholder={risk === "money" ? String(tiers.approval_admin_threshold) : ""} onChange={(e) => setAdminAbove(e.target.value)} />
              </label>
            )}
          </div>
        )}
        {risk === "money" && (
          <div className="notice tool-tiers" role="note">
            资金类工具不能比本租户的退款分级更松：自动办理不超过 {money(tiers.auto_refund_limit, cur)}，
            超过 {money(tiers.approval_admin_threshold, cur)} 只有管理员能审批；其他币种的金额一律由管理员审批。
          </div>
        )}
        {risk !== "read" && mode !== "never" && (
          <label className="field">给顾客看的说明（可选）
            <input className="input" maxLength={200} value={note} onChange={(e) => setNote(e.target.value)}
              placeholder="例如：超过 20 元的补偿券需要人工核实" />
          </label>
        )}
        <label className="choice">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} disabled={tool.status === "held"} />
          <span><b>启用</b><span className="faint"> · 停用后下一轮对话起不再调用；它的待审批单不能再通过，只能驳回</span></span>
        </label>
        <ErrorBanner error={error} />
      </div>
    </Drawer>
  );
}

export default function ToolsPage() {
  const { user } = useAuth();
  const admin = user?.role === "admin";
  const { data, error, loading, reload, setData } = useLoad(() => api.get<Listing>("/api/admin/tools"), []);
  const [editing, setEditing] = useState<Tool | null>(null);
  const [switching, setSwitching] = useState<Tool | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const trigger = useRef<HTMLButtonElement | null>(null);
  const noticeRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (notice) noticeRef.current?.focus();
  }, [notice]);

  const tiers = data?.tiers ?? { currency: "CNY", auto_refund_limit: 0, approval_admin_threshold: 0 };
  const replace = (t: Tool) => setData((d) => (d ? { ...d, tools: d.tools.map((x) => (x.id === t.id ? { ...x, ...t } : x)) } : d));

  async function toggle(tool: Tool) {
    setBusy(true);
    setActionError(null);
    try {
      const saved = await api.patch<Tool & { pending_approvals?: number | number[] }>(`/api/admin/tools/${tool.id}`, { enabled: !tool.enabled });
      const waiting = Array.isArray(saved.pending_approvals) ? saved.pending_approvals : [];
      replace({ ...saved, pending_approvals: waiting.length || tool.pending_approvals, stats: tool.stats });
      setNotice(saved.enabled ? `已启用 ${saved.label}，下一轮对话起可以调用。`
        : `已停用 ${saved.label}。${waiting.length ? `它的 ${waiting.length} 张待审批单（#${waiting.join("、#")}）不能再通过，请驳回并告知顾客。` : ""}`);
    } catch (e) {
      setActionError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
      setSwitching(null);
    }
  }

  return (
    <div className="page stack" style={{ gap: 16 }}>
      <PageHeader title="工具集成"
        description={<>租户自己声明的工具：调用商户系统的 HTTP 接口，或同步自 MCP Server。每个工具有风险等级和审批策略；资金类工具受本租户退款分级约束，
          所有调用记入链路追踪。{admin ? "" : "坐席只能查看，修改需要管理员。"}</>} />
      <ErrorBanner error={error ?? actionError} />
      {notice && <div ref={noticeRef} className="notice" role="status" tabIndex={-1}>{notice}</div>}
      {data && (
        <div className="grid grid-3 tool-summary">
          <div className="card card-pad">
            <div className="label">出站白名单（平台运营方设置）</div>
            <div className="row row-wrap" style={{ marginTop: 6 }}>
              {data.tool_hosts.length ? data.tool_hosts.map((h) => <code key={h} className="mono tool-host">{h}</code>)
                : <span className="faint">未设置：HTTP 工具不能调用任何外部地址</span>}
            </div>
          </div>
          <div className="card card-pad">
            <div className="label">资金类工具的上限（= 退款分级）</div>
            <div style={{ marginTop: 6 }}>自动办理 ≤ <b className="num">{money(tiers.auto_refund_limit, tiers.currency)}</b>
              ，超过 <b className="num">{money(tiers.approval_admin_threshold, tiers.currency)}</b> 仅管理员</div>
          </div>
          <div className="card card-pad">
            <div className="label">调用统计</div>
            <div style={{ marginTop: 6 }}>近 {data.stats_days} 天的顾客与坐席对话，不含评测运行</div>
          </div>
        </div>
      )}
      <div className="card table-wrap">
        <div className="card-head"><h2>租户声明的工具</h2><span className="faint">演示租户的商户系统是虚构的 .example 地址（SYNTHETIC）</span></div>
        <table className="table">
          <thead>
            <tr><th>工具</th><th>风险</th><th>审批策略</th><th>状态</th><th>调用</th><th>最近错误</th><th>待审批</th>
              {admin && <th><span className="sr-only">操作</span></th>}</tr>
          </thead>
          <tbody>
            {data?.tools.map((t) => {
              const [riskText, riskCls] = RISK[t.risk] ?? [t.risk, ""];
              return (
                <tr key={t.id}>
                  <td>
                    <b>{t.label}</b> <span className="badge">{t.kind === "mcp" ? "MCP" : "HTTP"}</span>
                    <div className="faint"><span className="mono">{t.name}</span>{host(t.binding.url) && <> · {host(t.binding.url)}</>}</div>
                  </td>
                  <td className="nowrap"><span className={`badge ${riskCls}`}>{riskText}</span></td>
                  <td className="tool-policy">{policyText(t.risk, t.approval, tiers)}</td>
                  <td className="nowrap">
                    {t.status !== "active" ? <span className="badge badge-warning">{t.status === "held" ? "被拦下" : "待复核"}</span>
                      : t.enabled ? <span className="badge badge-success">已启用</span> : <span className="badge">已停用</span>}
                  </td>
                  <td className="nowrap"><Calls stats={t.stats} days={data.stats_days} /></td>
                  <td><LastError stats={t.stats} /></td>
                  <td className="nowrap">
                    {t.pending_approvals ? <Link to="/approvals" aria-label={`${t.label}有 ${t.pending_approvals} 张待审批单`}>{t.pending_approvals} 张</Link>
                      : <span className="faint">—</span>}
                  </td>
                  {admin && (
                    <td className="nowrap">
                      <div className="row">
                        <button className="btn btn-sm" aria-label={`编辑 ${t.label}`}
                          onClick={(e) => { trigger.current = e.currentTarget; setEditing(t); }}><Pencil size={14} /> 编辑</button>
                        <button className="btn btn-sm" disabled={busy || (!t.enabled && t.status !== "active")}
                          aria-label={`${t.enabled ? "停用" : "启用"} ${t.label}`}
                          onClick={(e) => { trigger.current = e.currentTarget; if (t.enabled) setSwitching(t); else void toggle(t); }}>
                          {t.enabled ? <><PowerOff size={14} /> 停用</> : <><Power size={14} /> 启用</>}
                        </button>
                      </div>
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
        {loading && !data && <Skeleton rows={3} />}
        {data && data.tools.length === 0 && (
          <Empty icon={<Wrench size={20} />}>
            这个租户还没有声明工具。管理员通过 <code className="mono">POST /api/admin/tools</code> 声明 HTTP 工具，或接入 MCP Server 同步；
            新工具的地址必须在出站白名单内。
          </Empty>
        )}
      </div>
      {data && (
        <div className="card table-wrap">
          <div className="card-head"><h2>内置工具</h2><span className="faint">随产品提供，不能停用；退款按租户设置里的退款分级审批</span></div>
          <table className="table">
            <thead><tr><th>工具</th><th>风险</th><th>审批策略</th><th>调用</th><th>最近错误</th><th>待审批</th></tr></thead>
            <tbody>
              {data.builtin.map((b) => {
                const [riskText, riskCls] = RISK[b.risk] ?? [b.risk, ""];
                return (
                  <tr key={b.name}>
                    <td><b>{TOOL_TEXT[b.name] ?? b.label}</b><div className="faint mono">{b.name}</div></td>
                    <td className="nowrap"><span className={`badge ${riskCls}`}>{riskText}</span></td>
                    <td className="tool-policy">{builtinPolicy(b, tiers)}</td>
                    <td className="nowrap"><Calls stats={b.stats} days={data.stats_days} /></td>
                    <td><LastError stats={b.stats} /></td>
                    <td className="nowrap">
                      {b.pending_approvals ? <Link to="/approvals">{b.pending_approvals} 张</Link> : <span className="faint">—</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {editing && (
        <EditDrawer tool={editing} tiers={tiers} onClose={() => { setEditing(null); trigger.current?.focus(); }}
          onSaved={(t, note) => { setEditing(null); replace({ ...t, stats: editing.stats, pending_approvals: editing.pending_approvals }); setNotice(note); void reload(); }} />
      )}
      {switching && (
        <ConfirmDialog title={`停用 ${switching.label}？`} confirmText="停用" tone="danger" busy={busy}
          onConfirm={() => void toggle(switching)} onClose={() => { setSwitching(null); trigger.current?.focus(); }}>
          停用后，从下一轮对话起智能客服不再调用它。
          {switching.pending_approvals > 0 && <> 它还有 <b>{switching.pending_approvals}</b> 张待审批单：停用后这些单子不能再通过，只能驳回并告知顾客。</>}
        </ConfirmDialog>
      )}
    </div>
  );
}
