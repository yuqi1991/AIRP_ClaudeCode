# 功能成熟度矩阵

本表记录当前 canonical AIRP runtime 的代码与测试证据。旧 Claude Code/file-loop 路径已经从源码和测试入口移除；旧静态配置只在明确的导入迁移边界读取。

## Proven

| 功能 | 证据 | 当前边界 |
|---|---|---|
| 卡片导入与存档 | `src/airp/import_card.py`、`src/airp/import_prepare.py`、`tests/test_import_compatibility.py` | PNG/JSON/TXT 与代表性 SillyTavern JSON v2 已覆盖；兼容矩阵仍可扩充 |
| RP Session/Revision/Projection | `src/airp/host/rp/session_runtime.py`、`tests/test_graph_execution.py` | 单 server 服务一张卡；card-local multi-session 已持久化 |
| GraphRuntime 与顺序 Agent Graph | `src/airp/engine/graph_runtime.py`、`src/airp/engine/graph_definitions.py`、`tests/test_graph_execution.py` | 当前是稳定顺序 pipeline，不支持任意条件 DSL |
| Provider Node Runner | `src/airp/engine/node_runner.py`、`src/airp/engine/provider.py`、`tests/test_provider_execution.py` | canonical 能力为流式 chat completions/responses、tool loop、abort、input/output Regex；真实 route 仍需 ADR-0022 qualification |
| Studio 配置库 | `src/airp/engine/studio_library.py`、`src/airp/server.py`、`src/airp/web/studio.html` | Provider、Agent、Graph、Project、Worldbook、Regex Collection 由 Workspace 持有 |
| 集成式游戏工作区与 Studio 抽屉 | `src/airp/web/index.html`、`src/airp/web/game-workspace-contract.js`、`src/airp/web/game-drawer.js`、`docs/specs/integrated-game-studio-workspace.md` | 游戏页提供互斥的顶部下拉抽屉；游戏抽屉支持 Project 搜索、导入、删除、编辑、活动 Project 切换与每个 Project 的最后 Session 恢复；右侧 Monitor 保持存档和 Graph/Trace 上下文 |
| 游戏抽屉与跨 Project 存档恢复 | `src/airp/host/rp/project_runtime.py`、`src/airp/web/game-drawer.js`、`tests/test_game_project_drawer.py`、`tests/test_studio_projects.py` | 导入入口接收 JSON；SillyTavern 内嵌 `character_book` 会创建并绑定 Worldbook；删除当前 Project 时自动切换到剩余 Project |
| Agent instruction 宏 | `src/airp/engine/macros.py`、`src/airp/engine/context_compiler.py`、`tests/test_agent_instruction_macros.py` | 宏只读显式 runtime snapshot，不扫描 filesystem |
| 世界书按需 capability | `src/airp/engine/worldbook_library.py`、`src/airp/host/rp/tools.py`、`tests/test_studio_worldbooks.py` | Project 可绑定多本 Worldbook，Agent 通过 exact-title tool 读取正文 |
| Regex Collection | `src/airp/engine/regex_collections.py`、`src/airp/engine/regex_transformer.py`、`tests/test_regex_collections.py` | 每个 Agent 可绑定集合；按顺序应用 input/output/both |
| Agent Trace/SSE | `src/airp/server.py`、`src/airp/host/rp/session_runtime.py`、`tests/test_agent_studio_golden_path.py` | 保留当前/最近 Graph Run；节点输入输出和模型/工具事件可审计 |
| Session Turn Runtime 最小回合契约 | `docs/adr/0021-independent-runtime-turn-contract.md`、`src/airp/host/rp/session_runtime.py`、`src/airp/host/rp/commands.py` | canonical Runtime 独占 Task、Context、唯一 commit、projection 和 durable events；真实 Provider qualification 由 ADR-0022 定义 |
| Project/Session 持久化边界 | `docs/adr/0023-project-owned-runtime-and-active-projection.md`、`src/airp/host/rp/project_runtime.py`、`src/airp/host/rp/session_manager.py`、`tests/test_game_project_drawer.py` | 多 Project/Session 可持久化且单 active projection 可恢复；Project-owned runtime 删除清理仍待实现 |
| 角色卡脚本资源 | `src/airp/resources/run_card_scripts.cjs`、`src/airp/resources/mvu_shared.cjs` | 资源随 Python 包分发；不依赖 `skills/` |

## Experimental

| 功能 | 当前情况 | 主要缺口 |
|---|---|---|
| Provider 发布 qualification / 浏览器真实长会话 | ADR-0022 已锁定能力契约；DeepSeek 单回合 opt-in smoke 存在 | 仍需每个发布 route 的 20 回合 soak、2 次 SSE 重连和 1 次 Runtime 重启证据 |
| 卡片/世界书兼容诊断 | [`ADR-0024`](../adr/0024-import-diagnostics-report.md) 已锁定 `airp.import-diagnostics` v1、逐字段 provenance 和 success/degraded/failed 语义；原型已验证完整/降级/失败三类输入 | `import_card.py`/`import_prepare.py` 尚未发出正式报告；缺真实 fixture、API 返回和游戏抽屉展示 |
| MVU 与本地安全边界 | [`ADR-0025`](../adr/0025-mvu-and-local-security-boundary.md) 已锁定 strict commit、schema wildcard、脚本禁用、loopback/Origin/capability 和 secret/HTML 边界 | 现有 strict validator、路径约束和 SecretStore 已有证据；wildcard、host/CORS/auth、sanitizer 与脚本移除仍待实现 |
| 后台 NPC 与剧情规划 | 作为用户 Agent instruction/Worldbook 内容运行 | 引擎不提供内置叙事规则，质量取决于用户配置 |

## 明确不属于引擎

- 文风、人称、NSFW、字数、摘要/选项、标签或正文格式。
- Prompt preset、游戏页写作偏好面板、项目级 Runtime Config。
- `skills/` 目录扫描、隐式 skill discovery、隐藏 Agent 或 Provider。

这些内容如果需要，必须由用户在 Studio 的 Agent instruction、宏、Worldbook 或 Regex Collection 中显式定义。
