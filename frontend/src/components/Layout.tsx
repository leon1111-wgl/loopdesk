import { useEffect, useRef, useState, type ReactNode } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import {
  Activity,
  BookOpen,
  Check,
  ChevronDown,
  ChevronsLeft,
  ChevronsRight,
  ClipboardCheck,
  FlaskConical,
  Github,
  Headset,
  Info,
  LayoutDashboard,
  LogOut,
  Menu,
  MessagesSquare,
  Monitor,
  Moon,
  ReceiptText,
  RefreshCcw,
  ScrollText,
  Ticket,
  Sun,
  X,
} from "lucide-react";
import { api } from "../api";
import { isStaff, useAuth } from "../auth";
import { REPO_URL, isDemo, recording } from "../demo/replay";
import type { Approval, Handoff, User } from "../types";
import { Dropdown, INDUSTRY_TEXT, Logo, ROLE_TEXT, useMediaQuery, useTheme } from "./ui";

interface NavItem {
  to: string;
  label: string;
  icon: ReactNode;
  count?: number;
}

interface DemoAccounts {
  accounts: { email: string; name: string; role: string; tenant: string }[];
  password: string | null;
}

const NAV_KEY = "loopdesk.nav-collapsed";

function DemoNotice() {
  const rec = recording();
  const source = rec && !rec.model.offline ? (
    <>
      回答由真实模型 <b>{rec.model.strong}</b> 生成，{rec.date} 录制自真实后端
      {rec.commit && <>（提交 <a href={`${REPO_URL}/commit/${rec.commit}`} target="_blank" rel="noreferrer" style={{ marginLeft: 0 }}>{rec.commit}</a>）</>}
      ，数据均为虚构
    </>
  ) : (
    <>页面数据录制自真实后端（离线模型、虚构数据）</>
  );
  return (
    <div className="demo-notice" role="note" data-model={rec?.model.strong}>
      <Info size={14} aria-hidden="true" />
      <div><strong>演示工作区</strong><span className="demo-source">{source}。</span>
        <details><summary>使用说明与运行方式</summary><p>可以切换角色浏览、回放对话页里的示例问题；提交类操作不会生效。
          <a href={REPO_URL} target="_blank" rel="noreferrer">在本地运行完整版本 ↗</a></p></details>
      </div>
    </div>
  );
}

/** The workspace in the top bar. With the demo accounts available it also switches tenant, signing in
 * with the same role there (the demo password is public); otherwise it only says where you are. */
function TenantSwitcher({ user }: { user: User }) {
  const { login, logout } = useAuth();
  const [demo, setDemo] = useState<DemoAccounts | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api.get<DemoAccounts>("/api/auth/demo-accounts").then(setDemo).catch(() => setDemo(null));
  }, []);
  const others = demo?.password
    ? [...new Set(demo.accounts.map((a) => a.tenant))].filter((t) => t !== user.tenant.name)
    : [];
  const industry = INDUSTRY_TEXT[user.tenant.industry] ?? "企业";
  async function switchTo(tenant: string) {
    const accounts = demo?.accounts.filter((a) => a.tenant === tenant) ?? [];
    const target = accounts.find((a) => a.role === user.role) ?? accounts.find((a) => a.role === "admin");
    if (!target || !demo?.password) return;
    try {
      await login(target.email, demo.password);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }
  return (
    <Dropdown label={`当前租户：${user.tenant.name}，切换租户`} align="left" className="tenant-switch"
      button={<>
        <span className="workspace-mark" aria-hidden="true">{user.tenant.name.slice(0, 1)}</span>
        <span className="tenant-name ellipsis">{user.tenant.name}</span>
        <span className="tenant-industry hide-compact">{industry}</span>
        <ChevronDown size={14} aria-hidden="true" />
      </>}>
      {(close) => (
        <>
          <div className="menu-label">当前工作区</div>
          <div className="menu-item active" aria-current="true">
            <span className="workspace-mark sm" aria-hidden="true">{user.tenant.name.slice(0, 1)}</span>
            <span className="stack" style={{ gap: 0 }}><span>{user.tenant.name}</span><span className="faint">{industry} · 演示租户</span></span>
            <Check size={14} style={{ marginLeft: "auto" }} aria-hidden="true" />
          </div>
          {others.length > 0 && <div className="menu-label">切换到其他演示租户（以{ROLE_TEXT[user.role]}身份）</div>}
          {others.map((tenant) => (
            <button key={tenant} className="menu-item" onClick={() => { close(); void switchTo(tenant); }}>
              <span className="workspace-mark sm" aria-hidden="true">{tenant.slice(0, 1)}</span>{tenant}
            </button>
          ))}
          {error && <div className="menu-label" role="alert" style={{ color: "var(--danger)" }}>{error}</div>}
          <div className="menu-sep" />
          <button className="menu-item" onClick={() => { close(); logout(); }}><LogOut size={14} aria-hidden="true" /> 换个账号登录</button>
        </>
      )}
    </Dropdown>
  );
}

