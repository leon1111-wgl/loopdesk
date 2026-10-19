import { useCallback, useEffect, useState, type ReactNode } from "react";
import { X } from "lucide-react";

export const TIER_TEXT: Record<string, string> = { fast: "快模型", strong: "强模型", cache: "答案缓存" };
export const tierText = (tier?: string | null) => TIER_TEXT[tier ?? ""] ?? tier ?? "—";

export const ROLE_TEXT: Record<string, string> = { admin: "管理员", agent: "客服坐席", customer: "顾客" };

export const TOOL_TEXT: Record<string, string> = {
  search_knowledge: "检索知识库",
  get_order: "查询订单",
  track_shipment: "查询物流",
  request_refund: "发起退款",
  create_ticket: "创建工单",
  handoff_to_human: "转人工",
};

const RUN_STATUS: Record<string, [string, string]> = {
  completed: ["已解决", "badge-success"],
  handoff: ["已转人工", "badge-info"],
  waiting_approval: ["等待审批", "badge-warning"],
  running: ["进行中", "badge-primary"],
  failed: ["失败", "badge-danger"],
  pending: ["待审批", "badge-warning"],
  approved: ["已通过", "badge-success"],
  rejected: ["已驳回", "badge-danger"],
  proposed: ["待审核", "badge-warning"],
  applied: ["已发布", "badge-success"],
  candidate: ["候选", "badge-warning"],
  active: ["生效", "badge-success"],
  archived: ["已归档", ""],
};

export function StatusBadge({ status }: { status: string }) {
  const [text, cls] = RUN_STATUS[status] ?? [status, ""];
  return <span className={`badge ${cls}`}>{text}</span>;
}

export function VerdictBadge({ verdict, coverage }: { verdict?: string | null; coverage?: number | null }) {
  if (!verdict) return <span className="faint">—</span>;
  const map: Record<string, [string, string]> = {
    pass: ["证据充分", "badge-success"],
    warn: ["部分无依据", "badge-warning"],
    blocked: ["证据冲突", "badge-danger"],
  };
  const [text, cls] = map[verdict] ?? [verdict, ""];
  return (
    <span className={`badge ${cls}`} title="逐句证据校验结果">
      {text}
      {coverage != null && ` · ${Math.round(coverage * 100)}%`}
    </span>
  );
}

export function Modal({ title, onClose, children, footer }: { title: string; onClose: () => void; children: ReactNode; footer?: ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div className="card modal" role="dialog" aria-label={title}>
        <div className="card-head">
          <h2>{title}</h2>
          <button className="icon-btn" onClick={onClose} aria-label="关闭">
            <X size={18} />
          </button>
        </div>
        <div className="card-pad stack">{children}</div>
        {footer && <div className="card-head" style={{ borderTop: "1px solid var(--border)", borderBottom: "none", justifyContent: "flex-end" }}>{footer}</div>}
      </div>
    </div>
  );
}

export function Drawer({ title, onClose, children }: { title: ReactNode; onClose: () => void; children: ReactNode }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);
  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <aside className="drawer" role="dialog">
        <div className="card-head" style={{ position: "sticky", top: 0, background: "var(--surface)", zIndex: 1 }}>
          <div style={{ minWidth: 0 }}>{title}</div>
          <button className="icon-btn" onClick={onClose} aria-label="关闭">
            <X size={18} />
          </button>
        </div>
        <div className="card-pad">{children}</div>
      </aside>
    </>
  );
}

export function Kpi({ label, value, sub }: { label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="card kpi">
      <div className="kpi-label">{label}</div>
      <div className="kpi-value">{value}</div>
      {sub && <div className="kpi-sub">{sub}</div>}
    </div>
  );
}

export function Json({ value }: { value: unknown }) {
  return <pre className="json mono">{JSON.stringify(value, null, 2)}</pre>;
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty">{children}</div>;
}

export function ErrorBanner({ error }: { error: string | null }) {
  return error ? <div className="error-banner">{error}</div> : null;
}

export const pct = (v: number | null | undefined, digits = 0) => (v == null ? "—" : `${(v * 100).toFixed(digits)}%`);
export const ms = (v: number | null | undefined) => (v == null ? "—" : v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${v}ms`);
export const yuan = (v: unknown) => `¥${Number(v ?? 0).toFixed(2)}`;
const pad = (n: number) => String(n).padStart(2, "0");

/** Friendly timestamps: 刚刚 / 5 分钟前 / 今天 14:05 / 昨天 09:12 / 10-08 14:05. */
export function time(iso?: string | null) {
  if (!iso) return "—";
  const d = new Date(iso);
  const now = new Date();
  const seconds = (now.getTime() - d.getTime()) / 1000;
  if (seconds >= 0 && seconds < 60) return "刚刚";
  if (seconds >= 0 && seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  if (d.getTime() >= startOfToday) return `今天 ${hm}`;
  if (d.getTime() >= startOfToday - 86400000) return `昨天 ${hm}`;
  return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`;
}

export function fullTime(iso?: string | null) {
  return iso ? new Date(iso).toLocaleString("zh-CN", { hour12: false }) : "—";
}

export function Logo({ size = 24 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" aria-hidden="true">
      <rect width="24" height="24" rx="5" fill="var(--primary)" />
      <path d="M7.5 12a4.5 4.5 0 1 1 4.5 4.5" stroke="#fff" strokeWidth="2" fill="none" strokeLinecap="round" />
      <path d="M12 16.5h-3" stroke="#fff" strokeWidth="2" strokeLinecap="round" />
      <circle cx="12" cy="12" r="1.6" fill="#fff" />
    </svg>
  );
}

type Theme = "system" | "light" | "dark";
const THEME_KEY = "loopdesk.theme";

function applyTheme(theme: Theme) {
  if (theme === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", theme);
}

export function initTheme() {
  try {
    applyTheme((localStorage.getItem(THEME_KEY) as Theme) || "system");
  } catch {
    /* storage unavailable: follow the system */
  }
}

export function useTheme(): [Theme, (t: Theme) => void] {
  const [theme, setTheme] = useState<Theme>(() => {
    try {
      return (localStorage.getItem(THEME_KEY) as Theme) || "system";
    } catch {
      return "system";
    }
  });
  const update = useCallback((t: Theme) => {
    setTheme(t);
    applyTheme(t);
    try {
      localStorage.setItem(THEME_KEY, t);
    } catch {
      /* ignore */
    }
  }, []);
  return [theme, update];
}

/** Track a CSS media query, e.g. the 780px breakpoint where the sidebar becomes a drawer. */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const mql = window.matchMedia(query);
    const onChange = () => setMatches(mql.matches);
    onChange();
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [query]);
  return matches;
}

/** Load data with loading/error state and a reload function. */
export function useLoad<T>(loader: () => Promise<T>, deps: unknown[] = []) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(loader, deps);
  const reload = useCallback(async () => {
    setLoading(true);
    try {
      setData(await run());
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [run]);
  useEffect(() => {
    void reload();
  }, [reload]);
  return { data, error, loading, reload, setData };
}
