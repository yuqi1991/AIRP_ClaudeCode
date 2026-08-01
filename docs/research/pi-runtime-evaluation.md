# Pi 作为 AIRP Agent Execution Layer 的可行性研究

- **状态**：技术选型前研究（未决策）
- **研究日期**：2026-07-19
- **结论**：建议做最小原型；原型通过前不新增 Accepted ADR

## 执行摘要

AIRP 当前的主要瓶颈不在角色卡、世界书、MVU 或渲染，而在产品 runtime 被 Claude Code 的会话和 harness 反向塑形：

- 浏览器输入通过 `input.txt`、`.pending`、`ScheduleWakeup` 和 `/api/wait_pending` 长轮询唤醒 Claude Code；
- `round_prepare.py` 虽能预组装本轮材料，Claude Code 的追加式 transcript 仍会保留旧对话、文件读取和世界书 Grep 结果，AIRP 无法完整决定实际模型上下文；
- token 统计依赖解析 Claude Code 本地 transcript，而不是本轮模型调用的原生 telemetry；
- session、事件、任务、中断、恢复、多 Agent 通信和状态写权限都不是 AIRP 的一等模型；
- 旧版 `skills/styles/` 曾保存当前激活卡的全局单例运行态；该目录现已迁移并删除，
  当前 session state 位于 Workspace 和卡片本地 SQLite。

这与 [ADR-0001](../adr/0001-claude-code-direct-drive-is-prototype.md) 已接受的判断一致：Claude Code 直驱是可运行原型，不是长期产品 runtime。

本研究的结论是：

> **Pi 适合作为替代 Claude Code Agent loop 的候选执行层，但不是 AIRP 所需的完整产品 runtime。**

推荐组合：

| 责任层 | 所有者 | 建议 |
|---|---|---|
| Agent loop、工具执行、流式生命周期 | Pi | 候选采用 `@earendil-works/pi-agent-core` |
| Provider/model 统一调用、usage/cost | Pi | 候选采用 `@earendil-works/pi-ai` |
| session/event/task、ContextCompiler、状态提交、多 Agent 协议、可观测性 | AIRP | 必须自建 |
| 卡片、MVU、渲染、世界书兼容逻辑 | 现有 AIRP engine | 通过 adapter 渐进复用 |
| 面向终端的 coding-agent 产品 | Pi coding agent CLI | 不作为 AIRP 玩家 runtime |

Pi 对两个当前核心问题都有合适的底层能力：

1. `transformContext()` 与 `convertToLlm()` 允许应用在每次模型调用前转换、裁剪、注入或过滤消息；
2. `prompt()`、`abort()`、`steer()`、`followUp()` 和生命周期事件允许 AIRP worker 直接驱动 Agent，无需 `ScheduleWakeup` 和 HTTP 长轮询。

但 Pi 的 `Agent.state.messages` 本身也是消息数组。如果 AIRP 把它当永久历史并无限追加，上下文漂移仍会重演。长期事实源必须是 AIRP 自己的 event/state store；Pi transcript 只能是短期执行记录。

---

## 1. 项目与包身份

