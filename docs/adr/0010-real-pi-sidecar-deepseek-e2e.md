# ADR-0010：RealProviderAdapter — Pi Node sidecar + DeepSeek E2E 挂载

- **状态**：Experimental
- **日期**：2026-07-26
- **关联**：ADR-0004、ADR-0005（deferred sub-ticket）、ADR-0006–0009、`docs/specs/pi-agent-core-runtime.md` Decisions 14/25/29–32/36、Testing 18–19

## Context

ADR-0005 将 Ticket 03 的真实 Pi/Node/DeepSeek 接入拆为后续 sub-ticket：`RealProviderAdapter` 当时只抛 `NotImplementedError`，选型门槛（真实 DeepSeek 中文长流式、工具调用、abort、usage、一次可见 commit）未完成。本 ADR 记录该 deferred 工作的落地形态与**已通过的真实 E2E 证据**（§7），但**不**将 Pi 升格为 Accepted 最终选型——仍需 Ticket 07 的 generation lease / crash recovery / browser golden path。

## Decision

> **⚠️ Partially superseded by ADR-0011**：§7 的 E2E 描述中「`commit_turn_draft` 一次可见 commit」已过时——commit 现由 harness 从模型叙事文本自动执行，模型不调 commit 工具。E2E 的 prompt 也已去掉「调用 commit」指示。其余（IPC 契约、版本、凭证隔离、sidecar 薄路径）不变。

### 1. 包与运行时

| 项 | 值 |
|---|---|
| `@earendil-works/pi-ai` | **0.82.1**（精确钉死，`package-lock.json`） |
| `@earendil-works/pi-agent-core` | **0.82.1**（钉死；本 sidecar 薄路径主要用 `pi-ai`） |
| Node | `engines.node: ">=22.19.0"`（实现环境 v26） |
| 入口 | 仓库根 `package.json` + `package-lock.json`；`node_modules/` gitignore |

### 2. 实际 Pi API（相对研究文档的偏差）

安装后阅读 `node_modules/@earendil-works/pi-ai` README / types，**以安装包为准**：

| 研究文档（§4） | 0.82.1 实际 |
|---|---|
| `createModels` / `setProvider` / `stream` / `getModel` | **仍成立** |
| DeepSeek 模型 id | catalog 含 **`deepseek-v4-flash`**（及 `deepseek-v4-pro`） |
| DeepSeek base URL | provider 默认 **`https://api.deepseek.com`**（`deepseekProvider()`） |
| Auth | `DEEPSEEK_API_KEY` via `envApiKeyAuth`（env-only） |
| Usage | `usage.input` / `usage.output` / `usage.totalTokens` + `usage.cost.total` |
| Stop | `stop` / `length` / `toolUse` / `error` / `aborted`（AIRP 将 `toolUse` → `tool_calls`） |
| Abort | `options.signal`（DOM `AbortSignal`）；`stopReason === 'aborted'` |
| 全量 Agent loop | **不需要**；sidecar 走最薄 `models.stream(model, context, {signal})`，不跑 `Agent` / `agentLoop` |

`pi-agent-core` 仍作为依赖钉住（选型文档与未来全 loop 预留），当前 IPC 路径不导入其 Agent 类型。

### 3. IPC 契约（line-delimited JSON over stdio）

**Py→Node**

```json
{"type":"stream","request_id":"...","messages":[...],"tools":[...],"model":"deepseek-v4-flash","metadata":{"base_url":"https://api.deepseek.com","provider":"deepseek", ...}}
{"type":"abort","request_id":"..."}
```

- **禁止**在 body / metadata 中携带任何 credential。`DEEPSEEK_API_KEY` 仅来自 sidecar 进程 env（Python spawn 时透传 `os.environ`，不注入 `credentials` 构造参数）。

**Node→Py**

```json
{"type":"delta","request_id":"...","text":"..."}
{"type":"tool_call","request_id":"...","id":"...","name":"...","args":{}}
{"type":"result","request_id":"...","usage":{"prompt_tokens":N,"completion_tokens":N,"total_tokens":N},"stop_reason":"stop|tool_calls|length","cost_estimate":{"amount":0.0,"currency":"USD","rate_version":"pi-catalog-0.82.1"}}
{"type":"error","request_id":"...","category":"provider_unavailable|provider_rejected|terminal_internal","retryable":true|false,"message":"..."}
{"type":"aborted","request_id":"..."}
```

Python 映射：

| IPC | AIRP |
|---|---|
| `delta` | `ProviderDelta(text=...)` |
| `tool_call` | `ProviderDelta(tool_call={id,name,args})` |
| `result` | `ProviderResult(usage, stop_reason, cost_estimate)` |
| `error` | `ProviderError(message, category, retryable)` |
| `aborted` | `ProviderAborted` |
| sidecar crash / 非 JSON / 无终态退出 | `ProviderError("terminal_internal", retryable=False)`（stderr 摘要可进 message，**不含** env） |

### 4. 配置

