import { useEffect, useState, type ReactNode } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import {
  Activity,
  BookOpen,
  Check,
  ChevronDown,
  ClipboardCheck,
  FlaskConical,
  Github,
  Headset,
  Info,
  LayoutDashboard,
  LogOut,
  MessagesSquare,
  Monitor,
  Moon,
  ReceiptText,
  RefreshCcw,
  ScrollText,
  Target,
  Ticket,
  Wrench,
  Sun,
} from "lucide-react";
import { api } from "../api";
import { isStaff, useAuth } from "../auth";
import { REPO_URL, isDemo, recording } from "../demo/replay";
import type { Approval, Handoff, User } from "../types";
import { Dropdown, INDUSTRY_TEXT, Logo, ROLE_TEXT, showIntroAgain, useTheme } from "./ui";

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


function DemoNotice() {
  const rec = recording();
  const source = rec && !rec.model.offline ? (
    <>
      示例问题回放真实模型 <b>{rec.model.strong}</b> 的录制（{rec.date}
      {rec.commit && <>，提交 <a href={`${REPO_URL}/commit/${rec.commit}`} target="_blank" rel="noreferrer" style={{ marginLeft: 0 }}>{rec.commit}</a></>}）
    </>
  ) : (
    <>示例问题回放离线模型的录制，录制自真实后端</>
  );
  return (
    <div className="demo-notice" role="note" data-model={rec?.model.strong}>
      <Info size={14} aria-hidden="true" />
      <div><strong>演示工作区</strong><span className="demo-source">{source}；其他问题由浏览器内的规则引擎回答，不是模型，不代表真实模型的质量。数据均为虚构。</span>
        <details><summary>使用说明与运行方式</summary><p>可以切换角色浏览，回放示例问题，也可以直接问自己的问题。退款、工单等改动只作用于本页的模拟副本，标注“模拟”，刷新后还原；其他提交类操作不会生效。
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
      {(close) => (
        <>
          <div className="user-card">
            <span className="avatar me" aria-hidden="true">{user.name.slice(0, 1)}</span>
            <div style={{ minWidth: 0 }}>
              <div className="ellipsis" style={{ fontWeight: 600 }}>{user.name}</div>
              <div className="faint ellipsis">{ROLE_TEXT[user.role]} · {user.email}</div>
            </div>
          </div>
          <div className="menu-sep" />
          <Link className="menu-item" to="/" onClick={() => { showIntroAgain(); close(); }}><Info size={14} aria-hidden="true" /> 产品介绍</Link>
          <a className="menu-item" href={REPO_URL} target="_blank" rel="noreferrer"><Github size={14} aria-hidden="true" /> 项目源码</a>
          <button className="menu-item" onClick={logout}><LogOut size={14} aria-hidden="true" /> 退出登录</button>
        </>
      )}
    </Dropdown>
  );
}

interface Section {
  key: "service" | "knowledge" | "ops";
  label: string;
  short: string;
  icon: ReactNode;
  items: NavItem[];
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

  // Role-aware sections: each has its own accent colour and a row of page tabs under the top bar.
  const sections: Section[] = [
    { key: "service", label: "服务运营", short: "服务", icon: <Headset size={16} />, items: [
      { to: "/dashboard", label: "运营看板", icon: <LayoutDashboard size={15} /> },
      { to: "/chat", label: "对话测试台", icon: <MessagesSquare size={15} /> },
      { to: "/handoffs", label: "人工接待", icon: <Headset size={15} />, count: waiting },
      { to: "/approvals", label: "审批队列", icon: <ClipboardCheck size={15} />, count: pending },
      { to: "/tickets", label: "工单", icon: <Ticket size={15} />, count: openTickets },
      { to: "/refunds", label: "退款对账", icon: <ReceiptText size={15} />, count: unsettled },
    ] },
    { key: "knowledge", label: "知识与质量", short: "知识", icon: <BookOpen size={16} />, items: [
      { to: "/knowledge", label: "知识库", icon: <BookOpen size={15} /> },
      { to: "/evals", label: "评测中心", icon: <FlaskConical size={15} /> },
      { to: "/simulations", label: "模拟顾客", icon: <Target size={15} /> },
      { to: "/flywheel", label: "数据飞轮", icon: <RefreshCcw size={15} /> },
    ] },
    { key: "ops", label: "运维审计", short: "运维", icon: <Activity size={16} />, items: [
      { to: "/traces", label: "链路追踪", icon: <Activity size={15} /> },
      { to: "/tools", label: "工具集成", icon: <Wrench size={15} /> },
      ...(user?.role === "admin" ? [{ to: "/audit", label: "操作审计", icon: <ScrollText size={15} /> }] : []),
    ] },
  ];
  const path = "/" + (location.pathname.split("/")[1] || "chat");
  const current = sections.find((sec) => sec.items.some((i) => i.to === path)) ?? sections[0];

