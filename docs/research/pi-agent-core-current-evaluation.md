# Pi Agent Core 当前能力核验

- **状态**：Adopted，运行边界已落地
- **日期**：2026-08-02
- **范围**：评估并接入可嵌入的 `@earendil-works/pi-agent-core`；不使用 Pi Coding Agent CLI、Pi session 文件或内置文件写入工具。

## 结论

`@earendil-works/pi-agent-core@0.83.0` 满足一次 Agent Run 内的模型-工具-模型多轮迭代。Pi 在普通文本终止时结束本 Agent Run；AIRP 因而采用静态 Handoff 链，而不是让模型通过文本决定路由或要求 `send_message`/`finish` 工具。一个节点的普通终止输出经 Agent 绑定 Regex 处理后，与用户配置的 Handoff Prompt 组合，成为下一节点输入；最后节点的普通输出才是提交候选。

这条边界与 AIRP 的领域所有权一致：Pi 只负责短生命周期的单 Agent loop；AIRP 仍是 task、revision、存档、上下文、工具权限、trace 和玩家可见正文的唯一事实源。

## 本次实际安装与运行

初次核验在临时目录执行；当前仓库已将 core、Pi AI 与 TypeBox 固定为 `0.83.0` / `1.3.7`，生产默认执行器为 `PiCoreNodeRunner`。

```text
npm install --prefix <temporary-directory> --no-save \
  @earendil-works/pi-agent-core@0.83.0
```

本机 Node 为 `v22.22.3`。安装成功后，使用 Pi 官方 `createFauxCore()` 构造假 provider，配置一项 typed `lookup` 工具，并让 provider 依次返回“调用 lookup”与“普通文本交付”。实际结果为：2 次 provider 调用、1 次工具执行，以及 `agent_start → turn_start → tool_execution_* → turn_end → turn_start → turn_end → agent_end` 生命周期事件。接入后，`tests/test_pi_node_runner.py` 又用本地 OpenAI-compatible SSE 服务验证了真实 sidecar 路径：工具回合、流式文本、同一 Task 内同 Agent transcript 延续、Handoff 注入与 Graph Run 结束时的 worker 销毁。

## 官方事实与 AIRP 对照

