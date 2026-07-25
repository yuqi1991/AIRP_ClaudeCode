# ADR-0008：Revision Branch — 全局单调 revision + parent 链 + 单 active head

- **状态**：Experimental
- **日期**：2026-07-25
- **关联**：ADR-0004、ADR-0005、ADR-0006、ADR-0007、Ticket 06（`.scratch/pi-runtime/issues/06-revision-reroll-rollback.md`）、`docs/specs/pi-agent-core-runtime.md` Decisions 8、26–28、34

## Context

Ticket 01–05 的 commit 模型是**线性**的：`revision = active_revision + 1`，上下文用 `WHERE revision <= N`。这足以支撑 submit/stop/乐观冲突，但无法表达：

1. **Reroll**：同一玩家输入、同一 parent，生成新 assistant tip，旧 tip 保留审计；
2. **Rollback**：把 active head 移到历史 revision，不删事件/commit/快照；后续 submit 从该 head 开新分支。

若用「重编号 revision」实现分支，会破坏 `state_snapshots[rev]`、`commit_for_revision`、optimistic `expected_revision` 契约。

## Decision

采用 **branch DAG，revision 全局唯一且单调**：

1. **`commits.revision`** 仍是全局 `max(revision)+1`，永不复用、永不重编号。
2. **`commits.parent_revision`** 记录本 commit 的父 revision（开局/首回合 → `0`）。普通 submit 的 parent = task 冻结的 `base_revision`。
3. **`sessions.active_revision`** 即 **active head**（对外仍叫 `active_revision()`）：新 task 的 `base_revision`、乐观提交对照、以及 active-lineage 上下文读取的根。
4. **Reroll**：
   - 读取目标 revision 的玩家原文与 `parent_revision`；
   - 用**新** idempotency key 建新 task，`base_revision = parent`；
   - 先把 active head 移到 parent（`session.head_moved`，`reason=reroll`），再生成/提交；
   - 成功后新 tip 成为 head；旧 tip 仍可 `commit_for_revision` / events / usage 审计。
5. **Rollback**：只更新 `sessions.active_revision` 并写 `session.head_moved` / `session.rolled_back`；不删 commits/events/snapshots；随后 rebuild 兼容投影（chat/state/content）为 active parent 链。
6. **Active-lineage 上下文**：`_runtime_turns` / `active_lineage_turns` 从 head **沿 parent 链** 回溯（限 N），**不再** `WHERE revision <= N`。被 supersede 的兄弟分支不进 context/manifest，但 telemetry 仍可查。
7. **乐观冲突不变**：task 冻结 `base_revision=R`，提交时要求 `active_revision == R`；reroll/rollback 挪走 head 后，在飞 task 得 `stale_revision`，无 commit、无投影。
8. **投影**：成功 commit 与 rollback 后，对 active head 做 clear-then-replay（经 `LegacyProjectionAdapter.apply`），保证 chat 每个用户位置只有一个 assistant tip。
9. **迁移**：schema v3 — `ALTER TABLE commits ADD parent_revision`；线性历史 backfill `parent = revision-1`（rev≤1 → 0）。

## Consequences

- Ticket 04 预留的 `not_implemented` / `501` 被真实 reroll/rollback 替换；SSE 线格式不变，`turn.committed` 增加 `parent_revision`。
- 线性 happy path 行为兼容：连续 submit 仍 `parent=i-1`、`active` 每次 +1（数值上等于 max+1）。
- Crash recovery / lease（Ticket 07）与真实 Pi/DeepSeek 仍未落地。
- 已知限制：单 Session tracer bullet；兼容投影仍是 rebuildable 文件面，不是多 branch 并存 UI。
- 已知限制（并发/lease）：本票**未**实现 generation lease（spec Decision 8「至多一个 narrative task 持有 Session 生成租约」）。为让 director 路径的 reroll/rollback 能与并发命令交错，`reroll`/director 在生成阶段**释放** `self._lock`。后果：reroll 先把 active head 移到 parent、新 tip 尚未 commit 时存在一个**中间态窗口**（head 已在 parent），此窗口内的 snapshot/events 读会看到该中间态；并发 submit 也可能在 reroll director 跑前插入新 task，导致 provider 浪费与一方 `stale_revision`。**commit 边界仍唯一且原子**（`_commit_via_tool` 在 `BEGIN IMMEDIATE` 内做 optimistic + per-task 幂等），故最坏情况是 stale + 浪费，**不会**重复 commit / 部分 commit / 数据损坏。真正的 lease/串行化与崩溃恢复分类属 Ticket 07。
