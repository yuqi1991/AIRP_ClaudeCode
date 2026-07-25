# ADR-0005：叙事导演执行层以 Python + FakeProvider 先行，Pi Node sidecar 延后

- **状态**：Experimental
- **日期**：2026-07-23
- **关联**：ADR-0004、Ticket 03（`.scratch/pi-runtime/issues/03-pi-narrative-director.md`）、`docs/specs/pi-agent-core-runtime.md`

## Context

Ticket 03「接入 Pi 叙事导演执行层」要求用一个真实 Agent loop（typed tools、流式预览、abort、provider telemetry）替代 Ticket 01/02 的确定性 fake executor。但 Pi 是纯 Node.js 库（`@earendil-works/pi-agent-core` + `pi-ai`，要求 Node `>=22.19.0`），而 Runtime Spine（Ticket 01/02）以及整个 engine（MVU、card、render、worldbook、handler）都是 Python。

ADR-0004 是 Experimental，**没有**固定长期语言边界；研究文档（`docs/research/pi-runtime-evaluation.md`）也明确把 Python/TypeScript 长期边界定为「原型后再决定」。因此 Ticket 03 必须先选择 Pi 的集成形态，该选择决定整票的产物形状，且「引入 Node sidecar / 全栈迁 TypeScript」属于较难回退的工具链决策。

考虑过的选项：

| 选项 | 内容 | 代价 |
|---|---|---|
| A | 在 Python 内把叙事导演执行层做实，用确定性 FakeProvider 覆盖契约；真实 Pi 包 + Node sidecar + DeepSeek E2E 拆为后续 sub-ticket | 不引入新工具链、完全可测、可逆；保住 Ticket 01/02 |
| B | 现在就加 `package.json`、锁 Pi 包、起 Node sidecar，Python executor 桥接 | 引入第二语言/第二进程/npm 依赖/IPC；更难回退 |
| C | 把 Runtime Spine 迁到 TypeScript，Python engine 经子进程 adapter 调用 | 丢弃/并行重写 Ticket 01/02；范围最大 |

## Decision

> **⚠️ Superseded in part by ADR-0011**：`commit_turn_draft` 与 `validate_state_proposal` 不再是模型可见工具。commit 成为 harness 动作（harness 从模型叙事文本解析后提交），模型工具面收窄为 read-only。本 ADR 的其余内容（FakeProvider、ProviderAdapter seam、telemetry、RealProviderAdapter 延后）仍然成立。

采用 **选项 A**。本票在 Python 内交付叙事导演执行层，配合确定性 `FakeProvider`，并保留一个窄而干净的 `ProviderAdapter` seam，使真实 Pi/Node 桥接可在后续 sub-ticket 落地而无需重写契约。

AIRP 在本票中拥有并交付（Ticket 03 验收条款 2–6）：

- `DirectorHandle` / 封闭 typed tool 注册表：`get_session_snapshot`、`get_recent_memory`、`load_worldbook_entry`、`validate_state_proposal`、`commit_turn_draft`（**唯一写工具**）。无 generic Bash、无任意文件写、无网络工具。
- 结构化 `TurnDraft`（`polished_input` / `content` / `summary` / `options` / `mvu_commands`）；MVU 复用 `engine.mvu`，不另立语义。
- 单一写入边界：只有 `commit_turn_draft` 推进 authoritative state，并在写前校验 `expected_revision`（optimistic）；同一 task 重复 commit 返回既有结果。
- 流式 `narrative.preview.delta` 预览事件；abort 通过 `AbortSignal` 协作传播；commit 后的 abort 不撤销已落盘回合。
- 每次 model call 记录 provider-reported usage、AIRP 自测 latency、stop_reason、带 `rate_version` 的 cost estimate；secret 在 events/manifests/tool traces/projections/usage 中一律不出。
- `ProviderAdapter` 为领域层唯一依赖面；`RealProviderAdapter` 抛 `NotImplementedError`，作为 Node/Pi sidecar 的文档化挂载点。领域代码不依赖 Pi 类型。

Ticket 03 条款 1（使用维护中的 Pi library 包）通过建立该 adapter seam 满足，真实包接入延后。

## Consequences

- 暂不引入新工具链（无 npm / Node sidecar），本票可逆。
- Ticket 01/02 的 Python runtime 与 legacy `FakeNarrativeExecutor` 分支保留；原 21 个契约测试与新增 22 个叙事导演契约测试（共 43）同处一套 Session Turn Runtime Contract。
- 真实 Pi/DeepSeek 验证——规格中的最终选型门槛——仍待完成。Pi 仍是候选，未 Accepted。
- 完整 lease / 串行命令流 / 崩溃恢复属于 Ticket 04（session event stream）与 Ticket 07（recovery）。本票交付的 abort 契约是 tracer-bullet 级别：在 task 翻转为 `running` **之前**注册 `AbortSignal`、用 `WHERE status='queued'` 守卫 queued→running 迁移、进入 director 前短路取消，足以覆盖 `stop()` 与 submit 重叠的丢取消竞态；更严格的 lease/并发语义留待后续。
- 已知非阻断限制：
  - `_collect_changed_paths` 只递归 dict、不比较 list 元素，故数组型 state proposal 的 `touched_paths` 不完整（`validate_state_proposal` 是非权威 dry-run，commit 路径仍走 `extract_commands`，不影响落盘正确性）。
  - 重试耗尽的 retryable provider error 落到 `failed_retryable` 终态，等待 Ticket 07 的恢复语义接手。
- 后续 sub-ticket 必须完成：新增 `package.json` + 锁定 `@earendil-works/pi-agent-core` + `pi-ai` + Node 版本；以 Node sidecar（结构化 IPC）实现 `RealProviderAdapter`；在专用测试卡上跑真实 DeepSeek E2E（中文长流式、工具调用、abort、usage、一次可见 commit）作为选型门槛。只有该门槛通过，才以新 ADR 确认或否决 Pi 为正式 execution layer。
