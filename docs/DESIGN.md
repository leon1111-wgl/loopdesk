# LoopDesk console design spec

The console follows the design language of Chinese e-commerce back offices: merchant consoles, their
customer-service workbenches and ad-platform consoles built on Semi Design and Arco Design (both MIT).
We borrow layout patterns and the token system only. No third-party logos, product names, trademarks
or images appear in the UI; the product is LoopDesk and every tenant, person and order is synthetic.

References studied (October 2026): the Semi Design site and its token reference (values taken from
`@douyinfe/semi-ui-19` 2.104.0's own stylesheet), the Arco Design site, and Zendesk Agent Workspace /
Intercom Inbox for the three-pane agent inbox.

## Implementation decision

**Our own CSS and components, carrying Semi's tokens.** Measured on a trial branch (Oct 29 2026):

| | current console | + Semi (React 19 build) |
|---|---|---|
| install / typecheck / build | — | clean (`@douyinfe/semi-ui-19` + `react19-adapter`) |
| e2e (14 tests, Semi Nav/Table/Tooltip mounted in the shell) | 14/14 | 14/14, no React 19 warnings or console errors |
| JS (gzip) | 129 KB | 276 KB |
| CSS (gzip) | 8 KB | 87 KB (per-component CSS needs a SCSS build plugin; no first-party Vite plugin) |

Semi works with React 19, so this is a size and risk call, not a compatibility one. The library would
2.6x the first load of the static demo (GitHub Pages, read on phones), and adopting its components means
rewriting every page's DOM: the focus handling, `inert` drawers, keyboard rows and AA-tuned colours the
console already has would have to be redone on top of Semi's, and Semi's defaults miss AA in places
(`text-3` is 35% grey; dark-mode solid buttons put white text on `#54A9FF`, 2.5:1). We keep React 19,
take Semi's palette, radii, control heights and table/form anatomy, and implement them in
`src/styles/*.css` plus a few primitives in `src/components/ui.tsx` (Modal, Drawer, ConfirmDialog with a
focus trap, Tabs, Tag, PageHeader, Dropdown). No new runtime dependency.

## Layout grid and navigation

- **Shell**: 56 px top bar across the full width; collapsible left side nav (216 px, 64 px icon rail
  when collapsed, remembered per browser); content area on the grey page background, 24 px padding
  (16 px on phones), cards on a 16 px gutter grid. Widths above 1600 px centre the content.
- **Top bar**: logo and product name, then the **tenant switcher** (current workspace, its industry
  and the SYNTHETIC tag; in demo mode it lists the other demo tenants and signs in with the same role
  there). Right side: environment tag (演示环境 · 数据均为虚构), source link, theme toggle
  (light / dark / system) and the **user menu** (name, role, e-mail, 退出登录).
- **Side nav**: grouped (服务 / 知识与质量 / 运维), 40 px items, 16 px icons, the active item on a
  primary-tint fill with primary text. Queue counts sit right-aligned as pills (red for work waiting on
  a person). Below 780 px the nav is an off-canvas drawer opened from the top bar.
- **Page header**: breadcrumb (租户 / 分组), 20 px title, one-line description, actions on the right.
- **Customers** get no console chrome: a brand bar and the chat, centred.

## Tokens

All colours are CSS custom properties in `src/styles/tokens.css`; components never use raw values.
Text and fills meet WCAG AA (4.5:1) against the surfaces they sit on, in both themes; `pnpm contrast`
(`scripts/contrast.mjs`) checks every pair.

| token | light | dark | use |
|---|---|---|---|
| `--primary` | `#0064FA` (Semi blue-5) | `#54A9FF` | links, active nav, focus |
| `--primary-solid` | `#0064FA` | `#0064FA` | fills behind white text |
| `--primary-soft` / `--primary-line` | `#EAF5FF` / `#CBE7FE` | `#14273F` / `#1F4470` | selected rows, tags |
| `--bg` | `#F5F6F7` | `#16161A` | page |
| `--surface` / `--surface-2` | `#FFFFFF` / `#F7F8FA` | `#232429` / `#2A2B30` | cards / table head, fills |
| `--border` / `--border-2` | `#E4E6E9` / `#EEF0F2` | `#3A3C42` / `#303237` | hairlines |
| `--text` / `--text-2` / `--text-3` | `#1C1F23` / `#494C4F` / `#6B7075` | `#F9F9F9` / `#C6CACD` / `#9FA3A8` | 3 text levels |
| `--success` | `#237A2E` | `#5DC264` | done, approved |
| `--warning` | `#A65200` | `#FFAE43` | waiting, SLA soon |
| `--danger` | `#D52515` | `#FC725A` | failed, overdue, money out |
| `--info` | `#6A3AC7` | `#A68BF0` | a person (handoff, agent) |

Each semantic colour has `-soft` (tag and banner fill) and `-solid` (white-text fill) variants.
Shadows only on floating layers (menus, dialogs, drawers); everything else is 1 px hairlines.

## Type, spacing, density

- System font stack (PingFang SC / Microsoft YaHei / -apple-system), no web fonts; tabular numbers.
- Scale: 12 (meta, table head) · 13 (dense cells) · 14 (body, default) · 16 (card title) ·
  20 (page title) · 28 (KPI value). Weights 400 / 500 / 600.
- 4 px spacing grid: 4, 8, 12, 16, 20, 24, 32. Radii 3 (tags), 6 (controls, cards), 8 (dialogs).
- Controls 32 px (small 24, large 40). Table rows 48 px with 12 × 16 px cells; head row 40 px on
  `--surface-2`, 12 px `--text-2` labels.

## Patterns

- **Table**: card with a toolbar (filters as a segmented control, count, actions), then the table;
  row hover fill; numbers right-aligned; row actions as text buttons in the last column; a wide table
  scrolls inside its card, never the page.
- **Card**: white, 1 px border, 6 px radius, 16 × 20 px head with a 16 px title, optional extra
  (legend, link) on the right.
- **Form**: label above the control, 8 px apart, 16 px between fields; required marks in the label;
  errors as a danger banner above the footer. Dialog footer right-aligned: secondary, then primary.
- **Tags**: 20 px, 3 px radius, soft fill + text colour from one semantic token.
- **Banner**: full-width soft fill, 1 px line of the same hue, leading icon, inline link at the end.
- **Dialogs and drawers**: focus moves in on open, Tab stays inside, Escape closes, focus returns to
  the opener. A money action always goes through a confirmation dialog that repeats the order and the
  amount.

## Agent workbench (three panes)

Used by 人工接待 (handoff queue) and, with the run inspector on the right, the staff 对话测试台.

| conversation list (300 px) | chat (fluid) | context panel (340 px) |
|---|---|---|
| status filter (全部 / 排队中 / 接待中) with counts; search; each row: avatar, customer, channel tag, SLA timer (green < 5 min, amber < 15 min, red after), summary, last message | header (customer, status tag, 结束人工服务); AI handoff brief; transcript with the bot's citation chips and verdict tag; quick replies; the bot's last answer offered as an editable draft (参考回答); composer | customer facts (channel, waiting, priority, owner); orders named in the conversation; this conversation's approvals, refunds and tickets with status tags and actions (去审批, 查看工单, 去对账) |

Below 1180 px the context panel folds under the chat; below 780 px the list becomes a drawer.

## States

- **Loading**: skeleton rows in tables and lists, a quiet "加载中…" line elsewhere; never a spinner
  over content that is already shown.
- **Empty**: icon, one sentence on why it is empty, and how to produce data in the demo
  (e.g. 用顾客账号输入"转人工").
- **Error**: danger banner above the content with the server's message; the last data stays visible.
