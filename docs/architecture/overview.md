# 总体架构与回合数据流

## 当前架构

当前系统由浏览器前端、本地 HTTP server、CLI 编排和兼容入口组成。生产实现统一位于 `src/airp/`：`airp.engine` 承载内容中立的 Graph/Provider/Context/Regex 引擎，`airp.host.rp` 承载故事会话、卡片投影和 RP 工具，`airp.server` 承载 `/v1/*` transport，`airp.cli`/`airp.launcher` 承载可安装启动入口，`airp.handler` 与 `airp.import_*` 承载 RP 兼容投影和导入。`skills/` 只保留旧 import、脚本和兼容 wrapper。独立 runtime 已有可玩的 card-local 多 Session 黄金路径；原 Claude Code loop 仍保留为 legacy 路径。

ADR-0018/0019 确定当前架构：Agent Framework 只负责内容中立的 Agent/Graph 运作，RP 投影和会话提交属于 Host；RP 输出不做内置标签解析，格式转换只存在于一次性 legacy import。Studio 定义是正常运行的唯一来源，旧配置仅用于迁移和回放；Provider 通过 AIRP 自有 OpenAI-compatible adapter 直接调用，用户可变数据统一进入 Workspace，HTTP 正式入口统一为 `/v1/*`。

```text
Browser ── HTTP commands / SSE ──► airp.server
                                      │
                                      ▼
                                SessionManager
                                      │ active runtime
                                      ▼
                              SessionTurnRuntime
                       ┌──────────────┼──────────────┐
                       ▼              ▼              ▼
                Context Manifest   Provider       SQLite lineage
                       │              │              │
                       └──────► harness commit ◄─────┘
                                      │
                                      ▼
                         chat/state/content projection
```

`airp-runtime`（实现于 `airp.cli`）负责导入或恢复卡片、恢复活动 session、交付 revision 0 开场并启动统一服务器。`SessionManager` 隐藏 SQLite catalog、活动指针、runtime 更换和兼容投影重建；浏览器提交、取消、重roll、回退、开场/存档切换和刷新恢复均通过 runtime interface 完成，不依赖 `.pending` 或 `input.txt`。`skills/start_runtime.py` 仅转发到同一 CLI，`skills/` 不参与 Agent 能力发现。

### Legacy Claude Code 路径

```text
Browser
  │ POST /v1/session/commands/submit
  ▼
server.py ── input.txt/.pending ──► runtime loop (Claude Code 当前实现)
                                      │
                                      ▼
                               round_prepare.py
                                      │ round_context.txt
                                      ▼
                              Narrative agent writes response.txt
                                      │
                                      ▼
                               round_deliver.py
                         ┌────────────┼────────────┐
                         ▼            ▼            ▼
                    handler.py   write_memory   token checkpoint
                         │
                         ▼
                 chat_log/state/content → Browser
```

## 深模块

| Module | Interface | 隐藏的实现职责 |
|---|---|---|
| `airp.engine.card` | 读取/写入卡片状态与回合 | `chat_log.json`、state.js、变量差分、回退与重roll |
| `airp.engine.render` | 将回合/变量渲染为前端载荷 | 宏替换、beautify panel、状态条、content.js |
| `airp.engine.mvu` | 解析、验证、执行变量命令 | JSONPatch、路径解析、schema、变量审计、checklist |
| `airp.engine.tokens` | checkpoint 与 token delta | Claude transcript 定位、usage 解析、累计记账 |
| `airp.engine.worldbook` | 构建 catalog 与按标题读取条目 | usage 保留、条目索引、reference/user Markdown 定位 |
| `airp.engine.context` | 构建启动/回合上下文 | import_context、变量路径列表、catalog 展示 |
| `airp.engine.context_compiler` | 将 revision-scoped snapshot 编译为 Manifest/payload | section 选择、稳定顺序、预算、hash、重放 |
| `airp.host.rp.session_runtime` | 提交、取消、回退、重roll、snapshot 与 opening | task lease、event/commit/revision DAG、state snapshot、幂等与投影恢复 |
| `airp.host.rp.session_manager` | 列出、创建、切换、重命名和删除存档 | card-local catalog、活动指针、runtime 生命周期与删除清理 |
| `airp.host.rp.tools` | Session、memory、Worldbook 的只读 Agent capabilities | Agent allowlist、冻结快照、调用审计与稳定错误 |

这些模块的原则是：调用方只需知道少量 interface，文件格式、HTML、MVU 路径与 transcript 细节留在模块内部。

## 导入流程

