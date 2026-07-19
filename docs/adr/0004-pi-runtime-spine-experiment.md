# ADR-0004：以 Pi Runtime Spine 验证独立执行层

- **状态**：Experimental
- **日期**：2026-07-19

## Context

Claude Code 直驱已被定位为原型，但直接采用任何替代 Agent framework 都会过早绑定产品 runtime。AIRP 首先需要收回玩家命令、任务、提交和兼容投影的控制权，并证明这些能力不依赖 Claude Code transcript、`.pending` 或 `wait_pending`。

Pi 提供候选的 Agent loop、工具生命周期和 provider abstraction；它不提供 AIRP 所需的 durable Session、Context Compiler、状态提交协议和写作团队协调模型。

## Decision

先建立单卡、单 Session、单叙事导演的 Runtime Spine：

- AIRP 持久化 Session Event、Task、revision、Turn Commit 和 projection checkpoint；
- 每个玩家命令以 idempotency key 进入唯一提交边界；
- 初期使用确定性 fake narrative executor 验证 Session Turn Runtime Contract；
- 现有卡片、MVU 和浏览器投影通过窄 Legacy Engine Adapter 兼容；
- Pi library packages 是后续接入的候选 execution dependency，不使用 Pi coding-agent CLI；
- 角色与 NPC 保持领域对象，不为每个角色创建 Agent identity。

此 ADR 只授权实验和协议边界，**不**确认 Pi 为最终 runtime 选型。

## Consequences

- 首个可验证 vertical slice 将不再以 `.pending` 或 `input.txt` 作为事实源；
- 后续 Context Manifest、Pi execution、SSE、revision reroll/rollback 和恢复能力可在同一 Session Turn Runtime Contract 上递进；
- Context Compiler 已以显式 source snapshot 编译并持久化 Manifest；当前只支持线性 revision，branch lineage 留待 reroll/rollback ticket；
- 世界书继续以 catalog + exact-title 按需读取，且每次调用的加载量受 policy 上限约束；
- 现有 `styles/` 全局单例仍是兼容投影，尚未在本切片消除；
- 只有真实 DeepSeek 和浏览器 E2E 满足规格验收门槛后，才新增 Accepted/Rejected 的最终选型 ADR。
