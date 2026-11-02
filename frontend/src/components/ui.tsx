import { useCallback, useEffect, useId, useRef, useState, type KeyboardEvent as ReactKeyboardEvent, type ReactNode, type RefObject } from "react";
import { AlertTriangle, Inbox, X } from "lucide-react";
import { useLocation } from "react-router-dom";
import { useAuth } from "../auth";

export const TIER_TEXT: Record<string, string> = { fast: "快模型", strong: "强模型", cache: "答案缓存", rule: "规则（未调用模型）" };
export const tierText = (tier?: string | null) => TIER_TEXT[tier ?? ""] ?? tier ?? "—";

export const ROLE_TEXT: Record<string, string> = { admin: "管理员", agent: "客服坐席", customer: "顾客" };

export const TOOL_TEXT: Record<string, string> = {
  search_knowledge: "检索知识库",
  list_my_orders: "列出我的订单",
  get_order: "查询订单",
  track_shipment: "查询物流",
  request_refund: "发起退款",
  withdraw_refund_request: "撤回退款申请",
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
  withdrawn: ["已撤回", ""],
  proposed: ["待审核", "badge-warning"],
  applied: ["已发布", "badge-success"],
  candidate: ["候选", "badge-warning"],
  active: ["生效", "badge-success"],
  archived: ["已归档", ""],
};

export const CHANNEL_TEXT: Record<string, string> = { web: "网页", feishu: "飞书", api: "API", eval: "评测" };

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

const FOCUSABLE = 'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';
/** Open dialogs, most recent last: a confirmation or an edit dialog opened from a drawer is on top
 * whatever its place in the DOM. */
const openDialogs: HTMLElement[] = [];

/** Keeps keyboard focus inside a dialog while it is open (WCAG 2.4.3): focus moves in on open unless
 * a child already took it (autoFocus), Tab and Shift+Tab wrap around, Escape calls onClose, and when
 * the dialog goes away focus returns to the element that opened it, if nothing else has taken it. */
export function useFocusTrap(ref: RefObject<HTMLElement | null>, onClose: () => void) {
  const opener = useRef<Element | null>(null);
  if (opener.current === null && typeof document !== "undefined") opener.current = document.activeElement;
  const close = useRef(onClose);
  close.current = onClose;
  useEffect(() => {
    const box = ref.current;
    if (!box) return;
    openDialogs.push(box);
    if (!box.contains(document.activeElement)) {
      (box.querySelector<HTMLElement>("[data-autofocus]") ?? box.querySelector<HTMLElement>(FOCUSABLE) ?? box).focus();
    }
    const onKey = (e: KeyboardEvent) => {
      // Only the dialog opened last reacts: a confirmation opened over a drawer closes first.
      if (openDialogs[openDialogs.length - 1] !== box) return;
      if (e.key === "Escape") {
        e.preventDefault();
        close.current();
        return;
      }
      if (e.key !== "Tab") return;
      const items = [...box.querySelectorAll<HTMLElement>(FOCUSABLE)].filter((el) => el.offsetParent !== null || el === document.activeElement);
      if (!items.length) return;
      const first = items[0];
      const last = items[items.length - 1];
      if (e.shiftKey && (document.activeElement === first || !box.contains(document.activeElement))) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && (document.activeElement === last || !box.contains(document.activeElement))) {
        e.preventDefault();
        first.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      const at = openDialogs.indexOf(box);
      if (at >= 0) openDialogs.splice(at, 1);
      const back = opener.current;
      if (back instanceof HTMLElement && back.isConnected && (document.activeElement === document.body || document.activeElement === null)) back.focus();
    };
  }, [ref]);
}

export function Modal({ title, onClose, children, footer, size }: {
  title: string; onClose: () => void; children: ReactNode; footer?: ReactNode; size?: "sm";
}) {
  const box = useRef<HTMLDivElement>(null);
  useFocusTrap(box, onClose);
  return (
    <div className="modal-backdrop" onMouseDown={(e) => e.target === e.currentTarget && onClose()}>
      <div ref={box} className={`card modal ${size === "sm" ? "modal-sm" : ""}`} role="dialog" aria-modal="true" aria-label={title} data-dialog tabIndex={-1}>
        <div className="card-head">
          <h2>{title}</h2>
          <button className="icon-btn" onClick={onClose} aria-label="关闭">
            <X size={18} />
          </button>
        </div>
        <div className="card-pad stack">{children}</div>
        {footer && <div className="modal-foot">{footer}</div>}
      </div>
    </div>
  );
}

/** A confirmation in the console's own dialog (never window.confirm): the consequence in one or two
 * sentences, then cancel and the action. tone "danger" for deletions and money going out. */