1. `airp.import_prepare.prepare_card()` 清理旧运行态；
2. `airp.import_card.run_import()` 解析 PNG/JSON/TXT；
3. 写入卡片目录中的卡数据、世界书正文、变量基线、开场和记忆文件；
4. 初始化当前前端 state/content 与 `.card_path`；
5. `airp.engine.context.build_import_context()` 写入 `import_context.txt`；
6. 叙事 agent 补齐世界书 usage 后交付开场。

## 回合流程

独立 runtime 的回合流程：

1. 浏览器 `POST /v1/session/commands/submit`，runtime 建立带幂等键的持久 task；
2. runtime 在 task 的 base revision 冻结 card、Project、Studio Graph 和相关世界书输入，编译 Context Manifest；旧配置只在一次性导入/兼容回放边界读取，不参与现行 Graph Run；
3. provider 流式产生 preview 与最终叙事文本，SSE 向浏览器发布任务状态；
4. harness 解析文本、执行质量与 MVU 门禁，原子写入 commit/revision/state；
5. active lineage 重建兼容 `chat_log.json`、`state.js` 与 `content.js` 投影。

### 浏览器流式、Provider 与 Graph 可观测性

`narrative.preview.delta` 是 durable event 的临时浏览器投影：前端按 task id 累积 delta 为一个“生成中”的 AI 回合；成功、失败、取消、开场或存档切换时清除它，正式 `turn.committed` 投影仍是唯一历史事实。这使 SSE 中断时的 snapshot/content 轮询恢复不会把半成品写进 `chat_log.json`。

Provider 面板通过 `/v1/studio/providers` 管理 Provider Profile，并将非敏感连接资料保存到 Workspace。网页输入的 key 只进入本地 Secret Store；它不进入 Graph、Execution Plan、事件、manifest、SQLite、projection 或响应。

顺序 graph 无论有一个还是多个节点，均通过 `GraphRuntime` 调度、`ProviderNodeRunner` 执行。runtime 发布 `agent_node.started/finished`、`model_call.started/finished` 与 `tool_run.*` durable events；前端 Agent Trace 以此显示当前节点、模型耗时/token 和工具结果。graph 仍是受限的线性 pipeline，不是通用工作流 DSL。

存档切换不复制 JSON 文件：`SessionManager` 选择目标 session 的 runtime，并由其 active lineage 原地重建共享 projection。生成 lease 活动时，创建、切换和删除返回冲突，避免浏览器在 provider 调用中途换掉 active runtime。

开场是 Host Session 的初始化阶段：卡片已有 `first_mes` 时直接交付；没有时由当前 Studio Graph 执行一次 AI-only opening。结果写入 revision 0 的 opening，不创建玩家 task/commit/revision；这样玩家第一条输入始终对应 revision 1。引擎不解析标签或写作格式，开场策略由 Host 和用户配置的 Agent instruction 决定。

Legacy Claude Code 回合流程：

1. 浏览器将用户输入写入兼容投影目录的 `input.txt`；
2. `round_prepare.py` 读取 settings、catalog、变量、近期记忆和近三轮对话，写 `round_context.txt`；
3. 当前 Claude Code agent 读取上下文，按 catalog 按需加载最多 2–3 个世界书条目，写 `response.txt`；
4. `round_deliver.py` 执行字数门禁、token 收集、调用 handler、写记忆；
5. `handler.append_turn()` 执行 MVU 命令，保存 turn，重建 content.js/state.js，通知前端。

## 运行时数据分层

| 位置 | 归属 | 例子 |
|---|---|---|
| `<card>/` | 卡片事实、共享基线和持久 session store | `.card_data.json`、`.session_init`、`.initvar.json`、`.runtime.sqlite3`、`memory/` |
| `<card>/memory/` | 卡片级共享叙事与设定 | `reference.md`、`project.md`、`story_plan.md`、`.worldbook_index.json` |
| `<card>/` 与运行态 projection root | 当前活动 session 的兼容投影 | `chat_log.json`、`.var_diff.json`、`content.js`、`state.js` |
| `src/airp/engine/` | 可复用纯逻辑代码 | card/render/mvu/tokens/worldbook/context |
| `src/airp/web/` | wheel 内置只读网页资源 | `index.html`、`studio.html`、默认静态片段 |
| `src/airp/engine/provider.py` | OpenAI-compatible Provider Adapter | `/v1/chat/completions`、`/v1/responses` |
| Workspace / `AIRP_STATIC_ROOT` | 用户可写 Studio、Secret 和运行投影 | library、projects、sessions、secrets、projection |

> 一个 server 进程当前仍只服务一张卡，且卡片级素材与 `memory/*.md` 是共享输入。多 session 已摆脱 JSON 历史单例，但多卡并行和 session-scoped 长期记忆仍是后续数据分层工作。
