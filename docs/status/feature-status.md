# 功能成熟度矩阵

> 状态定义见 [Wiki 首页](../README.md)。本表区分“已经有代码”“已经验证有效”和“未来目标”。

## Proven

| 功能 | 状态 | 用户价值 | 证据/入口 | 限制 |
|---|---|---|---|---|
| 卡片导入与存档 | Proven | 导入 PNG/JSON/TXT 素材，生成卡片目录、开场、世界书、变量和记忆文件 | `src/airp/import_card.py`、`src/airp/import_prepare.py`、`skills/tests/test_import_compatibility.py`、`/rp` | 已覆盖代表性 SillyTavern JSON v2 卡；广泛兼容矩阵与诊断报告仍缺 |
| MVU 变量与记忆 | Proven | 回合中执行变量更新，保存 chat log、变量差分和剧情摘要 | `src/airp/engine/mvu.py`、`src/airp/engine/card.py`、`skills/write_memory.py` | 未知路径和 server 不可用时校验较宽松 |
| 前端回合体验 | Proven | 浏览器输入、SSE 临时流式 AI 回合、当前 Active Graph 选择、Agent Trace、AI-only 开场、连续多轮、取消、开场/存档切换、重roll、回退、刷新/重启恢复、token 展示和响应式内容渲染。Provider 和 Graph 定义只由 Studio 管理。 | `src/airp/cli.py`、`src/airp/server.py`、`src/airp/web/index.html`、`skills/tests/test_agent_studio_golden_path.py` | 真实 DeepSeek 单回合已验证（2026-07-28）；浏览器长会话、断线/代理行为和跨 provider 兼容性仍待验证 |
| 世界书 skill 模式 | Proven | Project 绑定的世界书在 Execution Plan 中冻结 catalog，Agent 通过 Host 注册的只读 exact-title capability 按需加载正文 | `src/airp/engine/worldbook_library.py`、`src/airp/host/rp/tools.py`、`skills/tests/test_graph_execution.py`、ADR-0002/0019 | 不扫描 `skills/` 目录；未注册的自定义 capability 不会自动暴露 |
| engine 深模块 | Proven | token、MVU、卡片存储、渲染、世界书和 context 已拆为独立模块 | `src/airp/engine/`、ADR-0003 | 数据目录仍未重组 |
| AIRP Graph Runtime Spine | Proven | 命令、持久 generation lease、任务尝试、session-scoped revision lineage、唯一提交、恢复和兼容投影由 AIRP 自有 Graph/Session runtime 承载 | `src/airp/engine/runtime.py`、`src/airp/engine/graph_runtime.py`、Session Turn Runtime Contract、ADR-0019 | 真实 provider 浏览器长会话和多 provider 兼容矩阵仍待继续验证 |
| Card-local Multi-Session | Experimental | 同一卡片可新建、切换、重命名和删除存档；各自 opening/task/event/revision/state 独立，活动存档与标题可跨重启恢复 | `src/airp/host/rp/session_manager.py`、`skills/tests/test_studio_runtime_bootstrap.py`、ADR-0012 | 一个 server 进程只服务一张卡且一次仅激活一个存档；卡片级素材和长期 memory 仍是共享输入 |
| Context Manifest (internal compatibility) | Experimental | 编译器在执行前冻结 revision-scoped snapshot，持久化可重放 payload、section source/hash、预算决策、worldbook provenance。内部 `ContextLayout` 只表达通用上下文顺序，不是 Agent 或 Studio 的用户配置。 | `src/airp/engine/context_compiler.py`、`skills/tests/test_graph_execution.py`、ADR-0009 | 旧 RuntimeConfig/Prompt Preset 不再存在于现行路径；旧静态文件只在一次性 Studio 导入时读取。 |
| Agent Studio instruction macros | Experimental | Studio 管理 Provider、Agent、Graph、Project、Worldbook 和 Regex Collection；Agent 只有可编辑 instruction 模板，支持 dotted runtime macros、完整展开预览、handoff 在节点调用前展开、Provider/model/generation/advanced/tool 配置和当前/最近 Graph Trace | `src/airp/engine/agent_definitions.py`、`src/airp/engine/macros.py`、`src/airp/engine/graph_runtime.py`、`src/airp/engine/node_runner.py`、`src/airp/engine/runtime.py`、`src/airp/web/studio.html`、`skills/tests/test_agent_instruction_macros.py`、ADR-0017 | 宏只接收 catalog 和显式加载内容，不预加载整本世界书；浏览器真实多节点 DeepSeek 调试流仍需继续验收 |
| Content-neutral Agent Framework | Implemented | Framework 只负责 Agent/Graph/Artifact/Trace 运作；Agent Regex Collection 按用户配置处理节点输入/输出，Graph 最终 Artifact 由 Host Commit 原样提交。Project 不再保存 Graph 或 Adapter。 | `engine/active_graph.py`、`engine/graph_runtime.py`、`host/graph_turn_commit.py`、`compat/legacy_turn_import.py` | 旧标签文本只在一次性历史导入时解析；现行 Run 不含 RP Adapter 或内置写作规则。 |
| Provider Node Runner + OpenAI-compatible Provider | Proven | `ProviderNodeRunner` 负责单节点流式调用、tool-call loop、abort、输入/输出 Regex；`OpenAICompatibleProviderAdapter` 直接调用 `/v1/chat/completions` 或 `/v1/responses`，不经过 sidecar | `src/airp/engine/node_runner.py`、`src/airp/engine/provider.py`、`skills/tests/test_provider_execution.py`、`skills/tests/test_real_deepseek_e2e.py`、ADR-0019 | 真实 DeepSeek E2E 为 opt-in；具体 provider 的非标准参数由 Agent advanced JSON 原样承担 |
| Atomic Turn Commit | Experimental | commit 前只强制技术完整性：非空可见正文、严格 MVU/schema、optimistic revision、唯一 commit 与可重试幂等投影；字数、文风、人称、NSFW、摘要/选项等属于 Agent instruction 的软约束，不阻止 commit | `src/airp/host/rp/session_runtime.py`、`src/airp/engine/quality.py`、`src/airp/engine/mvu.py`、`skills/tests/test_graph_execution.py`、Ticket 05 contract、ADR-0006 | 持久 generation lease 与启动 recovery 已覆盖；provider 调用中强杀进程的广泛故障注入仍不足；分支语义见 Revision Branch 行 |
| Session Event Stream / Command API | Experimental | 玩家命令经 session-scoped 幂等 `SessionCommandService` 进入 runtime；stdlib HTTP 暴露 submit/cancel/snapshot、存档 CRUD 与 SSE（按 sequence 续接 queued→preview→committed→terminal）；刷新后可从同一 DB 恢复 snapshot/commit/projection | `src/airp/engine/commands.py`、`src/airp/server.py`、`skills/tests/test_graph_execution.py`、`skills/tests/test_agent_studio_golden_path.py`、ADR-0007/0012 | 每个 server 只有一个活动 session event stream；进程在 provider 调用中被强杀后的广泛故障注入仍不足；真实 DeepSeek 浏览器路径待验证 |
| Revision Branch / Reroll-Rollback | Experimental | 每 session 单调唯一 revision + `parent_revision` DAG + 单 active head；reroll 复用原输入/parent 建新 tip；rollback 移 head 不删历史；superseded 分支可审计但不进 active projection/manifest | `src/airp/host/rp/session_runtime.py`、`src/airp/engine/commands.py`、`src/airp/server.py`、`skills/tests/test_graph_execution.py`、Ticket 06 contract、ADR-0008 | generation lease 防止并发 head 操作；无 multi-head UI；真实浏览器/DeepSeek E2E 未做 |
| Sequential Agent Graph | Experimental | 游戏界面选择当前激活 Graph；Graph Definition 以稳定 node ID、Agent 引用和顺序驱动 pipeline；单节点也产生 node lifecycle events，浏览器 Agent Trace 实时显示节点、模型调用和工具运行。 | `src/airp/engine/active_graph.py`、`src/airp/engine/graph_definitions.py`、`src/airp/engine/graph_runtime.py`、`src/airp/web/studio.html` | 旧 `styles/graphs/*.json` 仅一次性导入到 Studio；不支持任意条件 DSL；当前 Trace 是执行时间线，不是可编辑画布或分支 DAG |

