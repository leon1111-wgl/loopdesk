# LoopDesk console design spec

The console follows the design language of Chinese e-commerce back offices: merchant consoles, their
customer-service workbenches and ad-platform consoles built on Semi Design and Arco Design (both MIT).
Since 2026-11-01 it has an intro page with a floating bar, a dark workspace, section tabs with their
own accent colours and a phone tab bar. Since 2026-11-02 it has its own tone, distinct from the
owner's other projects (see the identity below): service-desk magenta and evidence lime on aubergine
charcoal, with treatments taken from a support desk rather than shared components.
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

## Intro page

`/` for every visitor unless 下次直接进入 was chosen (`src/pages/IntroPage.tsx`,
`src/styles/intro.css`): a floating solid bar (brand, centred **queue tabs** where the section in view
carries the on-duty status dot and an underline, 中文 / EN, the primary 进入控制台) on a quiet dotted
"desk mat" (no glows), a headline whose second line is set like an **evidenced phrase in an answer**:
in the brand colour, on a flat highlighter band (`--highlight`), with a citation chip `1`. Then the
two calls to action, the 下次直接进入 checkbox and trust chips; a signed-in visitor also sees which
demo account is signed in, with 进入控制台 / 切换账号 / 退出登录. Then an example run (one refund, six steps, marked
示例 · 虚构数据), four structural numbers (tenants, roles, tools, e2e walkthroughs; no model metrics),
运作方式 as a five-step stepper with clickable steps and a play / pause button (3.8 s per step, off
under reduced motion), the three console areas, the demo accounts themselves (进入控制台 scrolls
there), and a short FAQ. Only the intro has an English version; the console is Chinese.

## Layout grid and navigation

- **Shell**: 60 px top bar, then a 46 px row of page tabs for the current section; content on the
  page background, 24 px padding (16 px on phones), cards on a 16 px gutter grid. Widths above
  1600 px centre the content.
- **Top bar**: brand, the **role-aware section tabs** (服务运营 / 知识与质量 / 运维审计; agents do
  not see 操作审计), then the **tenant switcher** (current workspace and its industry; in demo mode
  it signs in with the same role in another demo tenant), environment tag (演示环境 · 数据均为虚构),
  source link, theme toggle (dark, light, system; dark is the default) and the **user menu**.
- **Section accents**: service magenta `--acc-service`, knowledge lime `--acc-knowledge`, ops sand
  `--acc-ops`. The shell sets `--accent` from the section; the active tab underline, the active page
  tab, the breadcrumb's last item and the first KPI rule use it. Buttons keep the brand magenta.
- **Page tabs**: pill tabs with icons; queue counts as red pills. A section tab shows the total of its
  pages' counts when another section is open.
- **Phones (≤ 780 px)**: the section tabs become a fixed bottom tab bar; the page tabs scroll
  sideways. The top bar has no `backdrop-filter`, which would make it the containing block of that
  fixed bar.
- **Page header**: breadcrumb (租户 / 分区), 24 px title, one-line description, actions on the right.
- **Customers** get no console chrome: a brand bar and the chat, centred.

## Tokens

All colours are CSS custom properties in `src/styles/tokens.css`; components never use raw values.
Text and fills meet WCAG AA (4.5:1) against the surfaces they sit on, in both themes; `pnpm contrast`
(`scripts/contrast.mjs`) checks every pair.

| token | dark (default) | light | use |
|---|---|---|---|
| `--bg` | `#131016` aubergine charcoal | `#F6F4F7` lilac-grey | page |
| `--surface` / `--surface-2` / `--pressed` | `#1A161E` / `#211C26` / `#2E2634` | `#FFFFFF` / `#FAF8FB` / `#EBE6EE` | panels, fills |
| `--border` / `--line-strong` | `#2A2330` / `#3C3343` | `#E7E1EA` / `#D6CFDB` | hairlines / controls |
| `--text` / `--text-2` / `--text-3` | `#F2EDF4` / `#C9C0CE` / `#9F95A6` | `#1F1923` / `#4D4552` / `#6B6271` | 3 text levels |
| `--primary` / `--primary-solid` | `#F27AC8` / `#B8237F` | `#A8287A` / `#A8287A` | service-desk magenta: links / fills behind white text |
| `--acc-service` / `--acc-knowledge` / `--acc-ops` | `#F27AC8` / `#A8D65A` / `#E8C27A` | `#A8287A` / `#4F7A12` / `#8A5A12` | section accents: magenta, evidence lime, sand |
| `--success` / `--warning` / `--danger` | `#6FD08C` / `#E8C27A` / `#FF7A8F` | `#23763F` / `#8A5A12` / `#C4302B` | states |
| `--info` | `#9FB0D8` | `#4B5A85` | a person (handoff, agent): cool steel, neutral |
| `--chart-1` / `--chart-2` | `#D65AA8` / `#6F9E22` | `#A8287A` / `#4F7A12` | chart series (CVD-validated pair; series 2 also hatched); distribution bars are neutral except the top row |
| `--highlight` | magenta at 26% | magenta at 16% | the highlighter band behind the intro's evidenced phrase |

Each semantic colour has `-soft` (tag and banner fill) and `-solid` (white-text fill) variants.
Shadows only on floating layers (menus, dialogs, drawers); everything else is 1 px hairlines.

## Type, spacing, density

- System font stack (PingFang SC / Microsoft YaHei / -apple-system), no web fonts; tabular numbers.
- Scale: 12 (meta, table head) · 13 (dense cells) · 14 (body, default) · 15 (card title) ·
  24 (page title) · 30 (KPI value, DIN-style numerals) · 38–66 (intro headline). Weights 400–800.
- 4 px spacing grid: 4, 8, 12, 16, 20, 24, 32. Radii 6 (tags), 8 (inputs), 12 (cards), 14 (dialogs);
  buttons, segmented controls and page tabs are pills.
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
  the opener. Open dialogs form a stack: the one opened last handles the keyboard, whatever its place in
  the DOM. A drawer with a decision in flight does not close. A money action always goes through a confirmation dialog that repeats the order and the
  amount.

## Agent workbench (three panes)

Used by 人工接待 (handoff queue) and, with the run inspector on the right, the staff 对话测试台.

| conversation list (300 px) | chat (fluid) | context panel (340 px) |
|---|---|---|
| status filter (全部 / 排队中 / 接待中) with counts; search; each row: avatar, customer, channel tag, SLA timer (green < 5 min, amber < 15 min, red after), summary, last message | header (customer, status tag, 结束人工服务); AI handoff brief; transcript with the bot's citation chips and verdict tag; quick replies; the bot's last answer offered as an editable draft (参考回答); composer | customer facts (channel, waiting, priority, owner); orders named in the conversation; this conversation's approvals, refunds and tickets with status tags and actions (去审批, 查看工单, 去对账) |

Below 1180 px the context panel is hidden; below 780 px the panes stack (the queue scrolls in a 260 px box above the conversation). The transcript scrolls itself to the newest message; the page does not jump.

## States

- **Loading**: skeleton rows in tables and lists, a quiet "加载中…" line elsewhere; never a spinner
  over content that is already shown.
- **Empty**: icon, one sentence on why it is empty, and how to produce data in the demo
  (e.g. 用顾客账号输入"转人工").
- **Error**: danger banner above the content with the server's message; the last data stays visible.