export function ConfirmDialog({ title, children, confirmText, tone, busy, onConfirm, onClose }: {
  title: string; children: ReactNode; confirmText: string; tone?: "danger" | "primary"; busy?: boolean;
  onConfirm: () => void; onClose: () => void;
}) {
  return (
    <Modal title={title} onClose={onClose} size="sm"
      footer={<>
        <button className="btn" onClick={onClose} disabled={busy} data-autofocus>取消</button>
        <button className={`btn ${tone === "danger" ? "btn-danger-solid" : "btn-primary"}`} disabled={busy} onClick={onConfirm}>{confirmText}</button>
      </>}>
      <div className={`confirm-body ${tone === "danger" ? "danger" : ""}`}>
        <AlertTriangle size={20} aria-hidden="true" />
        <div className="stack" style={{ minWidth: 0, flex: 1 }}>{children}</div>
      </div>
    </Modal>
  );
}

export function Drawer({ title, onClose, children, footer, width }: {
  title: ReactNode; onClose: () => void; children: ReactNode; footer?: ReactNode; width?: number;
}) {
  const titleId = useId();
  const box = useRef<HTMLElement>(null);
  useFocusTrap(box, onClose);
  return (
    <>
      <div className="drawer-backdrop" onClick={onClose} />
      <aside ref={box} className="drawer" role="dialog" aria-modal="true" aria-labelledby={titleId} data-dialog tabIndex={-1}
        style={width ? { width: `min(${width}px, 100%)` } : undefined}>
        <div className="drawer-head">
          <div id={titleId} style={{ minWidth: 0 }}>{title}</div>
          <button className="icon-btn" onClick={onClose} aria-label="关闭">
            <X size={18} />
          </button>
        </div>
        <div className="drawer-body">{children}</div>
        {footer && <div className="drawer-foot">{footer}</div>}
      </aside>
    </>
  );
}

/** A small menu opened from a button: closes on Escape, a click outside, or choosing an item. */
export function Dropdown({ label, button, children, align, className }: {
  label: string; button: ReactNode; children: (close: () => void) => ReactNode; align?: "left" | "right"; className?: string;
}) {
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);
  const toggle = useRef<HTMLButtonElement>(null);
  const id = useId();
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => { if (!root.current?.contains(e.target as Node)) setOpen(false); };
    const key = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setOpen(false);
      toggle.current?.focus();
    };
    document.addEventListener("mousedown", away);
    window.addEventListener("keydown", key);
    return () => {
      document.removeEventListener("mousedown", away);
      window.removeEventListener("keydown", key);
    };
  }, [open]);
  return (
    <div ref={root} className={`menu ${className ?? ""}`}>
      <button ref={toggle} type="button" className="menu-toggle" aria-label={label} aria-expanded={open} aria-controls={open ? id : undefined}
        onClick={() => setOpen((v) => !v)}>
        {button}
      </button>
      {open && <div id={id} className={`menu-panel ${align === "left" ? "left" : ""}`}>{children(() => setOpen(false))}</div>}
    </div>
  );
}

const NAV_GROUP: Record<string, string> = {
  dashboard: "服务运营", chat: "服务运营", handoffs: "服务运营", approvals: "服务运营", tickets: "服务运营", refunds: "服务运营",
  knowledge: "知识与质量", evals: "知识与质量", flywheel: "知识与质量", traces: "运维审计", audit: "运维审计",
};

/** Page title block: breadcrumb trail (tenant / nav group by default), title, description, actions. */
export function PageHeader({ title, description, actions, crumbs: given, className }: {
  title: ReactNode; description?: ReactNode; actions?: ReactNode; crumbs?: string[]; className?: string;
}) {
  const { user } = useAuth();
  const group = NAV_GROUP[useLocation().pathname.split("/")[1] ?? ""];
  const crumbs = given ?? [user?.tenant.name ?? "", group ?? ""].filter(Boolean);
  return (
    <div className={`page-head ${className ?? ""}`}>
      <div className="page-title">
        {crumbs && crumbs.length > 0 && (
          <nav className="breadcrumb" aria-label="当前位置">{crumbs.map((c, i) => <span key={i}>{c}</span>)}</nav>
        )}
        <h1>{title}</h1>
        {description && <p>{description}</p>}
      </div>
      {actions && <div className="page-actions">{actions}</div>}
    </div>
  );
}

/** Filter buttons in a segmented control; each is a toggle button (aria-pressed). */
export function Segmented<T extends string>({ options, value, onChange, label }: {
  options: [T, ReactNode][]; value: T; onChange: (v: T) => void; label: string;
}) {
  return (
    <div className="segmented" role="group" aria-label={label}>
      {options.map(([key, text]) => (
        <button key={key} type="button" className={value === key ? "active" : ""} aria-pressed={value === key} onClick={() => onChange(key)}>{text}</button>
      ))}
    </div>
  );
}

