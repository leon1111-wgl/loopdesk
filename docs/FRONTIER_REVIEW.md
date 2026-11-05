# 前沿技术评审（2026-11）

对照客服 Agent 领域当前的公开做法，逐项判断 LoopDesk 要不要跟进。来源只用一手材料（规范更新记录、官方文档和发布说明、论文、头部开源项目），日期是材料自己的日期，查阅时间 2026-11-05。查不到日期或没有读到原文的，在条目里写明。

判断标准有两条：做了之后，看演示的人或审代码的人能不能看出差别；有没有一个测试或测量，不做就会失败。两条都不满足的，不管多热门都不做。

## 结论

| # | 候选 | 决定 | 理由 | 怎么验证 |
|---|---|---|---|---|
| 1 | 模型只做选择、程序陈述事实（Action-Selector）用在免费演示 | **采纳，已做** | 没有 API 费用的小模型不可能写出可信答案；这个模式正好把它限制在选择题里 | `frontend/src/demo/engine/engine.test.ts`「the model tier only chooses」；`tier2-eval.yml` |
| 2 | 按结果评分的完整对话（end-state） | **采纳，已做** | 现有门禁评一轮、不落库；错的终态才是错的退款 | `backend/evals/journeys.json`，后端 `tests/test_journeys.py` 和浏览器 `journeys.test.ts` 跑同一份 |
| 3 | MCP 2026-07-28：工具注解、结构化结果 | **部分采纳，已做** | 只读服务器几行就能对齐；OAuth 和有状态部分用不上 | `tests/test_providers_and_channels.py` |
| 4 | OWASP Agentic Top 10 / Agents Rule of Two 对照 | **采纳，文档** | 面向企业读者的通用语言；控制点都已有，对照表指向可运行的测试 | 下面的对照表 |
| 5 | OpenTelemetry GenAI 约定 | 已有，不改 | 已用 `invoke_agent`、`execute_tool`、`chat`；约定本身还是 Development，追新没有收益 | `tests/test_telemetry.py` |
| 6 | 自适应注入攻击下同时报告成功率和任务完成率 | 已有一部分，下一步 | `redteam/` 已有迭代越狱，尚未把任务完成率放在一起报告 | — |
| 7 | 顾客长期记忆 | 不做 | 看演示的人注意不到；OWASP ASI06 把记忆投毒单列为风险，隐私和删除义务都要先有设计 | — |
| 8 | 实时语音新模型与语义断句 | 不做 | 现有语音通道是同一文字 Agent 外面的薄传输层，这一轮没有可测的缺口 | — |
| 9 | 换一个更好的小模型 | 只测量 | Gemma 3 1B 有一篇外部证据、显存只有一半，但只有测量显示在这份工作上更好才换；浏览器内置 AI 不支持中文 | `tier2-eval.yml` 可换模型重跑 |
| 10 | “已解决”的定义和复开扣除 | 已有定义，留待后续 | 按会话统计、排除转人工、拒答和差评，已比“顾客没再说话”严格；复开扣除要改接口字段 | `app/api/metrics.py` |

## 1. 模型只做选择（已做）

