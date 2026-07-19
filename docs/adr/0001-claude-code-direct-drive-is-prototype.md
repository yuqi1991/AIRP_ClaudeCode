# ADR-0001：Claude Code 直驱是原型，不是长期 Runtime

- **状态**：Accepted
- **日期**：2026-07-19

## Context

当前 RP 回合由 Claude Code session、ScheduleWakeup 和本地 `wait_pending` 长轮询驱动。它快速证明了产品流程，但 session 生命周期、追加式 transcript、上下文控制和多 agent 通信都受 Claude Code harness 限制。

## Decision

将当前 Claude Code 直驱模式定位为可运行原型。下一代优先建设独立、轻量、可控的 harness/runtime，保留卡片、世界书、MVU、前端和 engine 模块经验。

## Consequences

- 不再围绕 Claude Code loop 做长期产品设计；
- runtime 选型、上下文装配、agent 通信和工具调用需独立设计；
- 当前 pipeline 继续作为行为基准和迁移资产；
- 具体 harness（包括 Pi）尚未选定，需另立 ADR。
