# 总体架构与回合数据流

## 当前架构

生产实现统一位于 `src/airp`。`airp.engine` 是内容中立的 Graph/Provider/Context/Regex/Macro 层；`airp.host.rp` 是故事会话、卡片投影、世界书和 RP capability 层；`airp.server` 提供 HTTP/SSE transport；`airp.cli`/`airp.launcher` 是唯一启动入口。网页资源在 `airp.web`，卡片脚本运行资源在 `airp.resources`，用户可变配置在 Workspace。

```text
Browser ── HTTP commands / SSE ──► airp.server
                                      │
                                      ▼
                               SessionManager
                                      │
                                      ▼
                            SessionTurnRuntime (RP host)
                                      │
                   ┌──────────────────┼──────────────────┐
                   ▼                  ▼                  ▼
             Context Manifest   GraphRuntime       SQLite lineage
                                      │
                           ProviderNodeRunner
                                      │
                                      ▼
                           card/state/content projection
```

`airp-runtime` 负责导入或恢复卡片、恢复活动 session、交付 opening 并启动 `0.0.0.0:8765`。Studio 是 Provider、Agent、Graph、Project、Worldbook 和 Regex Collection 的唯一编辑入口；游戏页只选择激活 Graph。

## 深模块

| Module | 职责 |
|---|---|
| `airp.engine.graph_runtime` | 按 Graph Definition 调度节点，生成生命周期结果 |
| `airp.engine.node_runner` | 展开 Agent instruction、调用 Provider、执行工具/Regex |
| `airp.engine.provider` | OpenAI-compatible `/v1/chat/completions` 与 `/v1/responses` 流式适配 |
| `airp.engine.context_compiler` | 将 revision snapshot 编译成可重放 Context Manifest |
| `airp.engine.regex_collections` | 按 Agent 绑定的顺序规则处理 input/output/both |
| `airp.host.rp.session_runtime` | task、event、revision、opening、commit、projection |
| `airp.host.rp.session_manager` | card-local session 创建、切换、重命名、删除 |
| `airp.host.rp.tools` | Session、memory、Worldbook 的显式只读 capability |
| `airp.import_card` / `airp.import_prepare` | PNG/JSON/TXT 导入和卡片初始化 |

Engine 不解析用户正文标签，也不内置文风、人称、NSFW、字数或输出格式。需要这些要求时，用户在 Agent instruction 或宏中编辑。

## 导入与启动

1. `airp.import_prepare.prepare_card()` 清理旧 marker，并调用 `airp.import_card.run_import()`。
2. 导入器解析 PNG/JSON/TXT，写卡片事实、世界书 catalog/正文、opening 和变量基线。
3. `airp.resources` 从 wheel 内置 `airp.web` 复制只读资源到运行 projection；不读取 `skills/`。
4. CLI 构造 `SessionTurnRuntime`，从 Workspace 读取当前 Studio Graph/Project，并交付 opening。
5. 服务器启动后，浏览器通过 `/v1/session/commands/submit`、SSE 和 `/v1/studio/*` 交互。

## 回合流程

1. submit 创建带幂等键的持久 task。
2. runtime 冻结 card、Project、Graph 和绑定世界书，编译 Context Manifest。
3. GraphRuntime 顺序执行 Agent 节点；ProviderNodeRunner 发布 model/tool/node lifecycle events，并流式发布 `narrative.preview.delta`。
4. 任一节点失败即整图失败，错误节点写入 Trace；用户点击整图重跑时创建新 attempt。
5. 成功结果原子写入 commit/revision/state，并重建 `chat_log.json`、`state.js`、`content.js` 投影。

## 可调试性

Studio Agent Trace 至少保留当前/最近一次 Graph Run 的节点输入、instruction 展开结果、模型请求摘要、输出、错误、token/耗时和工具调用。节点详情来自真实 node run 记录；没有 node run 时应明确显示尚未创建，而不是伪造“成功”。

## 数据分层

| 位置 | 内容 |
|---|---|
| `<card>/` | `.card_data.json`、`memory/`、`.runtime.sqlite3` 与兼容投影 |
| Workspace `library/` | Provider、Agent、Graph、Worldbook、Regex Collection |
| Workspace `projects/` | 角色卡/故事 Project 与绑定关系 |
| Workspace `secrets.json` | Provider API key（本地 Secret Store） |
| `src/airp/web/` | wheel 内置只读网页资源 |
| `src/airp/resources/` | wheel 内置卡片脚本资源 |

仓库不再保留第二套 skills runtime。旧静态配置只在明确的 Studio migration 边界读取，canonical runtime 不会从 `skills/` 发现 Agent、工具或提示词。