## Working

| 功能 | 状态 | 当前情况 | 主要缺口 |
|---|---|---|---|
| 卡片/世界书兼容 | Working | 可解析 PNG/JSON/TXT、世界书、正则和美化相关资产；`刻晴.json` 已验证标准宏、世界书不误判变量与完整游玩 | 缺少统一兼容报告、广泛 fixture 矩阵和可视化调试 |
| 后台 NPC 与剧情规划 | Working | 规则和回合流程已定义，存在 story plan 与记忆文件 | 长期演化质量、可解释性和独立验证不足 |
| 文风与防抢话 | Working | settings/profile/硬性门禁已存在 | 仍依赖单 agent 自觉执行，缺少独立审校闭环 |

## Broken / 关键缺口

| 功能/问题 | 状态 | 为什么重要 | 当前证据 |
|---|---|---|---|
| Claude Code runtime | Broken | `wait_pending`/session loop 不适合作为独立产品 runtime | 当前流程依赖 ScheduleWakeup 与 Claude session |
| 上下文精确控制 | Broken | 追加式 transcript 会累积历史和按需 Grep tool result，无法完全由引擎决定 prompt 形状 | ADR-0001 |
| 世界模拟 | Broken | 后台 NPC、事件和伏笔尚不够自主、稳定或可解释 | 主要依赖单 agent 每轮遵守规则 |
| 卡片兼容诊断 | Broken | 导入失败、变量规则、正则、美化问题难以定位和向玩家解释 | 无完整诊断报告/测试矩阵 |
| 自动角色资料补全 | Planned | agent 尚未形成可追溯的 Web Search→资料摘要→设定采纳流程 | 产品原则已确认，未实现 |
| 玩家实时编辑 | Planned | 玩家尚不能有结构化、即时、可追溯地编辑设定 | 产品方向已确认，未实现 |

## Planned

- 产品化独立 harness/runtime，并退役 Claude Code 直驱 loop；
- 多 agent 编排：叙事导演、世界模拟、角色演化/文风润色；
- 玩家实时设定编辑；
- 卡片/世界书兼容诊断与修复建议；
- 外部角色资料的自主检索、来源记录和玩家采纳机制；
- 扩大卡片兼容 fixture、真实 provider 浏览器长会话与故障注入验收。