用户所指的 Pi 是当前位于 [`earendil-works/pi`](https://github.com/earendil-works/pi) 的 TypeScript AI Agent toolkit。官方仓库同时包含统一 LLM API、Agent runtime、TUI 和 coding-agent CLI。

| 用途 | 当前标识 | 说明 |
|---|---|---|
| 官方仓库 | [`earendil-works/pi`](https://github.com/earendil-works/pi) | 当前官方维护的 monorepo |
| Agent runtime | [`@earendil-works/pi-agent-core`](https://github.com/earendil-works/pi/tree/main/packages/agent) | 可嵌入 Agent loop、工具和状态 |
| Provider API | [`@earendil-works/pi-ai`](https://github.com/earendil-works/pi/tree/main/packages/ai) | 多 provider/model 的统一调用层 |
| Coding CLI | [`@earendil-works/pi-coding-agent`](https://github.com/earendil-works/pi/tree/main/packages/coding-agent) | 面向终端的 coding-agent 产品 |

旧的 [`@mariozechner/pi-agent-core`](https://registry.npmjs.org/@mariozechner%2Fpi-agent-core) 已在 npm registry 标记 deprecated，并指向 `@earendil-works/pi-agent-core`。新接入不应继续使用旧 namespace。

研究时 registry 中 `@earendil-works/pi-agent-core` 的 latest 为 `0.80.10`，要求 Node.js `>=22.19.0`。实际原型应锁定精确版本，而不是依赖浮动 latest；版本和 Node 要求需在实施时重新核对。

### 为什么不直接使用 Pi coding-agent CLI

Pi coding agent 官方 README 将其描述为终端 coding harness，提供 interactive、print/JSON、RPC 和 SDK 模式，默认面向 `read`、`write`、`edit`、`bash` 等编码工具。它也有 JSONL session、分支、恢复和 compaction。

这些能力适合参考和调试，但直接将 CLI 嵌入 AIRP 会重新引入一层 coding-agent 产品语义：

- 自己的终端会话与追加式 JSONL；
- coding-oriented 默认工具和项目资源发现；
- 不属于 AIRP 领域的命令、权限和 compaction 规则；
- 无法自然表达玩家 session、世界状态提案、回合提交和资料采纳。

所以 AIRP 应嵌入 Pi 的 library surface，而不是把 Pi CLI 改造成浏览器 RP 服务。

---

## 2. AIRP 当前基线

当前回合的实际控制流为：

```text
Browser
  └─ POST /api/submit
       └─ server.py 写 input.txt / .pending
            └─ Claude Code ScheduleWakeup
                 └─ GET /api/wait_pending 长轮询
                      └─ round_prepare.py 写 round_context.txt
                           └─ Claude Code 生成 response.txt
                                └─ round_deliver.py
                                     ├─ handler.py / engine
                                     ├─ write_memory.py
                                     └─ transcript token checkpoint
                                          └─ state.js / content.js → Browser
```

详细现状见：

- [总体架构与回合数据流](../architecture/overview.md)
- [当前 Runtime 与目标 Runtime](../architecture/future-runtime.md)
- [技术债与已知限制](../status/technical-debt.md)

### 2.1 上下文不是完全可控的模型输入

`round_prepare.py` 已经建立了正确的上下文组织方向：静态 catalog 和约束在前，用户输入、变量、近期记忆和近期聊天在后；世界书通过 `title — usage` catalog 按需读取，而不是每轮预塞全部正文。

问题是 `round_context.txt` 只代表 AIRP 主动组装的一部分。Claude Code session 还会累积：

- 旧回合的对话；
- 之前的文件读取结果；
- 按需 Grep 过的世界书正文；
- 工具调用和宿主指令；
- 由 Claude Code 决定的 compaction 结果。

因此，AIRP 目前不能回答一个关键可观测性问题：**某次 provider call 最终究竟看到了哪些材料，以及每项材料为什么被包含。**

### 2.2 用户输入等待属于外部 harness

`/api/wait_pending` 通过一个最长约 300 秒的 HTTP 请求反复检查 `.pending`。`CLAUDE.md` 再要求 Claude Code 用 `ScheduleWakeup` 发起下一次检查。

这意味着：

- 玩家输入是文件信号，而不是 runtime event；
- Claude Code session 中断后没有 AIRP 自己的 task 恢复语义；
- stop、重试、去重、回放和并发没有统一状态机；
- 多 session 会受到 `.card_path`、输入文件和前端产物全局单例的限制。

### 2.3 模型、工具与 telemetry 被宿主间接承载

当前 provider 由 Claude Code 间接调用；token 使用量需要从本地 transcript 反向采集；工具是 Bash、Python CLI 和文件格式约定，而不是带 schema、权限、版本与审计的领域接口。

换 runtime 的目标不应只是“用另一套 CLI 调模型”，而应把控制权收回 AIRP。

---

## 3. Pi Agent Core 能提供什么

以下能力来自 Pi 官方 [Agent Core README](https://github.com/earendil-works/pi/blob/main/packages/agent/README.md)、[`agent.ts`](https://github.com/earendil-works/pi/blob/main/packages/agent/src/agent.ts) 和 [`types.ts`](https://github.com/earendil-works/pi/blob/main/packages/agent/src/types.ts)。

### 3.1 可嵌入的 Agent 控制面

`Agent` 暴露的主要控制方法包括：

```ts
agent.prompt(...)
agent.continue()
agent.abort()
agent.waitForIdle()
agent.reset()
agent.subscribe(...)
agent.steer(...)
agent.followUp(...)
```

并可清空 steering/follow-up queues。其状态包括 system prompt、messages、tools、model、thinking level、streaming 状态、pending tool calls 和 error。

对 AIRP 的映射：

- `prompt()`：task worker 启动一个叙事或模拟 run；
- `abort()`：玩家停止生成或 runtime 取消 task；
- `steer()`：在当前 assistant turn 的工具调用完成后，为下一次模型调用排入玩家纠偏；它不会中断正在进行的 provider stream 或工具；
- `followUp()`：当前工作完成后的内部续作；
- `subscribe()`：将生成和工具事件写入 trace，并通过 SSE/WebSocket 推送；
- `waitForIdle()`：等待一次 run 包括最终 subscriber 持久化在内完整结束。

这些方法可以消除 Claude Code 的唤醒循环，但 durable queue、task lease 和重启恢复仍需 AIRP 提供。需要立即停止当前 provider stream 或工具时应调用 `abort()` 并处理 partial output；`steer()` 只负责下一 LLM turn 的排队纠偏。

### 3.2 上下文转换 seam

Pi 在每次模型调用前提供：

```ts
transformContext(messages, signal)
convertToLlm(messages)
```

官方定义允许 `transformContext()` 裁剪或注入上下文，`convertToLlm()` 将应用消息转为模型消息，并可过滤 UI-only 或自定义消息。

AIRP 可据此建立：

```text
SessionEventStore + StateProjection + MemoryStore
  → ContextCompiler
  → ContextManifest
  → transformContext()
  → convertToLlm()
  → provider payload
```

建议每次模型调用保存：

- system prompt 和 tool policy 版本；
- 被选中的 event 范围与 memory 版本；
- 世界书条目 ID、标题和版本；
- 每个 section 的 inclusion reason、字符数和 token 估算；
- 被裁剪或摘要的项目；
- context hash、预算、provider 和 model；
- 实际 usage/cost。

这份 `ContextManifest` 才能成为“模型本轮看到了什么”的可审计答案。

#### 必须避免的误区

Pi 源码显示 `Agent` 仍把 `AgentMessage[]` 保存在 `state.messages` 中。Pi 给了应用转换 seam，但没有自动替 AIRP 制定长期上下文政策。

推荐规则：

- Pi messages 仅属于一个短期 `AgentRun`；
- AIRP event store 才是长期事实源；
- 每轮或每个 task 重新 materialize context；
- 工具结果按 retention policy 保存引用、摘要或丢弃；
- 旧 execution transcript 可供审计，但不能自动进入下一轮 prompt。

### 3.3 Typed tools 与执行 hook

Pi `AgentTool` 包含：

- `name`、`label`、`description`；
- 参数 schema；
- `execute(toolCallId, params, signal, onUpdate)`；
- parallel/sequential 执行配置；
- 流式进度；
- `beforeToolCall` 和 `afterToolCall` hook；
- block、修改结果和 terminate 等控制。

当前隐式文件/Bash 行为可以逐步升级为领域工具：

| 当前行为 | 候选领域工具 |
|---|---|
| Grep `reference.md` | `load_worldbook_entry(title)` |
| 读取变量和场景 | `get_session_snapshot(expectedRevision)` |
| 验证 MVU | `validate_state_proposal(proposal)` |
| 提交回合 | `commit_turn_draft(draft, expectedRevision)` |
| 世界推进 | `propose_world_state_change(baseRevision, patch, rationale)` |
| 外部检索 | `record_source(intent, query, url, retrievedAt, summary, confidence, uncertainty)` |

正式 runtime 不应把通用 Bash 当主要状态写入接口。所有领域写入应经过 schema、revision check、权限 policy 和 commit service。

### 3.4 生命周期事件

Pi 提供：

```text
agent_start / agent_end
turn_start / turn_end
message_start / message_update / message_end
tool_execution_start / tool_execution_update / tool_execution_end
```

AIRP 可将其映射成自己的 durable trace：

| Pi event | AIRP trace/event |
|---|---|
| `agent_start` | `agent_run.started` |
| `turn_start` | `model_call.started` |
| `message_update` | `narrative.preview.delta` |
| `tool_execution_start` | `tool_run.started` |
| `tool_execution_update` | `tool_run.progress` |
| `tool_execution_end` | `tool_run.finished` |
| `turn_end` | `model_call.finished` + usage/cost |
| `agent_end` | `agent_run.finished` |

Pi subscriber 会按注册顺序执行并被等待，因此持久化 listener 必须短、可靠、幂等。耗时世界模拟不应直接塞进 listener，而应创建后续 task。

---

## 4. Pi AI 能提供什么

根据官方 [Pi AI README](https://github.com/earendil-works/pi/blob/main/packages/ai/README.md) 和 [`types.ts`](https://github.com/earendil-works/pi/blob/main/packages/ai/src/types.ts)，Pi AI 提供统一 provider/model catalog、流式调用、消息与 tool call 表示、模型能力元数据、usage/cost、自定义 provider 和测试替身。

典型调用面为：

```ts
const models = createModels();
models.setProvider(provider);

const model = models.getModel(providerId, modelId);
const stream = models.stream(model, context, options);
const finalMessage = await stream.result();
```

对 AIRP 的价值：

1. **ProviderAdapter 基础**：AIRP 可直接持有 provider/model policy，而不是由 Claude Code 间接承载；
2. **结构化 telemetry**：每次 model call 直接记录 provider-reported usage、stop reason 和时间戳；AIRP 在请求外层测量 latency；Pi 根据模型目录单价计算 cost estimate；
3. **能力感知**：runtime 可依据 context window、max output、reasoning、图像能力等元数据选模型；
4. **自定义 provider**：可适配代理、本地 endpoint 或兼容 API；
5. **测试替身**：faux provider 可用于工具序列、abort、usage、错误和上下文回归测试。

Pi 返回的 token usage 来自 provider 响应；`usage.cost` 则由 Pi 按模型 catalog 中的费率计算，是估算值，不等同于 provider invoice。AIRP 应锁定并校验费率版本，必要时另做 billing reconciliation。Pi 的 assistant message 没有原生 latency/duration 字段，latency 必须由 AIRP 在请求边界自行测量。

### Provider 抽象的边界

统一 API 不代表不同模型的语义完全相同。DeepSeek 路径必须通过真实 contract test 验证：

- 流式文本与 reasoning；
- tool call 参数增量和并行行为；
- abort 和 partial response；
- stop reason；
- usage/cache/cost 字段；
- 长输出和上下文上限；
- error、timeout、retry 与 fallback policy。

AIRP 业务层不应直接依赖 Pi 类型。建议再包一层 AIRP-owned `ProviderAdapter`，以降低 Pi 版本迁移和 provider 差异的影响。

---

## 5. Pi 能解决与不能解决的问题

### 5.1 能显著改善

#### 上下文可控性

Pi 提供应用级 context conversion seam。结合 AIRP 自建 ContextCompiler，可以让每次 provider payload 都可重建、可测试、可哈希、可解释。

#### 去除 ScheduleWakeup 与 wait_pending

浏览器消息可直接成为 session event，dispatcher 创建 task，worker 调用 `agent.prompt()`；中断调用 `agent.abort()`，结果通过事件流返回浏览器。不需要 `.pending`、300 秒 HTTP long-poll 或 Claude Code session 唤醒。

#### Provider、工具与 telemetry

Pi AI 提供统一调用与 usage；Pi Core 提供 typed tools、hook、进度、事件和 abort signal。这比 Bash/file convention 和 transcript 逆向解析更适合作为产品 runtime 基础。

#### 自动化测试

使用 faux provider 可以固定模型响应和 tool call，建立以下回归：

- ContextManifest 的 section、预算和裁剪；
- 工具参数与调用次序；
- MVU proposal 和 state revision 冲突；
- abort、tool error、provider error 和 retry；
- reroll/rollback；
- usage/cost 限额。

### 5.2 不能自动解决

#### Durable session/event/task

Pi Agent 不等于持久任务系统。它不自动提供：

- 进程重启后的 task 恢复；
- lease、retry、dead letter；
- 幂等消息提交；
- session 锁和顺序保障；
- event replay；
- 浏览器断线续传。

#### 长期上下文治理

`transformContext()` 是能力点，不是默认政策。近期窗口、摘要、世界书选择、工具结果保留和不同 Agent 的可见权限仍由 AIRP 设计。

#### 多 Agent 领域协议

创建多个 `Agent` 实例并不等于解决状态竞态。AIRP 仍需定义 task、proposal、approval、commit、版本冲突和成本预算。

#### 数据模型与全局单例

Pi 不会自动消除 `.card_path`、`state.js`、`content.js` 和输入文件的全局语义，也不会自动解决 styles 与卡片目录双写。

#### 安全边界

Typed tools 使权限更清晰，但路径沙箱、网络 egress、不可信卡片、来源采纳、secret、CORS/XSS 和 destructive action policy 仍由 AIRP 负责。

---

## 6. 当前组件到目标组件的映射

| 当前组件 | 目标责任 | 迁移方式 |
|---|---|---|
| `/api/submit` 写 `input.txt/.pending` | `SessionEventStore.append(user.message)` | API 直接追加幂等事件 |
| `/api/wait_pending` | 无 | dispatcher 监听事件并创建 task |
| `ScheduleWakeup` | 无 | worker/actor 直接消费 task |
| Claude Code session | Pi `Agent` / `agentLoop` | 只负责短期执行 |
| Claude transcript | AIRP event store + execution trace | 不再作为领域事实源 |
| `round_prepare.py` | `ContextCompiler` | 先封装复用，后输出结构化 manifest |
| `round_context.txt` | `ContextManifest` + model context | 文件仅保留为调试兼容产物 |
| worldbook catalog + Grep | `load_worldbook_entry` tool | 保留 skill 模式，升级为 typed read |
| `response.txt` 标签 | `NarrativeTurnDraft` | 原型期保留 parser adapter |
| `round_deliver.py` | `TurnCommitService` / task stages | 门禁、提交、记忆、通知显式化 |
| `handler.py` | legacy commit adapter | 初期复用，逐步拆开领域提交与渲染 |
| `engine.mvu` | 状态 proposal 验证/执行 | 保留领域模块 |
| `engine.card` | `SessionStateRepository` adapter | 保持卡片兼容后迁移事实源 |
| `engine.render` | render projection | 最终不再依赖全局 `content.js` |
| `engine.tokens` | `ModelCallTelemetry` | 改用 provider usage/cost |
| reroll/delete | session revision/branch task | 不再重建全局 pending |

---

## 7. 推荐目标架构

```text
Browser
  ├─ commands ───────────────────────────────────────────────┐
  └─ SSE/WebSocket ◀── preview / progress / committed events │
                                                             ▼
AIRP API Gateway
  └─ auth / idempotency / event append / subscriptions
                                                             ▼
AIRP Durable Runtime
  ├─ SessionEventStore      ├─ TaskStore / queue
  ├─ State projections      ├─ ContextManifest store
  ├─ session actor/lock     ├─ TraceStore
  └─ usage/cost ledger      └─ SourceRecord store
                │                              │
                ▼                              ▼
ContextCompiler                    Multi-agent Coordinator
  role/state/memory/budget           task/proposal/commit protocol
  worldbook selection                role permissions and budgets
                └──────────────┬───────────────┘
                               ▼
Pi Execution Adapter
  ├─ @earendil-works/pi-agent-core
  └─ @earendil-works/pi-ai
          │             │                    │
          ▼             ▼                    ▼
 ProviderAdapter   Typed domain tools   Existing Python engine adapters
```

### 推荐的一等对象

| 对象 | 目的 | 关键字段示例 |
|---|---|---|
| `Session` | 一个玩家游玩实例 | `id`, `cardId`, `status`, `revision` |
| `SessionEvent` | 不可变事实流 | `seq`, `type`, `payload`, `causationId`, `correlationId` |
| `Task` | 可重试工作单元 | `kind`, `state`, `attempt`, `lease`, `inputEventRange` |
| `AgentRun` | 一次 Pi 执行 | `taskId`, `role`, `provider`, `model`, `traceId` |
| `ContextManifest` | 模型输入清单 | `sections`, `hash`, `tokenBudget`, `actualUsage` |
| `ModelCall` | provider request | `usage`, `costEstimate`, `rateVersion`, `stopReason`, `latency` |
| `ToolRun` | 工具审计 | `tool`, `argsHash`, `resultRef`, `duration` |
| `StateProposal` | Agent 的状态建议 | `baseRevision`, `patch`, `rationale`, `approvalState` |
| `SourceRecord` | 外部资料来源 | `intent`, `query`, `url`, `retrievedAt`, `summary`, `confidence`, `uncertainty`, `adoptionState`, `adoptionEventId` |
| `TurnCommit` | 已提交玩家回合 | `narrative`, `options`, `mvuDelta`, `stateRevision` |

### 推荐状态机

```text
Task:
  queued → leased → running → committing → succeeded
                    │           │
                    ├→ cancelled└→ failed_retryable → queued
                    └→ failed_terminal
```

流式 narrative 只是 preview。只有 `TurnCommit` 完成后才成为正式剧情；abort 或 commit 失败的预览不能写入聊天事实。

---

## 8. 多 Agent 协议建议

Pi 负责执行每个 Agent run；AIRP coordinator 负责它们之间的显式协议。

### 叙事导演

输入已提交场景、玩家输入、批准的世界事件、角色/风格约束、近期记忆和选定世界书。输出叙事草稿、行动选项和 MVU proposal。不能直接改历史或绕过 commit service。

### 世界模拟

输入世界状态、时间、后台 NPC、事件和 simulation budget。输出带 `baseRevision`、rationale 和 evidence 的 `StateProposal`。默认不能直接写正式状态或最终叙事。

### 角色演化/文风润色

作为草稿后处理 stage，输入叙事草稿、角色档案和风格约束，输出文本修订和一致性发现。不得更改领域事实。

### Coordinator 硬约束

1. Agent 传递 task/proposal/reference，不隐式共享无限 transcript；
2. 跨 Agent 消息带来源、父 task、correlation ID 和输入版本；
3. 所有状态写入经过唯一 `StateCommitter`；
4. 叙事只消费已批准状态；
5. 世界模拟结果默认不是玩家事实；
6. 每个 role 有独立 context budget、model policy、tool allowlist 和成本限额。

---

## 9. 渐进迁移路线

### Phase 0：基线与契约测试

在改变 runtime 前，先新增一个范围受限的实验 ADR：只授权以 Pi 作为候选执行层进行原型，并固定首版 agent 消息、proposal/commit 和状态写权限边界；它不构成正式 runtime 选型。随后收集当前导入、上下文、回合提交、MVU 和 render fixture，定义 `NarrativeTurnDraft`、`StateProposal`、`TurnCommit`、`ContextManifest` 与 tool audit contract。

使用 Pi AI 测试替身验证 context、tool call、abort、错误、usage 和 revision conflict。完成标志是当前 pipeline 不再只能靠人工 E2E 回归。

### Phase 1：单 Session、单 Agent 的 Pi 原型

最小边界：

- 独立 TypeScript worker，Node.js 版本满足锁定 Pi 包要求；
- `pi-agent-core` + `pi-ai`；
- 单卡、单 session、单叙事 Agent；
- 先接当前 DeepSeek provider 路径；
- 继续复用 Python engine/handler 作为 commit adapter；
- 不使用 Pi coding CLI；
- 不启用多 Agent；
- 不开放通用 Bash 写状态。

建议首批工具：

```text
get_session_snapshot(sessionId, revision)
get_recent_memory(sessionId, maxChars)
load_worldbook_entry(sessionId, title)
validate_state_proposal(proposal)
commit_turn_draft(draft, expectedRevision)
```

`commit_turn_draft` 是唯一写入工具。

### Phase 2：替换输入等待和文件信号

- `/api/submit` 追加 `user.message` event；
- dispatcher 创建 `narrative.turn` task；
- SSE/WebSocket 推送 queued/running/preview/tool/final/error；
- 增加 idempotency key、session lock 和 optimistic revision；
- stop 映射 `Agent.abort()`；
- reroll/rollback 映射 revision/branch task；
- 删除对 `.pending`、`input.txt` 和 `/api/wait_pending` 的功能依赖。

### Phase 3：显式 ContextCompiler 与 telemetry

- 将 `round_prepare.py` 逻辑迁移为可调用 ContextCompiler；
- 输出结构化 ContextManifest；
- 每次调用显式应用窗口、摘要、worldbook 和 tool-result retention；
- provider-reported token usage 直接取 Pi AI result；cost 使用锁定费率版本的 Pi catalog estimate，并保留后续账单核对 seam；
- 记录 inclusion reason、裁剪项和版本/hash。

### Phase 4：Typed domain protocol

- `response.txt` 先作为兼容 adapter；
- Agent 改为提交结构化 `NarrativeTurnDraft`；
- 将 handler orchestration 拆成 parser adapter、MVU validation、turn commit 和 render projection；
- 消除 session state 与全局 `styles/` 产物的事实源混淆。

### Phase 5：多 Agent 与资料来源

- 世界模拟成为独立 task role；
- 叙事导演消费批准后的 proposal；
- 文风/角色 Agent 作为 draft review；
- Web Search 产生包含查询意图、URL、检索时间、摘要、可信度和不确定性的 `SourceRecord`；
- 玩家采纳、拒绝或编辑候选资料时追加明确 adoption event；
- 建立 role budget、trace 和成本限制。

---

## 10. 原型验收门槛

正式选型采用两步 ADR：原型前的实验 ADR 只授权候选验证并固定协议边界；只有以下条件通过后，才以新的 Accepted ADR 确认或否决 Pi 作为正式 execution layer：

1. 回合不再使用 `ScheduleWakeup`；
2. 回合不再调用 `/api/wait_pending`；
3. 不依赖 Claude Code transcript 的任何功能性信息；
4. 每次 model call 都能保存并回放 `ContextManifest`；
5. token usage 来自 provider response；cost 明确标为使用锁定 Pi catalog rate 的估算，并可与账单核对；
6. tool call、abort、retry 和 commit 都有 trace；
7. DeepSeek 的 streaming、工具调用、abort、长输出、usage 和错误路径通过真实 E2E；
8. reroll/rollback 基于 session revision 正确工作；
9. runtime 重启后，未完成 task 有明确可恢复或失败状态；
10. 至少一组现有角色卡 fixture 保持导入、MVU、记忆和前端回合兼容。

通过前，Pi 的状态是 **候选**，不是 Accepted 技术选型。

---

## 11. 风险与开放问题

| 风险 | 影响 | 处理建议 |
|---|---|---|
| Pi namespace/API 仍可能演进 | 升级会影响 runtime | 锁版本；建立 AIRP Pi adapter；Pi 类型不扩散到领域层 |
| Node.js 版本要求 | 增加 TS/Node 运行环境 | 原型和 CI 固定版本 |
| DeepSeek 语义差异 | tool/stream/cache 可能不一致 | 建真实 provider contract tests |
| 继续累积 Pi messages | 上下文漂移重演 | event store 为事实源；每 run 显式 materialize context |
| Pi 无 durable queue | 重启、重试、并发无保证 | 自建 TaskStore、lease、retry、idempotency、session lock |
| Python/TypeScript 双栈 | 部署和错误传播复杂 | 原型期定义窄 adapter；原型后再决定长期语言边界 |
| 多 Agent 状态竞态 | 设定污染和覆盖 | proposal/validation/commit，单一 StateCommitter |
| 流式预览提交失败 | 玩家看到未落盘文本 | 明确 preview 与 committed event |
| 通用工具扩大攻击面 | 卡片或模型可越权 | 仅提供 typed domain tools、路径/网络 allowlist 和 revision check |
| 多 Agent 成本扩大 | token 和重试失控 | role/task/context budget 与 usage ledger |
| 自动检索污染设定 | 外部资料覆盖玩家事实 | 默认候选，记录来源，玩家采纳后才进入本地设定 |
| 全局单例遗留 | 阻碍多 session | 单独推进 session/card repository 重构 |

---

## 12. 推荐决策方向

推荐启动 Pi 最小原型，但把 Pi 定位为 **Agent Execution Layer**：

```text
AIRP owns:
  session / event / task / context / commit / observability /
  multi-agent protocol / player authority

Pi owns:
  provider abstraction / agent loop / tool lifecycle /
  streaming / abort / steering execution controls

Existing AIRP engine owns:
  card / MVU / render / worldbook / legacy compatibility
```

不推荐：

- 直接嵌入 Pi coding-agent CLI；
- 只换 Agent loop，却继续保留 `.pending` 和无限 transcript；
- 一开始就上多 Agent，而没有稳定的 context、commit 和 trace；
- 继续把生成的静态投影目录当长期 session state。

最重要的判断不是“Pi 功能是否比 Claude Code 多”，而是：**Pi 的 library seam 是否足够薄，使 AIRP 能拿回 runtime 所有权。** 从官方 API 看，答案足以支持最小原型；但 durable runtime、领域协议和 DeepSeek 兼容性仍必须由原型验证。

---

## 参考资料

### Pi 官方资料

1. [Pi 官方仓库](https://github.com/earendil-works/pi)
2. [Pi Agent Core README](https://github.com/earendil-works/pi/blob/main/packages/agent/README.md)
3. [Pi Agent Core `agent.ts`](https://github.com/earendil-works/pi/blob/main/packages/agent/src/agent.ts)
4. [Pi Agent Core `types.ts`](https://github.com/earendil-works/pi/blob/main/packages/agent/src/types.ts)
5. [Pi AI README](https://github.com/earendil-works/pi/blob/main/packages/ai/README.md)
6. [Pi AI `types.ts`](https://github.com/earendil-works/pi/blob/main/packages/ai/src/types.ts)
7. [Pi coding-agent README](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/README.md)
8. [Pi coding-agent extensions](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/extensions.md)
9. [`@earendil-works/pi-agent-core` npm metadata](https://registry.npmjs.org/@earendil-works%2Fpi-agent-core)
10. [旧 `@mariozechner/pi-agent-core` npm metadata](https://registry.npmjs.org/@mariozechner%2Fpi-agent-core)

### Anthropic 官方对照资料

1. [Claude Agent SDK Overview](https://code.claude.com/docs/en/agent-sdk/overview)
2. [Claude Agent SDK Sessions](https://code.claude.com/docs/en/agent-sdk/sessions)
3. [Claude Code context window](https://code.claude.com/docs/en/context-window)

### AIRP 本地依据

- [`CONTEXT.md`](../../CONTEXT.md)
- [当前 Runtime 与目标 Runtime](../architecture/future-runtime.md)
- [总体架构与回合数据流](../architecture/overview.md)
- [功能成熟度矩阵](../status/feature-status.md)
- [技术债与已知限制](../status/technical-debt.md)
- [路线图](../status/roadmap.md)
- [ADR-0001：Claude Code 直驱是原型](../adr/0001-claude-code-direct-drive-is-prototype.md)
- [ADR-0002：世界书 Skill 模式](../adr/0002-worldbook-skill-mode.md)
- [ADR-0003：引擎 Deep Module](../adr/0003-engine-deep-modules.md)
