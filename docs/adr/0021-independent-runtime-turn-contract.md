# ADR-0021：独立 Runtime/Harness 的最小回合契约

- **状态**：Accepted（契约已锁定；真实 Provider 与长会话可靠性仍属后续验收）
- **日期**：2026-08-02
- **关联**：Wayfinder #16、ADR-0006、ADR-0007、ADR-0011、ADR-0014、ADR-0019

## Context

AIRP 已经有一条由 `airp.server`、`SessionCommandService`、`SessionTurnRuntime`、
`GraphRuntime` 和 `AgentExecutor` 组成的 canonical 执行路径。当前生产执行器为
`PiCoreNodeRunner`，而 `ProviderNodeRunner` 保留为测试适配器。任务、Session
revision、事件、Trace 和兼容投影进入卡片本地 SQLite/Projection；浏览器通过
Command API 和 SSE 观察这条路径。

在继续处理真实 Provider、长会话和多游戏边界前，必须明确谁拥有一次回合的生命周期。
如果浏览器、旧 file-loop、Graph 节点或模型各自保留一部分提交权，就会出现重复生成、
重复写入、无法恢复或“预览已经成功但故事没有提交”的歧义。

## Decision

### 1. Runtime/Harness 是唯一回合编排者

一次玩家回合的唯一生命周期是：

```text
submit
  -> durable TurnTask + source snapshot
  -> Context Manifest
  -> Graph/Agent + Provider/Tool execution
  -> Turn Draft parse and validation
  -> exactly one Turn Commit
  -> projections and durable events
```

浏览器、Studio 和 Monitor 只能提交命令或读取 snapshot、事件和 Trace。它们不直接写
故事状态、变量、聊天记录或兼容 projection。

### 2. Task 是唯一的回合身份

- 每个外部 mutating command 必须携带 idempotency key。
- 相同 key 的重复提交返回同一个 Task/event/commit 身份，不得启动第二次模型运行。
- 一个 Task 最多推进一个 commit 和一个 active revision；质量、MVU、provider 或投影
  失败只能产生明确的非提交终态。
- Session 以 durable generation lease 串行处理回合；stale revision 不能写入新 head。

### 3. Context 在执行前冻结

Task 创建时冻结 Project、角色卡事实、绑定 Worldbook、Graph、Agent、Provider、
工具权限、active revision 和玩家输入。Context Manifest 记录实际选择、token budget、
来源和 provenance；后续编辑只影响新 Task，不改写当前 Task 的上下文。

### 4. Provider/Tool 是受限 adapter

Provider/Tool adapter 负责协议、流式输出、取消、超时、错误分类和 telemetry，但不能
直接修改 authoritative story state。Agent 只能使用 Host 注册的 capability；模型输出的
文本和 MVU proposal 由 harness 解析、校验并提交。模型不可见、也不能调用一个“提交
回合”工具来决定引擎是否完成回合。

### 5. Commit 与投影是两个明确阶段

Commit 在 optimistic revision、质量门禁和严格 MVU/schema 校验通过后原子推进 revision。
`chat_log.json`、`state.js`、`content.js` 等是可重建 projection；只有所需 projection
完成后，任务才对 UI 呈现为最终成功。投影可重试且幂等，不能产生第二条回合记录。

### 6. 取消、事件和重启恢复属于同一契约

- queued/running Task 的取消传播到 provider/tool 执行；部分输出只留在 Trace，不能
  变成提交事实。
- 事件使用 Session 内单调 sequence，SSE 是 durable event 的投影；客户端可从最后
  一个 sequence 重连并补齐事件。
- 进程重启后，Runtime 根据 durable Task/lease/commit/projection 状态分类任务并继续
  或明确终止；不得依赖 `input.txt`、`.pending` 或进程内内存作为唯一事实。

### 7. 不做静默 legacy fallback

一个 Session 在启动时选择 canonical Runtime；canonical Task 失败不会自动切回 Claude
Code/file-loop 继续写同一份故事。回退必须是显式的 operator/session 选择，避免双写和
分叉历史。

## Contract surface

| 表面 | 事实 | 不属于表面 |
|---|---|---|
| Command API | `submit`、`cancel`、snapshot、reroll/rollback 等带幂等身份的命令 | 浏览器私有队列或旧信号文件 |
| Event/SSE | 有序、可重放的 Task/Graph/Node/preview/commit 事件 | 仅存在于进程内的进度 |
| Task | 状态、attempt、base revision、source snapshot、commit identity | 模型是否“记得”提交 |
| Context Manifest | 实际上下文、预算、来源与 Worldbook load provenance | 未选择的历史 transcript |
| Turn Commit | 唯一 authoritative revision 写入 | Provider 或模型直接改状态 |
| Projection | 可重建、可重试、与 commit 绑定 | 将 preview 当成已提交回合 |

## Scope boundary

本 ADR 不决定任意 DAG 的分支、并行、条件边、循环、合并和调度语义，也不决定多 Agent
世界模拟的职责、消息协议或状态提案/批准模型。当前 Graph 仍是线性顺序执行；这些
问题保留在 Wayfinder map 的 `Not yet specified`。

## Evidence and remaining work

当前实现和确定性测试已覆盖 command/event、幂等 Task、唯一 commit、MVU/质量门禁、
投影幂等、取消、Graph Trace 和基础重启分类。真实 Provider 跨协议、断线/代理 SSE、
长会话故障注入和浏览器可见的长期恢复按 ADR-0022 的发布 qualification 继续验收；
多游戏/存档边界由 #15 验收。