export function Skeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div className="skeleton-rows" aria-label="加载中" role="status">
      {Array.from({ length: rows }, (_, i) => <span key={i} className="skeleton" />)}
    </div>
  );
}

export function Kpi({ label, value, sub, extra }: { label: string; value: ReactNode; sub?: ReactNode; extra?: ReactNode }) {
  return (
    <div className="card kpi">
      <div className="kpi-label">{label}</div>
      <div className="kpi-value">{value}</div>
      {extra}
      {sub && <div className="kpi-sub">{sub}</div>}
    </div>
  );
}

export function Json({ value }: { value: unknown }) {
  return <pre className="json mono">{JSON.stringify(value, null, 2)}</pre>;
}

export function Empty({ children, icon }: { children: ReactNode; icon?: ReactNode }) {
  return (
    <div className="empty">
      {icon !== null && <span className="empty-icon" aria-hidden="true">{icon ?? <Inbox size={22} />}</span>}
      <div>{children}</div>
    </div>
  );
}

export function ErrorBanner({ error }: { error: string | null }) {
  return error ? <div className="error-banner" role="alert">{error}</div> : null;
}

// How the console names a tenant's industry and greets its customers.
export const INDUSTRY_TEXT: Record<string, string> = { ecommerce: "电商零售", saas: "SaaS 服务", music: "音乐流媒体" };
export const CUSTOMER_GREETING: Record<string, string> = {
  ecommerce: "您好，可以咨询售后政策、查询订单和物流，或者申请退款。",
  saas: "您好，可以咨询产品功能、服务等级和工单处理。",
  music: "您好，可以咨询会员与自动续费、数字专辑、歌曲版权和账号问题，或者申请会员退款。",
};

export const pct = (v: number | null | undefined, digits = 0) => (v == null ? "—" : `${(v * 100).toFixed(digits)}%`);
export const ms = (v: number | null | undefined) => (v == null ? "—" : v >= 1000 ? `${(v / 1000).toFixed(1)}s` : `${v}ms`);
export const yuan = (v: unknown) => `¥${Number(v ?? 0).toFixed(2)}`;
const pad = (n: number) => String(n).padStart(2, "0");

/** Documents are numbered 1, 2, 3… (an edit names the one it was made on, so a save in between
 *  is refused) and shown as 1.00, 1.01, 1.02…: each save adds 0.01. */
export const docVersion = (n: number) => `${1 + Math.floor((n - 1) / 100)}.${pad((n - 1) % 100)}`;

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

/** A length of time for waits and deadlines: 5 分钟 / 3 小时 20 分钟 / 2 天 4 小时. */
export function duration(ms: number) {
  const minutes = Math.max(0, Math.round(ms / 60000));
  if (minutes < 60) return `${minutes} 分钟`;
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return `${hours} 小时 ${minutes % 60} 分钟`;
  return `${Math.floor(hours / 24)} 天 ${hours % 24} 小时`;
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

/** IntroPage's "下次直接进入": when set, / opens the console (or sign-in) directly. */
export const SKIP_INTRO_KEY = "loopdesk.skip-intro";

/** Show the intro at / again (its own links and the account menu's 产品介绍 call this). */
export function showIntroAgain() {
  try {
    localStorage.removeItem(SKIP_INTRO_KEY);
  } catch {
    /* storage unavailable: the intro shows anyway */
  }
}

type Theme = "system" | "light" | "dark";
const THEME_KEY = "loopdesk.theme";

// The blue-black workspace is the default; "system" follows the operating system.
function applyTheme(theme: Theme) {
  document.documentElement.setAttribute("data-theme", theme);
}

export function initTheme() {
  try {
    applyTheme((localStorage.getItem(THEME_KEY) as Theme) || "dark");
  } catch {
    applyTheme("dark"); // storage unavailable: the default theme
  }
}

export function useTheme(): [Theme, (t: Theme) => void] {
  const [theme, setTheme] = useState<Theme>(() => {
    try {
      return (localStorage.getItem(THEME_KEY) as Theme) || "dark";
    } catch {
      return "dark";
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

/** Keyboard support for a clickable element that cannot be a <button> (table rows, list
 * items): focusable with Tab, activated with Enter or Space like a button. */
export function pressable(onPress: () => void) {
  return {
    tabIndex: 0,
    onClick: onPress,
    onKeyDown: (e: ReactKeyboardEvent<HTMLElement>) => {
      if (e.target !== e.currentTarget || (e.key !== "Enter" && e.key !== " ")) return;
      e.preventDefault();
      onPress();
    },
  };
}

export const prefersReducedMotion = () => window.matchMedia("(prefers-reduced-motion: reduce)").matches;

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
