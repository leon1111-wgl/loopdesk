import { useEffect, useState, type ReactNode } from "react";
import { NavLink, Outlet, useLocation } from "react-router-dom";
import {
  Activity,
  BookOpen,
  ClipboardCheck,
  FlaskConical,
  LayoutDashboard,
  LogOut,
  MessagesSquare,
  Monitor,
  Moon,
  RefreshCcw,
  Sun,
} from "lucide-react";
import { api } from "../api";
import { isStaff, useAuth } from "../auth";
import type { Approval } from "../types";
import { Logo, ROLE_TEXT, useTheme } from "./ui";

const TITLES: Record<string, string> = {
  dashboard: "运营看板",
  chat: "对话",
  approvals: "审批队列",
  knowledge: "知识库",
  traces: "链路追踪",
  flywheel: "数据飞轮",
  evals: "评测中心",
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
  const [theme, setTheme] = useTheme();

  useEffect(() => {
    if (!staff) return;
    const load = () =>
      api
        .get<Approval[]>("/api/approvals?status_filter=pending")
        .then((rows) => setPending(rows.length))
        .catch(() => undefined);
    load();
    const timer = window.setInterval(load, 8000);
    return () => window.clearInterval(timer);
  }, [staff, location.pathname]);

  const section = location.pathname.split("/")[1] || "chat";
  const groups: [string, NavItem[]][] = staff
    ? [
        ["服务", [
          { to: "/dashboard", label: "运营看板", icon: <LayoutDashboard size={16} /> },
          { to: "/chat", label: "对话测试台", icon: <MessagesSquare size={16} /> },
          { to: "/approvals", label: "审批队列", icon: <ClipboardCheck size={16} />, count: pending },
        ]],
        ["知识与质量", [
          { to: "/knowledge", label: "知识库", icon: <BookOpen size={16} /> },
          { to: "/evals", label: "评测中心", icon: <FlaskConical size={16} /> },
          { to: "/flywheel", label: "数据飞轮", icon: <RefreshCcw size={16} /> },
        ]],
        ["运维", [{ to: "/traces", label: "链路追踪", icon: <Activity size={16} /> }]],
      ]
    : [["服务", [{ to: "/chat", label: "在线客服", icon: <MessagesSquare size={16} /> }]]];

  const nextTheme = theme === "system" ? "light" : theme === "light" ? "dark" : "system";
  const themeLabel = { system: "跟随系统", light: "浅色", dark: "深色" }[theme];

  return (
    <div className="shell">
      <aside className="sidebar">
        <div className="brand">
          <Logo />
          <span className="brand-name">LoopDesk</span>
        </div>
        <div className="tenant-card">
          <div className="name">{user?.tenant.name}</div>
          <div className="meta">{user?.tenant.industry === "saas" ? "SaaS 服务" : "电商零售"} · 演示租户</div>
        </div>
        <nav className="nav">
          {groups.map(([group, items]) => (
            <div key={group}>
              <div className="nav-group">{group}</div>
              {items.map((item) => (
                <NavLink key={item.to} to={item.to} className={({ isActive }) => (isActive ? "active" : "")}>
                  {item.icon}
                  {item.label}
                  {item.count ? <span className="count">{item.count}</span> : null}
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
      <div className="main">
        <header className="topbar">
          <div className="crumbs">
            <span className="ellipsis">{user?.tenant.name}</span>
            <span>/</span>
            <b>{TITLES[section] ?? "LoopDesk"}</b>
          </div>
          <div className="row">
            <span className="badge">演示环境 · 数据均为虚构</span>
            <button className="icon-btn" title={`主题：${themeLabel}（点击切换）`} aria-label="切换主题" onClick={() => setTheme(nextTheme)}>
              {theme === "light" ? <Sun size={16} /> : theme === "dark" ? <Moon size={16} /> : <Monitor size={16} />}
            </button>
          </div>
        </header>
        <Outlet />
      </div>
    </div>
  );
}