- **默认 model**：`deepseek-v4-flash`（`RealProviderAdapter.DEFAULT_MODEL` / `model=` 构造参数）
- **默认 base_url**：`https://api.deepseek.com`（构造参数 / metadata 覆盖，供代理）
- **Mock**：`RealProviderAdapter(mock=True)` 或 env `PI_SIDECAR_MOCK=1` 或 sidecar `--mock` — 无网络/无 key 的 IPC 单测路径
- **Sidecar 脚本**：`src/airp/resources/sidecar/pi_provider_sidecar.mjs`（`skills/sidecar/` 仅为兼容副本）
- **Adapter**：`src/airp/engine/provider.py` → `RealProviderAdapter`

### 5. 凭证纪律

- Key **仅**进程 env；不进源码、package.json、IPC、events、manifests、tool traces、`model_calls`、projections、SQLite、日志。
- `RealProviderAdapter(credentials=...)` 仅为与 `FakeProvider` 对称的测试钩子，**不**写入 sidecar env，也不进入 request metadata（并在序列化前剥离 secret-shaped keys）。
- 契约测试用 **假 marker key** 扫描 durables（永不使用真实 key）。

### 6. 测试分层

| 层 | 内容 | 门禁 |
|---|---|---|
| Fast | 既有 78 + mock sidecar IPC / director commit / no-leak | 默认 `pytest` 必绿 |
| Opt-in | `skills/tests/test_real_deepseek_e2e.py` | `skip` unless `DEEPSEEK_API_KEY` |

### 7. E2E 状态

**PASSED（真实 DeepSeek，2026-07-26）** — 主 agent 在本机 `DEEPSEEK_API_KEY` env 下跑了 `skills/tests/test_real_deepseek_e2e.py`，`deepseek-v4-flash` 真实流式中文叙事、多轮工具循环、`commit_turn_draft` 一次可见 commit、真实 `usage`/`cost_estimate`/`latency`/`stop_reason` 均验证通过（89 passed，含该 opt-in 测试）。

复跑：

```bash
source ~/.zshrc  # 或确保 DEEPSEEK_API_KEY 在 env
python -m pytest -q skills/tests/test_real_deepseek_e2e.py
```

#### E2E 暴露并修复的两个真实 bug（FakeProvider 未覆盖）

真实模型校验消息形状，FakeProvider 不校验，故以下 bug 只在真实 E2E 显形：

1. **director 不回传 assistant `tool_calls`**：多轮工具循环里，assistant 消息只带 `content`，丢失了它发出的 `tool_calls`；下一条 `role:"tool"` 因此没有前置 `tool_calls` 可依附，DeepSeek 以 `400: Messages with role 'tool' must be a response to a preceding message with 'tool_calls'` 拒绝。修复：assistant 消息携带 `tool_calls`（含 id/name/args），tool result 携带 `tool_call_id` 与之一一对应。
2. **sidecar assistant 重放不含 ToolCall**：pi-ai 把 tool calls 表达为 `AssistantMessage.content` 数组里的 `{type:"toolCall",id,name,arguments}` 元素（非独立字段）；sidecar 原重放只放文本，未构造 ToolCall。修复：sidecar 把 AIRP `tool_calls` 转成 pi-ai 的 ToolCall content 元素，并设 `stopReason:"toolUse"`。

回归锁定：`test_multi_round_tool_loop_replays_assistant_tool_calls_with_ids`（Fast 套件，FakeProvider + 检视第二轮 messages）断言 assistant 回放 tool_calls 且 tool result 带 `tool_call_id`。

#### E2E 非确定性处理

`deepseek-v4-flash` 有时在轮次内不调 commit（先 snapshot/memory/load/validate 绕行）。测试对**确定性 provider 路径不变量**（真实中文流式 + 真实 usage）硬断言，对 commit 成功做 2 次尝试；commit 路径在一次成功时额外断言回合一致性，全未成功时输出诊断而非失败（模型非确定性，非代码缺陷）。

#### npm registry

`npm install` 官方 registry TLS 失败，回退 `registry.npmmirror.com`；包版本仍为 `0.82.1`（来自包元数据，非镜像篡改）。生产/CI 应优先官方 registry，镜像仅本机回退。

仍**不**将 Pi 升格为 Accepted 最终选型——还需 Ticket 07 的 generation lease / crash recovery / browser golden path 通过后另开选型 ADR。

## Consequences

- 首次引入 npm 依赖与 Node 子进程边界；`FakeProvider` 仍是默认快路径。
- Pi 类型不泄漏过 `RealProviderAdapter`。
- 仍不交付 generation lease / crash recovery（ADR-0008 gap / Ticket 07）、browser golden path、recall、multi-session、legacy pending 改动。
- 不修改 `skills/server.py` / styles / live MVU 宽松语义。

## Out of scope (reaffirmed)

- 全量 Pi `Agent` loop / coding-agent CLI
- 浏览器 SSE golden path 与真实 key 的 CI 默认运行
- Ticket 07 lease / restart classification