function UserMenu({ user }: { user: User }) {
  const { logout } = useAuth();
  return (
    <Dropdown label="账号菜单" className="user-menu"
      button={<>
        <span className="avatar me" aria-hidden="true">{user.name.slice(0, 1)}</span>
        <span className="user-name hide-compact">{user.name}</span>
        <ChevronDown size={14} className="hide-compact" aria-hidden="true" />
      </>}>
      {() => (
        <>
          <div className="user-card">
            <span className="avatar me" aria-hidden="true">{user.name.slice(0, 1)}</span>
            <div style={{ minWidth: 0 }}>
              <div className="ellipsis" style={{ fontWeight: 600 }}>{user.name}</div>
              <div className="faint ellipsis">{ROLE_TEXT[user.role]} · {user.email}</div>
            </div>
          </div>
          <div className="menu-sep" />
          <a className="menu-item" href={REPO_URL} target="_blank" rel="noreferrer"><Github size={14} aria-hidden="true" /> 项目源码</a>
          <button className="menu-item" onClick={logout}><LogOut size={14} aria-hidden="true" /> 退出登录</button>
        </>
      )}
    </Dropdown>
  );
}

export default function Layout() {
  const { user } = useAuth();
  const location = useLocation();
  const staff = isStaff(user);
  const [pending, setPending] = useState(0);
  const [waiting, setWaiting] = useState(0); // customers waiting for a person
  const [openTickets, setOpenTickets] = useState(0); // issue tickets promised a response
  const [unsettled, setUnsettled] = useState(0); // refunds nothing settles any more: a person checks the payment system
  const [theme, setTheme] = useTheme();
  // Below 780px the side nav becomes an off-canvas drawer opened from the top bar.
  const compact = useMediaQuery("(max-width: 780px)");
  const [navOpen, setNavOpen] = useState(false);
  const drawerOpen = compact && navOpen;
  const [collapsed, setCollapsed] = useState(() => {
    try {
      return localStorage.getItem(NAV_KEY) === "1";
    } catch {
      return false;
    }
  });
  const toggleRef = useRef<HTMLButtonElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const restoreFocus = useRef(false);

  useEffect(() => {
    if (!staff) return;
    const load = () => {
      api
        .get<Approval[]>("/api/approvals?status_filter=pending")
        .then((rows) => setPending(rows.length))
        .catch(() => undefined);
      api
        .get<Handoff[]>("/api/handoffs")
        .then((rows) => setWaiting(rows.filter((h) => h.position).length))
        .catch(() => undefined);
      api
        .get<{ status: string }[]>("/api/tickets")
        .then((rows) => setOpenTickets(rows.filter((t) => t.status === "open").length))
        .catch(() => undefined);
      api
        .get<{ unsettled: number; needs_person: number }>("/api/refunds/summary")
        .then((counts) => setUnsettled(counts.needs_person))
        .catch(() => undefined);
    };
    load();
    const timer = window.setInterval(load, 8000);
    return () => window.clearInterval(timer);
  }, [staff, location.pathname]);

  // Dismissing the drawer returns focus to the menu button. This runs after the render
  // that removes `inert` from the main column; focusing earlier would be ignored.
  const closeNav = () => {
    restoreFocus.current = true;
    setNavOpen(false);
  };

  useEffect(() => {
    if (!drawerOpen) {
      if (restoreFocus.current) toggleRef.current?.focus();
      restoreFocus.current = false;
      return;
    }
    closeRef.current?.focus();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && closeNav();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [drawerOpen]);

  function toggleCollapsed() {
    setCollapsed((v) => {
      try {
        localStorage.setItem(NAV_KEY, v ? "0" : "1");
      } catch {
        /* storage unavailable: the choice lasts for this page only */
      }
      return !v;
    });
  }

  const groups: [string, NavItem[]][] = [
    ["服务", [
      { to: "/dashboard", label: "运营看板", icon: <LayoutDashboard size={16} /> },
      { to: "/chat", label: "对话测试台", icon: <MessagesSquare size={16} /> },
      { to: "/handoffs", label: "人工接待", icon: <Headset size={16} />, count: waiting },
      { to: "/approvals", label: "审批队列", icon: <ClipboardCheck size={16} />, count: pending },
      { to: "/tickets", label: "工单", icon: <Ticket size={16} />, count: openTickets },
      { to: "/refunds", label: "退款对账", icon: <ReceiptText size={16} />, count: unsettled },
    ]],
    ["知识与质量", [
      { to: "/knowledge", label: "知识库", icon: <BookOpen size={16} /> },
      { to: "/evals", label: "评测中心", icon: <FlaskConical size={16} /> },
      { to: "/flywheel", label: "数据飞轮", icon: <RefreshCcw size={16} /> },
    ]],
    ["运维", [
      { to: "/traces", label: "链路追踪", icon: <Activity size={16} /> },
      ...(user?.role === "admin" ? [{ to: "/audit", label: "操作审计", icon: <ScrollText size={16} /> }] : []),
    ]],
  ];

  const nextTheme = theme === "system" ? "light" : theme === "light" ? "dark" : "system";
  const themeLabel = { system: "跟随系统", light: "浅色", dark: "深色" }[theme];
  const railed = collapsed && !compact;
  const industry = INDUSTRY_TEXT[user?.tenant.industry ?? ""] ?? "企业";

  if (!user) return null;
  if (!staff) {
    // Customers get a consumer chat, not the console: a brand bar and the conversation.
    return (
      <div className="shell shell-customer">
        <header className="topbar topbar-customer">
          <div className="customer-brand">
            <span className="workspace-mark" aria-hidden="true">{user.tenant.name.slice(0, 1)}</span>
            <div style={{ minWidth: 0 }}>
              <div className="ellipsis" style={{ fontWeight: 600 }}>{user.tenant.name} 在线客服</div>
              <div className="faint ellipsis">{industry} · 演示租户</div>
            </div>
          </div>
          <span className="spacer" />
          <span className="badge env-tag">{isDemo ? "静态演示" : "演示环境"}<span className="hide-compact"> · 数据均为虚构</span></span>
          <button className="icon-btn" title={`主题：${themeLabel}（点击切换）`} aria-label={`切换主题，当前：${themeLabel}`} onClick={() => setTheme(nextTheme)}>
            {theme === "light" ? <Sun size={16} /> : theme === "dark" ? <Moon size={16} /> : <Monitor size={16} />}
          </button>
          <UserMenu user={user} />
        </header>
        <div className="main">
          {isDemo && <DemoNotice />}
          <Outlet />
        </div>
      </div>
    );
  }

  return (
    <div className={`shell ${drawerOpen ? "nav-open" : ""} ${railed ? "nav-collapsed" : ""}`}>
      <header className="topbar" inert={drawerOpen || undefined}>
        <button
          ref={toggleRef}
          className="icon-btn nav-toggle"
          aria-label={pending + waiting ? `打开导航菜单（${pending} 条待审批，${waiting} 位顾客等待人工）` : "打开导航菜单"}
          aria-expanded={drawerOpen}
          aria-controls="app-nav"
          onClick={() => setNavOpen(true)}
        >
          <Menu size={18} />
          {pending + waiting > 0 && <span className="toggle-dot" />}
        </button>
        <div className="brand">
          <Logo size={24} />
          <span className="brand-name">LoopDesk</span>
        </div>
        <span className="topbar-sep" aria-hidden="true" />
        <TenantSwitcher user={user} />
        <span className="spacer" />
        <span className="badge env-tag">{isDemo ? "静态演示" : "演示环境"}<span className="hide-compact"> · 数据均为虚构</span></span>
        <a className="icon-btn hide-compact" href={REPO_URL} target="_blank" rel="noreferrer" title="项目源码" aria-label="项目源码"><Github size={16} /></a>
        <button className="icon-btn" title={`主题：${themeLabel}（点击切换）`} aria-label={`切换主题，当前：${themeLabel}`} onClick={() => setTheme(nextTheme)}>
          {theme === "light" ? <Sun size={16} /> : theme === "dark" ? <Moon size={16} /> : <Monitor size={16} />}
        </button>
        <UserMenu user={user} />
      </header>
      <div className="shell-body">
        {drawerOpen && <div className="scrim" onClick={closeNav} />}
        <aside
          id="app-nav"
          className="sidebar"
          aria-label="导航菜单"
          {...(drawerOpen ? { role: "dialog", "aria-modal": true } : {})}
        >
          <div className="sidebar-head">
            <Logo size={22} />
            <span className="brand-name">LoopDesk</span>
            <button ref={closeRef} className="icon-btn nav-close" aria-label="关闭导航菜单" onClick={closeNav}>
              <X size={18} />
            </button>
          </div>
          <nav className="nav" aria-label="主导航">
            {groups.map(([group, items]) => (
              <div key={group} className="nav-section">
                <div className="nav-group">{group}</div>
                {items.map((item) => (
                  <NavLink
                    key={item.to}
                    to={item.to}
                    className={({ isActive }) => (isActive ? "active" : "")}
                    title={railed ? item.label : undefined}
                    onClick={() => setNavOpen(false)}
                  >
                    {item.icon}
                    <span className="nav-label">{item.label}</span>
                    {item.count ? <span className="count">{item.count}<span className="sr-only"> 条待处理</span></span> : null}
                  </NavLink>
                ))}
              </div>
            ))}
          </nav>
          <div className="sidebar-foot">
            <button className="nav-collapse" onClick={toggleCollapsed} aria-label={railed ? "展开导航" : "收起导航"} title={railed ? "展开导航" : "收起导航"}>
              {railed ? <ChevronsRight size={16} /> : <ChevronsLeft size={16} />}
              <span className="nav-label">收起导航</span>
            </button>
          </div>
        </aside>
        <div className="main" inert={drawerOpen || undefined}>
          {isDemo && <DemoNotice />}
          <Outlet />
        </div>
      </div>
    </div>
  );
}