| AIRP 所需能力 | Pi 当前证据 | 结论 |
| --- | --- | --- |
| 一个 Agent 的工具驱动多轮工作 | [Agent Core README 的 Tool Calls 流程](https://github.com/earendil-works/pi/blob/v0.83.0/packages/agent/README.md#with-tool-calls) 与 [agent loop 源码](https://github.com/earendil-works/pi/blob/v0.83.0/packages/agent/src/agent-loop.ts) 显示：tool result 会进入 context，并触发下一次模型调用。 | 满足。适合世界书检索、历史查询、web search 等由 AIRP 注入的工具。 |
| 流式文本、工具进度与可观测性 | [事件定义](https://github.com/earendil-works/pi/blob/v0.83.0/packages/agent/src/types.ts) 和 [README Event Flow](https://github.com/earendil-works/pi/blob/v0.83.0/packages/agent/README.md#event-flow) 提供 `message_update`、`tool_execution_start/update/end`、`turn_end`、`agent_end`。订阅者会被 awaited。 | 满足基础事件面；AIRP adapter 需将事件持久化为既有 Task/Trace/SSE，而不能把 Pi transcript 当审计库。 |
| 取消 | [Agent.abort()](https://github.com/earendil-works/pi/blob/v0.83.0/packages/agent/src/agent.ts) 取消当前 `AbortController`；工具 `execute(..., signal)` 也获得同一 signal。 | 满足接口要求；AIRP 工具和 provider adapter 必须实际遵守 signal，且 Host 仍要保存 cancelled task 状态。 |
| Provider/运行失败 | [Agent 的 failure handler](https://github.com/earendil-works/pi/blob/v0.83.0/packages/agent/src/agent.ts) 会把运行时异常转换成带 `stopReason: "error"` 或 `"aborted"` 的 assistant 事件。 | Adapter 必须把该事件明确映射为 AIRP failed/retryable/cancelled task，不能把空 assistant 文本视为有效交付。 |
| 中途纠偏与后续工作 | [steer/followUp API](https://github.com/earendil-works/pi/blob/v0.83.0/packages/agent/README.md#steering-and-follow-up) 与 [loop 的 queue 处理](https://github.com/earendil-works/pi/blob/v0.83.0/packages/agent/src/agent-loop.ts) 支持排入消息。 | 可用，但队列是单 Agent 的运行时内存，不是跨 Agent mailbox，也不是重启后可恢复队列。 |
| 普通文本后“继续直到交付” | [loop 退出路径](https://github.com/earendil-works/pi/blob/v0.83.0/packages/agent/src/agent-loop.ts) 在没有 tool call、steering 或 follow-up 时直接 `agent_end`。`continue()` 也只允许最后一条为 user/tool result，不能在普通 assistant 文本后无条件续跑。 | **不直接满足。** 不能仅靠 Prompt 获得稳定的自主反思循环。 |
| 多 Agent 协作 | Agent Core 的公开 API 是单个 `Agent` 的 transcript、工具和队列；没有 agent registry、routing、durable mailbox 或工作流完成条件。参见 [Agent API](https://github.com/earendil-works/pi/blob/v0.83.0/packages/agent/src/agent.ts)。 | AIRP 以静态 Handoff 顺序承担路由；当前不需要 Pi 扩展或 mailbox。 |
| Provider 与模型 | core package 依赖 [Pi AI](https://github.com/earendil-works/pi/blob/v0.83.0/packages/agent/package.json)，并通过必填的 [`streamFn`](https://github.com/earendil-works/pi/blob/v0.83.0/packages/agent/src/agent.ts) 接收 provider stream；Pi AI 源码包含 [OpenAI Completions](https://github.com/earendil-works/pi/blob/v0.83.0/packages/ai/src/api/openai-completions.ts) 和 [OpenAI Responses](https://github.com/earendil-works/pi/blob/v0.83.0/packages/ai/src/api/openai-responses.ts) 适配。 | 可复用 Pi AI，或由 AIRP adapter 提供自己的 `streamFn`；现有 Provider Profile、密钥库、失败分类和 usage/cost trace 不应让渡给 Pi。 |

## 依赖与兼容性

- npm 当前 latest 是 `0.83.0`，`engines.node` 为 `>=22.19.0`；Node 20 用户必须使用 npm dist-tag `legacy-node20` 的 `0.74.2`，不能混装 latest。来源：[官方 npm 元数据](https://registry.npmjs.org/@earendil-works%2Fpi-agent-core)。
- core 会引入 `@earendil-works/pi-ai`、`typebox` 等依赖；若进行原型，应将 core 与 Pi AI 都锁定为同一精确版本 `0.83.0`，不要使用浮动 `^`。
- 当前 AIRP 的 Node `v22.22.3` 可以运行 latest；这只说明开发环境兼容，不构成所有部署环境兼容证明。
- 该包是 ESM TypeScript/JavaScript 库，而 AIRP 的生产 runtime 是 Python。因此不能在 Python 中直接 import；正式接入仍需要 Node worker/sidecar 或明确的 RPC adapter。这是进程边界成本，不是 Pi 自动提供的持久化能力。

## 已采纳的接入边界

不要把 Pi CLI 或 Pi 的 JSONL session/harness 当 AIRP session。以一个 AIRP-owned adapter 创建一次性 `AgentRun`：

```text
AIRP durable task + frozen context + static Handoff chain + tool allowlist
  → Pi Agent（单 Agent、多轮工具 loop、流事件、私有内存）
  → Regex transform + next Handoff or final candidate
  → AIRP Trace/SSE + exactly one revision proposal
```

Agent Prompt 可自由定义身份、协作和交付标准；Handoff Prompt 可自由定义下一节点应如何解释上游结果。框架只实施用户保存的节点顺序、固定循环次数、工具 allowlist、Regex 与最终提交边界，不注册角色语义、邮件箱、`finish` 工具或质量门。

Pi 的低层 [`agentLoop`](https://github.com/earendil-works/pi/blob/v0.83.0/packages/agent/src/agent-loop.ts) 继续条件仍是工具、steering 或 follow-up；AIRP 当前有意只采用工具驱动的单 Agent 多轮，普通文本表示该站完成并交接。

## 后续验证

1. 用真实受支持 Provider 完成发布 qualification，比较 Pi AI 的跨协议 payload、SSE 与 usage/cost。
2. 覆盖 abort、provider 断流、工具忽略 abort、超预算与 sidecar 意外退出。
3. 在未来动态路由需求出现时，先独立设计受限的最大次数/条件协议；不要绕过静态 Handoff 边界直接引入 mailbox。
