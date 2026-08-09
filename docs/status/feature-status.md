# 功能成熟度矩阵

本表记录当前 canonical AIRP runtime 的代码与测试证据。旧 Claude Code/file-loop 路径已经从源码和测试入口移除；旧静态配置只在明确的导入迁移边界读取。

## Proven

| 功能 | 证据 | 当前边界 |
|---|---|---|
| 卡片导入与存档 | `src/airp/import_card.py`、`src/airp/import_prepare.py`、`tests/test_import_compatibility.py` | PNG/JSON/TXT 与代表性 SillyTavern JSON v2 已覆盖；兼容矩阵仍可扩充 |
| RP Session/Revision/Projection | `src/airp/host/rp/session_runtime.py`、`tests/test_graph_execution.py` | 单 server 服务一张卡；card-local multi-session 已持久化 |
| GraphRuntime 与静态接力链 | `src/airp/engine/graph_runtime.py`、`src/airp/engine/graph_definitions.py`、`tests/test_graph_execution.py` | 严格串行、连接 Handoff Prompt、Regex 后交接与固定次数循环已覆盖；不支持任意条件 DSL |
| Pi Agent 执行器 | `src/airp/engine/pi_node_runner.py`、`src/airp/resources/pi_agent_sidecar.mjs`、`tests/test_pi_node_runner.py`、`tests/test_pi_runtime_ticket31.py` | canonical 路径使用 Pi Core 的单 Agent 多轮工具 loop；本地 OpenAI-compatible SSE 已验证两玩家回合的双轮固定接力、失败整图重跑、Trace、Regex 与唯一提交。每个 Graph Run 销毁临时 transcript。Python ProviderNodeRunner 保留给确定性测试及显式回退 |
| Studio 配置库 | `src/airp/engine/studio_library.py`、`src/airp/server.py`、`src/airp/web/index.html` | Provider、Agent、Graph、Project、Worldbook、Regex Collection 由 Workspace 持有 |
| 默认协作套件一次性安装 | `src/airp/default_collaboration_suite.py`、`tests/test_default_collaboration_suite_recovery.py`、`tests/test_default_collaboration_suite_http.py` | Workspace 锁、pending WAL、实际 ID receipt、逆序补偿、重启恢复和脱敏启动诊断已覆盖；默认 Provider、Writer/Reviewer、三节点循环 Graph 和 Regex Collection 首次打开即加载且可编辑；新建、导入、复制 Project 仅首次激活，clear/改选、Graph 删除及 Project 删除重建语义已有 HTTP 契约；complete 后不检查、修复或复活用户编辑/删除的对象 |
| 集成式游戏工作区与 Studio 抽屉 | `src/airp/web/index.html`、`src/airp/web/game-workspace-contract.js`、`src/airp/web/game-drawer.js`、`tests/browser/workspace.spec.mjs`、`docs/specs/integrated-game-studio-workspace.md` | 游戏页提供互斥的顶部下拉抽屉；Monitor 保留唯一活动 Graph 下拉和 Graph/Trace 上下文；四类 Studio 保存携带 `expected_revision`，安全启动诊断不回显 key，运行中保存从下一 Task 生效；Playwright 覆盖桌面/移动布局、溢出与交叠 |
| 游戏抽屉与跨 Project 存档恢复 | `src/airp/host/rp/project_runtime.py`、`src/airp/web/game-drawer.js`、`tests/test_game_project_drawer.py`、`tests/test_studio_projects.py` | 导入入口接收 JSON；SillyTavern 内嵌 `character_book` 会创建并绑定 Worldbook；删除当前 Project 时自动切换到剩余 Project |
| Agent instruction 宏 | `src/airp/engine/macros.py`、`src/airp/engine/context_compiler.py`、`tests/test_agent_instruction_macros.py` | 宏只读显式 runtime snapshot，不扫描 filesystem |
| 世界书按需 capability | `src/airp/engine/worldbook_library.py`、`src/airp/host/rp/tools.py`、`tests/test_studio_worldbooks.py` | Project 可绑定多本 Worldbook，Agent 通过 exact-title tool 读取正文 |
| Regex Collection | `src/airp/engine/regex_collections.py`、`src/airp/engine/regex_transformer.py`、`tests/test_regex_collections.py` | 每个 Agent 可绑定集合；按顺序应用 input/output/both |
| Agent Trace/SSE | `src/airp/server.py`、`src/airp/host/rp/session_runtime.py`、`tests/test_agent_studio_golden_path.py` | 保留当前/最近 Graph Run；节点输入输出和模型/工具事件可审计 |
| Session Turn Runtime 最小回合契约 | `docs/adr/0021-independent-runtime-turn-contract.md`、`src/airp/host/rp/session_runtime.py`、`src/airp/host/rp/commands.py` | canonical Runtime 独占 Task、Context、唯一 commit、projection 和 durable events；真实 Provider qualification 由 ADR-0022 定义 |
| Project/Session 持久化边界 | `docs/adr/0023-project-owned-runtime-and-active-projection.md`、`src/airp/host/rp/project_runtime.py`、`src/airp/host/rp/session_manager.py`、`tests/test_game_project_drawer.py` | 多 Project/Session 可持久化且单 active projection 可恢复；删除会清理 Project-owned runtime、Session DB 与 active/recent metadata |
| 卡片/世界书兼容诊断 | [`ADR-0024`](../adr/0024-import-diagnostics-report.md)、`src/airp/import_card.py`、`src/airp/import_prepare.py`、`src/airp/web/game-drawer.js`、`tests/test_import_compatibility.py`、`tests/test_studio_projects.py` | 发出 `airp.import-diagnostics` v1；显示 success/degraded/failed、来源路径、世界书计数和绑定结果 |
| MVU 与本地安全边界 | [`ADR-0025`](../adr/0025-mvu-and-local-security-boundary.md)、`src/airp/engine/mvu.py`、`src/airp/server.py`、`src/airp/web/index.html`、`tests/test_mvu_strict_schema.py`、`tests/test_server_security.py` | strict wildcard、脚本/HTML allowlist、默认 loopback、Origin 检查、暴露模式 capability、body 限制均有回归证据；loopback 无 Origin 保留本地兼容请求 |
| 游玩中设定编辑与追溯 | [`ADR-0026`](../adr/0026-project-editing-and-trace-semantics.md)、`src/airp/engine/revisions.py`、`src/airp/engine/*_definitions.py`、`src/airp/host/rp/session_runtime.py`、`tests/test_revision_audit.py`、`tests/test_config_provenance.py` | Project/Library revision、expected revision 冲突、哈希审计及 Task 配置 provenance 已接入；Session revision 独立 |
| 角色卡脚本资源 | `src/airp/resources/run_card_scripts.cjs`、`src/airp/resources/mvu_shared.cjs` | 资源随 Python 包分发；不依赖 `skills/` |

## Experimental

| 功能 | 当前情况 | 主要缺口 |
|---|---|---|
| Provider 发布 qualification / 浏览器真实长会话 | 默认 CI 已覆盖 deterministic 20 回合、两次 `Last-Event-ID` SSE 重连、Runtime 重启、失败分类和任务级 Token/latency/cost；Playwright 已覆盖 1440/1280 桌面与 390/360 移动几何门槛；DeepSeek 单回合 opt-in smoke 通过，20 回合真实 route 命令已提供 | 每个发布 route 仍需由维护者显式运行 `AIRP_RUN_REAL_QUALIFICATION=1` 并保存真实 20 回合证据；未运行前保持 Experimental |
| 后台 NPC 与剧情规划 | 作为用户 Agent instruction/Worldbook 内容运行 | 引擎不提供内置叙事规则，质量取决于用户配置 |

## 明确不属于引擎

- 文风、人称、NSFW、字数、摘要/选项、标签或正文格式。
- Prompt preset、游戏页写作偏好面板、项目级 Runtime Config。
- `skills/` 目录扫描、隐式 skill discovery、隐藏 Agent 或 Provider。

这些内容如果需要，必须由用户在 Studio 的 Agent instruction、宏、Worldbook 或 Regex Collection 中显式定义。