  const nextTheme = theme === "dark" ? "light" : theme === "light" ? "system" : "dark";
  const themeLabel = { system: "跟随系统", light: "浅色", dark: "深色" }[theme];
  const themeButton = (
    <button className="icon-btn" title={`主题：${themeLabel}（点击切换）`} aria-label={`切换主题，当前：${themeLabel}`} onClick={() => setTheme(nextTheme)}>
      {theme === "light" ? <Sun size={16} /> : theme === "dark" ? <Moon size={16} /> : <Monitor size={16} />}
    </button>
  );
  const industry = INDUSTRY_TEXT[user?.tenant.industry ?? ""] ?? "企业";

  if (!user) return null;
  if (!staff) {
    // Customers get a consumer chat, not the console: a brand bar and the conversation.
    return (
      <div className="shell shell-customer" data-section="service">
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
          {isDemo && (location.pathname.startsWith("/simulations")
            ? <NavLink to="/chat" className="btn btn-sm" aria-label="回到对话"><MessagesSquare size={14} aria-hidden="true" /><span className="hide-compact">回到对话</span></NavLink>
            : <NavLink to="/simulations" className="btn btn-sm" aria-label="试一个目标" title="拿一张隐藏目标的任务卡和客服对话，结束后按数据终态评分"><Target size={14} aria-hidden="true" /><span className="hide-compact">试一个目标</span></NavLink>)}
          {themeButton}
          <UserMenu user={user} />
        </header>
        <div className="main">
          {isDemo && <DemoNotice />}
          <Outlet />
        </div>
      </div>
    );
  }

  const sectionCount = (sec: Section) => sec.items.reduce((n, i) => n + (i.count ?? 0), 0);
  return (
    <div className="shell" data-section={current.key}>
      <header className="topbar">
        <NavLink to="/dashboard" className="brand" aria-label="LoopDesk 首页">
          <Logo size={28} />
          <span className="brand-name">LoopDesk</span>
          <small className="brand-sub hide-compact">客服工作台</small>
        </NavLink>
        <nav className="main-nav" aria-label="导航菜单">
          {sections.map((sec) => (
            <Link key={sec.key} to={sec.items[0].to} data-section={sec.key}
              className={`nav-item ${sec === current ? "active" : ""}`} aria-current={sec === current ? "true" : undefined}
              aria-label={sectionCount(sec) > 0 && sec !== current ? `${sec.label}（${sectionCount(sec)} 条待处理）` : sec.label}>
              {sec.icon}
              <span className="nav-long">{sec.label}</span>
              <span className="nav-short" aria-hidden="true">{sec.short}</span>
              {sectionCount(sec) > 0 && sec !== current && <span className="count">{sectionCount(sec)}<span className="sr-only"> 条待处理</span></span>}
            </Link>
          ))}
        </nav>
        <div className="top-actions">
          <TenantSwitcher user={user} />
          <span className="badge env-tag hide-compact">{isDemo ? "静态演示" : "演示环境"} · 数据均为虚构</span>
          <a className="icon-btn hide-compact" href={REPO_URL} target="_blank" rel="noreferrer" title="项目源码" aria-label="项目源码"><Github size={16} /></a>
          {themeButton}
          <UserMenu user={user} />
        </div>
      </header>
      <nav className="subnav" aria-label={`${current.label}页面`}>
        {current.items.map((item) => (
          <NavLink key={item.to} to={item.to} className={({ isActive }) => `sub-item ${isActive ? "active" : ""}`}>
            {item.icon}
            <span>{item.label}</span>
            {item.count ? <span className="count">{item.count}<span className="sr-only"> 条待处理</span></span> : null}
          </NavLink>
        ))}
      </nav>
      <div className="main">
        {isDemo && <DemoNotice />}
        <Outlet />
      </div>
    </div>
  );
}
