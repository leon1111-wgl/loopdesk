import { useEffect, useRef, useState, type ReactNode } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import {
  Activity,
  BookOpen,
  ClipboardCheck,
  FlaskConical,
  Headset,
  LayoutDashboard,
  LogOut,
  Menu,
  MessagesSquare,
  Monitor,
  Moon,
  RefreshCcw,
  ScrollText,
  Ticket,
  Sun,
  X,
} from "lucide-react";
import { api } from "../api";
import { isStaff, useAuth } from "../auth";
import type { Approval, Handoff } from "../types";
import { Logo, ROLE_TEXT, useMediaQuery, useTheme } from "./ui";

const TITLES: Record<string, string> = {
  dashboard: "运营看板",
  chat: "对话",
  approvals: "审批队列",
  handoffs: "人工接待",
  tickets: "工单",
  knowledge: "知识库",
  traces: "链路追踪",
  flywheel: "数据飞轮",
  evals: "评测中心",
  audit: "操作审计",
};

interface NavItem {
  to: string;
  label: string;
  icon: ReactNode;
  count?: number;
}

export default function Layout() {
  const { user, logout } = useAuth();
  const location = useLocation();
  const staff = isStaff(user);
  const [pending, setPending] = useState(0);
  const [waiting, setWaiting] = useState(0); // customers waiting for a person
  const [openTickets, setOpenTickets] = useState(0); // issue tickets promised a response
  const [theme, setTheme] = useTheme();
  // Below 780px the sidebar becomes an off-canvas drawer opened from the top bar.
  const compact = useMediaQuery("(max-width: 780px)");
  const [navOpen, setNavOpen] = useState(false);
  const drawerOpen = compact && navOpen;
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

  const section = location.pathname.split("/")[1] || "chat";
  const groups: [string, NavItem[]][] = staff
    ? [
        ["服务", [
          { to: "/dashboard", label: "运营看板", icon: <LayoutDashboard size={16} /> },
          { to: "/chat", label: "对话测试台", icon: <MessagesSquare size={16} /> },
          { to: "/approvals", label: "审批队列", icon: <ClipboardCheck size={16} />, count: pending },
          { to: "/handoffs", label: "人工接待", icon: <Headset size={16} />, count: waiting },
          { to: "/tickets", label: "工单", icon: <Ticket size={16} />, count: openTickets },
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
      ]
    : [["服务", [{ to: "/chat", label: "在线客服", icon: <MessagesSquare size={16} /> }]]];

  const nextTheme = theme === "system" ? "light" : theme === "light" ? "dark" : "system";
  const themeLabel = { system: "跟随系统", light: "浅色", dark: "深色" }[theme];

  return (
    <div className={`shell ${drawerOpen ? "nav-open" : ""}`}>
      {drawerOpen && <div className="scrim" onClick={closeNav} />}
      <aside
        id="app-nav"
        className="sidebar"
        aria-label="导航菜单"
        {...(drawerOpen ? { role: "dialog", "aria-modal": true } : {})}
      >
        <div className="brand">
          <Logo />
          <span className="brand-name">LoopDesk</span>
          <button ref={closeRef} className="icon-btn nav-close" aria-label="关闭导航菜单" onClick={closeNav}>
            <X size={18} />
          </button>
        </div>
        <div className="tenant-card">
          <div className="name">{user?.tenant.name}</div>
          <div className="meta">{user?.tenant.industry === "saas" ? "SaaS 服务" : "电商零售"} · 演示租户</div>
        </div>
        <nav className="nav" aria-label="主导航">
          {groups.map(([group, items]) => (
            <div key={group}>
              <div className="nav-group">{group}</div>
              {items.map((item) => (
                <NavLink
                  key={item.to}
                  to={item.to}
                  className={({ isActive }) => (isActive ? "active" : "")}
                  onClick={() => setNavOpen(false)}
                >
                  {item.icon}
                  {item.label}
                  {item.count ? <span className="count">{item.count}<span className="sr-only"> 条待处理</span></span> : null}
                </NavLink>
              ))}
            </div>
          ))}
        </nav>
        <div className="sidebar-foot">
          <span className="avatar me">{user?.name.slice(0, 1)}</span>
          <div style={{ minWidth: 0, lineHeight: 1.3 }}>
            <div className="ellipsis" style={{ fontSize: 13, fontWeight: 500 }}>{user?.name}</div>
            <div className="faint">{ROLE_TEXT[user?.role ?? ""]}</div>
          </div>
          <span className="spacer" />
          <button className="icon-btn" title="退出登录" aria-label="退出登录" onClick={logout}>
            <LogOut size={15} />
          </button>
        </div>
      </aside>
      <div className="main" inert={drawerOpen || undefined}>
        <header className="topbar">
          <div className="crumbs">
            <button
              ref={toggleRef}
              className="icon-btn nav-toggle"
              aria-label={staff && pending + waiting ? `打开导航菜单（${pending} 条待审批，${waiting} 位顾客等待人工）` : "打开导航菜单"}
              aria-expanded={drawerOpen}
              aria-controls="app-nav"
              onClick={() => setNavOpen(true)}
            >
              <Menu size={18} />
              {staff && pending + waiting > 0 && <span className="toggle-dot" />}
            </button>
            <span className="ellipsis crumb-tenant">{user?.tenant.name}</span>
            <span className="crumb-tenant">/</span>
            <b className="ellipsis">{TITLES[section] ?? "LoopDesk"}</b>
          </div>
          <div className="row">
            <span className="badge">演示环境<span className="hide-compact"> · 数据均为虚构</span></span>
            <button className="icon-btn" title={`主题：${themeLabel}（点击切换）`} aria-label={`切换主题，当前：${themeLabel}`} onClick={() => setTheme(nextTheme)}>
              {theme === "light" ? <Sun size={16} /> : theme === "dark" ? <Moon size={16} /> : <Monitor size={16} />}
            </button>
          </div>
        </header>
        <Outlet />
      </div>
    </div>
  );
}
