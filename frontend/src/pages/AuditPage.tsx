import { useState } from "react";
import { api } from "../api";
import { Empty, ErrorBanner, time, useLoad } from "../components/ui";

interface AuditEntry {
  id: number;
  action: string;
  action_label: string;
  target: string;
  detail: Record<string, unknown>;
  request_id: string;
  created_at: string;
  actor: { id: number; name: string; email: string } | null;
}

const LABEL: Record<string, string> = {
  auto_refund_limit: "自动退款额度",
  ticket_sla_hours: "工单时限（小时）",
  return_window_days: "无理由退货期（天）",
  quality_window_days: "质量问题退货期（天）",
  title: "标题",
  visibility: "可见范围",
  status: "状态",
  question: "问题",
  must_include: "必含内容",
  must_not_include: "禁含内容",
  expected_tools: "期望工具",
  version: "版本",
  from_version: "原版本",
  to_version: "新版本",
  file: "文件",
  content: "内容",
  document_version: "补丁文档版本",
  acknowledged_conflicts: "确认发布的冲突",
  key: "用例",
  sentence: "句子",
  other_sentence: "另一句",
  note: "备注",
  erased: "抹除的记录",
  approval_admin_threshold: "审批分级线（元）",
  timezone: "时区",
  tool: "工具",
  order_id: "订单号",
  amount: "金额（元）",
  comment: "意见",
  decision: "原决定",
  error: "错误类型",
  days: "天数",
  rows: "行数",
  conversation_id: "会话",
  message_id: "消息",
  overdue: "超过承诺时限",
};

const VALUE: Record<string, string> = {
  public: "公开", internal: "仅内部", active: "生效", candidate: "候选", archived: "已归档",
  approve: "通过", reject: "驳回", request_refund: "退款",
};

const CATEGORIES: [string, string][] = [
  ["", "全部类别"], ["tenant", "租户配置"], ["kb", "知识库"], ["flywheel", "知识补丁"], ["eval", "评测用例"],
  ["user", "账号与个人数据"], ["approval", "审批"], ["export", "数据导出"], ["handoff", "人工接待"], ["ticket", "工单"],
];

function show(value: unknown): string {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (typeof value === "string" && VALUE[value]) return VALUE[value];
  if (Array.isArray(value)) return value.map(show).join("、");
  if (typeof value === "object") return Object.entries(value).map(([k, v]) => `${k} ${show(v)}`).join("，");
  return String(value);
}

function Detail({ detail }: { detail: Record<string, unknown> }) {
  const { changes, diff, ...rest } = detail as { changes?: Record<string, [unknown, unknown]>; diff?: string };
  return (
    <div className="stack" style={{ gap: 4 }}>
      {changes && Object.entries(changes).map(([field, [before, after]]) => (
        <div key={field}>{LABEL[field] ?? field}：<span className="faint">{show(before)}</span> → <b>{show(after)}</b></div>
      ))}
      {Object.entries(rest).map(([key, value]) => (
        <div key={key} className="small"><span className="faint">{LABEL[key] ?? key}：</span>{show(value)}</div>
      ))}
      {diff && (
        <details>
          <summary className="small">查看修改的行</summary>
          <pre className="json small" style={{ whiteSpace: "pre-wrap", margin: "6px 0 0" }}>{diff}</pre>
        </details>
      )}
    </div>
  );
}

interface Integrity {
  ok: boolean;
  entries: number;
  last_seq: number;
  altered: number[];
  missing: number[];
  unsigned: number;
}

function IntegrityNotice() {
  const { data, error } = useLoad(() => api.get<Integrity>("/api/audit/verify"));
  if (error) return <div className="error-banner" role="alert">无法完成完整性校验：{error}</div>;
  if (!data) return null;
  if (data.ok) {
    return (
      <div className="notice" role="status">
        完整性校验通过：{data.last_seq} 条记录编号连续、签名一致{data.unsigned ? `（另有 ${data.unsigned} 条早于签名功能的记录）` : ""}。
      </div>
    );
  }
  return (
    <div className="error-banner" role="alert">
      完整性校验未通过：{data.altered.length ? `${data.altered.length} 条记录内容与签名不符` : ""}
      {data.altered.length && data.missing.length ? "，" : ""}
      {data.missing.length ? `缺少编号 ${data.missing.join("、")}` : ""}
      {(data.altered.length || data.missing.length) && data.unsigned ? "，" : ""}
      {data.unsigned ? `${data.unsigned} 条记录没有签名（升级前的旧记录可由运维补签一次；否则说明有人直接写过审计表）` : ""}。
      审计记录可能被直接改动过数据库。
    </div>
  );
}

export default function AuditPage() {
  const [category, setCategory] = useState("");
  const { data, error } = useLoad(
    () => api.get<AuditEntry[]>(`/api/audit?limit=200${category ? `&category=${category}` : ""}`), [category]);
  return (
    <div className="page stack" style={{ gap: 16 }}>
      <div className="page-head">
        <div>
          <h1>操作审计</h1>
          <p>管理员对风控额度、知识文档、知识补丁、评测用例和个人数据的修改，以及员工的审批决定、数据导出、人工接待和工单处理：谁、什么时候、做了什么。Agent 自己的操作见链路追踪。</p>
        </div>
        <div className="row">
          <select className="select" style={{ width: 140 }} value={category} onChange={(e) => setCategory(e.target.value)}
            aria-label="审计记录类别">
            {CATEGORIES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
          </select>
        </div>
      </div>
      <ErrorBanner error={error} />
      <IntegrityNotice />
      <div className="card table-wrap">
        <table className="table">
          <thead><tr><th>时间</th><th>操作人</th><th>操作</th><th>对象</th><th>内容</th></tr></thead>
          <tbody>
            {data?.map((e) => (
              <tr key={e.id}>
                <td className="faint nowrap" title={`请求 ID：${e.request_id}`}>{time(e.created_at)}</td>
                <td>{e.actor ? <>{e.actor.name}<div className="faint small">{e.actor.email}</div></> : <span className="faint">—</span>}</td>
                <td className="nowrap">{e.action_label}</td>
                <td className="faint">{e.target}</td>
                <td><Detail detail={e.detail} /></td>
              </tr>
            ))}
          </tbody>
        </table>
        {data && data.length === 0 && <Empty>{category ? "这一类还没有记录" : "还没有审计记录"}</Empty>}
      </div>
    </div>
  );
}
