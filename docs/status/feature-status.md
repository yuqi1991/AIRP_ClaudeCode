# 功能成熟度矩阵

本表记录当前 canonical AIRP runtime 的代码与测试证据。旧 Claude Code/file-loop 路径已经从源码和测试入口移除；旧静态配置只在明确的导入迁移边界读取。

## Proven

| 功能 | 证据 | 当前边界 |
|---|---|---|
| 卡片导入与存档 | `src/airp/import_card.py`、`src/airp/import_prepare.py`、`tests/test_import_compatibility.py` | PNG/JSON/TXT 与代表性 SillyTavern JSON v2 已覆盖；兼容矩阵仍可扩充 |
| RP Session/Revision/Projection | `src/airp/host/rp/session_runtime.py`、`tests/test_graph_execution.py` | 单 server 服务一张卡；card-local multi-session 已持久化 |
| GraphRuntime 与顺序 Agent Graph | `src/airp/engine/graph_runtime.py`、`src/airp/engine/graph_definitions.py`、`tests/test_graph_execution.py` | 当前是稳定顺序 pipeline，不支持任意条件 DSL |
| Provider Node Runner | `src/airp/engine/node_runner.py`、`src/airp/engine/provider.py`、`tests/test_provider_execution.py` | 支持流式 chat completions/responses、tool loop、abort、input/output Regex |
| Studio 配置库 | `src/airp/engine/studio_library.py`、`src/airp/server.py`、`src/airp/web/studio.html` | Provider、Agent、Graph、Project、Worldbook、Regex Collection 由 Workspace 持有 |
| 游戏抽屉与跨 Project 存档恢复 | `src/airp/host/rp/project_runtime.py`、`src/airp/web/game-drawer.js`、`tests/test_game_project_drawer.py` | 游戏页提供 Project 搜索/导入/编辑、活动 Project 切换与每个 Project 的最后 Session 恢复；导入入口当前接收 JSON |
| Agent instruction 宏 | `src/airp/engine/macros.py`、`src/airp/engine/context_compiler.py`、`tests/test_agent_instruction_macros.py` | 宏只读显式 runtime snapshot，不扫描 filesystem |
| 世界书按需 capability | `src/airp/engine/worldbook_library.py`、`src/airp/host/rp/tools.py`、`tests/test_studio_worldbooks.py` | Project 可绑定多本 Worldbook，Agent 通过 exact-title tool 读取正文 |
| Regex Collection | `src/airp/engine/regex_collections.py`、`src/airp/engine/regex_transformer.py`、`tests/test_regex_collections.py` | 每个 Agent 可绑定集合；按顺序应用 input/output/both |
| Agent Trace/SSE | `src/airp/server.py`、`src/airp/host/rp/session_runtime.py`、`tests/test_agent_studio_golden_path.py` | 保留当前/最近 Graph Run；节点输入输出和模型/工具事件可审计 |
| 角色卡脚本资源 | `src/airp/resources/run_card_scripts.cjs`、`src/airp/resources/mvu_shared.cjs` | 资源随 Python 包分发；不依赖 `skills/` |

## Experimental

| 功能 | 当前情况 | 主要缺口 |
|---|---|---|
| 浏览器真实 Provider 长会话 | 单回合 DeepSeek opt-in 测试存在 | 仍需跨 provider、断线和长会话故障注入 |
| 卡片/世界书兼容诊断 | 导入失败不阻断其它素材，变量来源有审计字段 | 缺统一可视化诊断报告与大规模 fixture |
| 后台 NPC 与剧情规划 | 作为用户 Agent instruction/Worldbook 内容运行 | 引擎不提供内置叙事规则，质量取决于用户配置 |

## 明确不属于引擎

- 文风、人称、NSFW、字数、摘要/选项、标签或正文格式。
- Prompt preset、游戏页写作偏好面板、项目级 Runtime Config。
- `skills/` 目录扫描、隐式 skill discovery、隐藏 Agent 或 Provider。

这些内容如果需要，必须由用户在 Studio 的 Agent instruction、宏、Worldbook 或 Regex Collection 中显式定义。