**现状。** Design Patterns for Securing LLM Agents（[arXiv 2506.08837](https://arxiv.org/abs/2506.08837)，2025-06）列出六种模式，并有一节专讲客服机器人：Action-Selector 让模型从允许清单里选动作，注入的文字最多让它选错选项，不能让它做清单外的事。CaMeL（[arXiv 2503.18813](https://arxiv.org/abs/2503.18813)，2025-03，修订 2025-06）用架构隔离换来可证明的安全，代价是 AgentDojo 上任务完成率从 84% 降到 77%。The Attacker Moves Second（[arXiv 2510.09023](https://arxiv.org/abs/2510.09023)，2025-10）用自适应攻击把 12 种已发表的防御打到大多超过 90% 的成功率，说明靠检测器和提示词加固不够，架构限制才站得住。

约束解码方面，WebLLM 通过 XGrammar 支持 JSON Schema（[发布记录](https://github.com/mlc-ai/web-llm/releases)，[XGrammar](https://github.com/mlc-ai/xgrammar)）。它只保证语法和枚举合法，不保证选得对。

**做法。** 演示第 2 档里，Qwen3-0.6B 在 JSON Schema 下只能选意图、顾客自己的订单、程序列出的知识片段编号。程序逐项校验：订单必须是顾客自己的或这句话里写了的；片段必须是程序给出的候选；退款、撤回这类写操作一律回到规则，按顾客原话判断。模型说“要退款”而原话是“我可能会退款 看情况”，什么都不提交。事实由工具和原文陈述，模型一个字都不写进答案。

**局限。** 模型可以选错片段，顾客会读到一段不对题的原文。这比编造好，但不是答对。

## 2. 按结果评分的完整对话（已做）

**现状。** Anthropic 的评测指南（[Demystifying evals for AI agents](https://www.anthropic.com/engineering/demystifying-evals-for-ai-agents)，2026-01-09）主张优先用确定性评分、评结果多于评路径，并用 pass^k 衡量一致性。tau-bench 系列（[τ²-bench](https://arxiv.org/abs/2506.07982)，2025-06；[仓库](https://github.com/sierra-research/tau2-bench)）用模拟用户、模拟后端和终态检查。Sierra（[Agent Development Life Cycle](https://sierra.ai/blog/agent-development-life-cycle)，2024-06）和 Decagon（Simulations，2025-09）公开的做法也是把对话回放成发布前的回归门禁。四家厂商都没有公布经审计的准确率。

**LoopDesk 之前。** 门禁 159 条用例评的是一轮对话的工具和文字，沙箱里演练、不写库；pass^k 已经有（`app.evals.summarize`）。

**做法。** `backend/evals/journeys.json` 写了 15 段完整对话：提交、审批、驳回、管理员分级、两步撤回、先问后申请、沿用上一轮订单只提议、频率限制、一句话两件故障、别人的订单、注入、虚拟订单、转人工和退出。每段真实写库，人的决定也在其中，最后比较订单状态、退款台账、审批队列、工单和转人工状态。比较是严格的：“没有多发生什么”也算在内。

同一份文件同时跑后端（真实 API）和浏览器引擎，所以两边在结果上互相约束。这是这项工作最大的收获：浏览器版不再只是“看起来一样”。

**局限。** 目前用的是脚本化顾客，不是模型扮演的顾客，所以测不到顾客临场换说法。

## 3. MCP 2026-07-28（部分采纳，已做）

**现状。** 最新稳定版是 2026-07-28（[变更记录](https://modelcontextprotocol.io/specification/2026-07-28/changelog)）：协议改为无状态，服务端发起的请求改成多轮往返，采样和根目录废弃。工具页要求客户端把工具注解当作不可信，除非来自可信服务器（[工具](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)）。Python SDK 2.3 已默认这个版本。

**做法。** 只读服务器 `app/mcp_server.py` 的四个工具都标了 `readOnlyHint`，返回结构化结果和 `outputSchema`。租户接入的外部 MCP 工具早就不信对方的注解，风险等级和审批策略由管理员设定（`app/tooling/mcp_client.py`）。

**不做。** 多轮往返的 `requestState` 可以做一次性审批令牌，但这个服务器有意不暴露写工具（退款只走 Agent 和审批队列），用不上。OAuth 2.1 只对 HTTP 传输有意义，这里是本机 stdio。MCP Registry 还是预览版。

## 4. OWASP 对照（文档）

[OWASP Top 10 for Agentic Applications](https://genai.owasp.org/2025/12/09/owasp-top-10-for-agentic-applications-the-benchmark-for-agentic-security-in-the-age-of-autonomous-ai/)（2025-12-09）。下表只列 LoopDesk 有具体控制点的项，并指向能运行的测试。

| 风险 | LoopDesk 的做法 | 测试 |
|---|---|---|
| ASI01 目标劫持 | 注入不被拦截但被记录；权限在工具层强制，文字改变不了额度 | `test_guardrails_and_verifier.py`、`test_traditional_injection.py`、`journeys.json` 的 `injection_changes_nothing` |
| ASI02 工具滥用 | 动钱只认明确请求；工具再查一遍顾客原话；金额超订单实付直接拒绝 | `test_refund_intent.py`、`test_refund_stated_amount.py`、`test_tool_schema_egress.py` |
| ASI03 身份与权限 | 身份取自登录用户，不取自模型；金额分级审批；四眼原则 | `test_refund_policy.py`、`test_staff_refund.py`、`test_approval_resume.py` |
| ASI04 供应链 | 租户工具有出站白名单、定义哈希，定义变了要重新确认 | `test_mcp_client.py`、`test_tool_schema_egress.py` |
| ASI06 记忆与上下文投毒 | 入库扫描，可疑片段人工放行前不进检索；没有长期记忆 | `test_knowledge_scan.py`、`test_poisoned_set.py` |
| ASI08 级联故障 | 模型和向量服务故障有熔断和降级，退款台账先写后付 | `test_model_outage.py`、`test_embedding_outage.py`、`test_refund_race.py` |
| ASI09 人机信任 | 审批卡展示服务端记录的证据包和原因，不展示模型的说法；状态句由工具字段渲染 | `test_status_templates.py`、`test_rejected_reapplication.py` |

Meta 的 [Agents Rule of Two](https://ai.meta.com/blog/practical-ai-agent-security/)（2025-10-31）是设计经验，不是标准：一次会话里“处理不可信输入”“接触敏感数据”“改变状态”最多占两项，三项都要时加人工。客服必须三项都占，所以 LoopDesk 的动钱路径都有人工或额度闸门。OWASP 的 Agent Control Standard v0.1.0 还不成熟（仓库自己写明通信没有认证、默认放行），不跟进。

## 5. OpenTelemetry GenAI 约定（已有）

所有 GenAI 属性和 span 仍是 Development 状态，v1.42.0 起已搬到[单独的仓库](https://github.com/open-telemetry/semantic-conventions-genai)（主仓库[发布说明](https://github.com/open-telemetry/semantic-conventions/releases/tag/v1.42.0)）。LoopDesk 已导出 `invoke_agent`、`execute_tool`、`chat` 和 token 用量，默认不外发提问和回答。Langfuse 和 Phoenix 都能接收。约定稳定之前不追字段细节。

## 6. 注入攻击的报告方式（下一步）

一份对 33,000 条轨迹的审计（[arXiv 2610.23254](https://arxiv.org/abs/2610.23254)，2026-10，范围是小模型）发现攻击失败多数是模型没理会，而不是防住了；只改系统提示词，成功率就能从 0.2% 变到 43%；还有 21.7% 的情况 Agent 安全但没完成任务。所以成功率要和任务完成率一起报。`docs/EVALUATION.md` 目前只报前者，这是下一批可做的事。

## 7. 顾客长期记忆（不做）

LangMem、Mem0、Letta 和 Anthropic 的记忆工具都已提供按用户隔离的记忆。Mem0 的文档明确说过期只是隐藏，不是删除。OWASP ASI06 把记忆投毒列为独立风险。这一项看演示的人注意不到，又要先解决可见、可删和来源追溯，所以不做。

## 8. 实时语音（不做）

OpenAI Realtime 和 Pipecat、LiveKit 都在快速迭代（语义断句、Smart Turn v3.2）。[τ-Voice](https://arxiv.org/abs/2603.13686)（2026-03）显示语音 Agent 只保留文字能力的 30% 到 45%。LoopDesk 的语音通道已经是同一文字 Agent 外面的薄传输层，FunASR 也在活跃维护；这一轮没有测出来的缺口，不动。

## 9. 换模型（只测量）

- **Gemma 3 1B**：[一篇物流客服意图分类论文](https://arxiv.org/abs/2603.23172)（2026-03）里中文准确率比 Qwen3-0.6B 高约 2 个百分点（94.81 对 92.58），一个数据集，没有显著性检验。WebLLM 配置里它的显存需求是 711 MB，Qwen3-0.6B 是 1403 MB（[src/config.ts](https://github.com/mlc-ai/web-llm)）。许可不是 Apache-2.0，还要另外托管权重。
- **Qwen3.5-0.8B**：显存 1629 MB，比现在还大。
- **Chrome 内置 Prompt API**：不支持中文，且要求 22 GB 空闲磁盘（[文档](https://developer.chrome.com/docs/ai/built-in-apis)）。
- **WebNN**：还在候选推荐阶段（[W3C](https://www.w3.org/TR/webnn/)，2026-10-21）。

结论：先用这份工作自己的用例量（`tier2-eval.yml`，模型可换），只有测量显示明显更好才换。结果见 `docs/EVALUATION.md`。

## 10. “已解决”的定义（已有）

Intercom Fin 把“顾客确认”和“顾客没再要求”都算解决，顾客回到同一对话就扣回；Zendesk 宣称由独立模型核实（2026-05-19）；都没有公布经审计的比率。LoopDesk 的自助解决率按会话统计，排除转人工、拒答、差评和故障（`app/api/metrics.py`）。没有跨会话的复开扣除，这要新增接口字段和一次演示重录，留待后续。

## 没能核实的

查阅时读不到的有：OWASP 两份 PDF 全文（用的是官方页面和第三方摘要）、MCP schema 的 ToolAnnotations 小节、Gemma 许可全文、Qwen3-0.6B 的 WebLLM 下载体积（站内实测权重 335 MB）。部分开源项目发布页只显示月日不显示年份。
