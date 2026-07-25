# ADR-0007：Session 事件流与 Command API 并行于 legacy bridge

- **状态**：Experimental
- **日期**：2026-07-25
- **关联**：ADR-0004、ADR-0005、ADR-0006、Ticket 04（`.scratch/pi-runtime/issues/04-session-event-stream.md`）、`docs/specs/pi-agent-core-runtime.md`

## Context

Ticket 01–03/05 已在 Python Runtime Spine 上交付 durable events、task、唯一 commit、director 与质量门禁。玩家入口仍是 Claude Code 的 `ScheduleWakeup` + `skills/server.py`（`input.txt` / `.pending` / `/api/wait_pending`）。Ticket 04 要求浏览器通过 command API 提交/停止，并通过 SSE 从 sequence 续接任务状态与正式回合事件，且完整路径不依赖 legacy pending loop。

`skills/server.py` 仍是线上兼容前端的入口，并可能含本地未提交改动；将新 transport 写进该文件会混合两套权威生命周期，并增加回归面。

## Decision

1. **库层唯一写入口**：新增 `engine/commands.py` 的 `SessionCommandService`，对外部暴露 `submit` / `cancel` / `snapshot` / `events_after`；`reroll` / `rollback` 预留稳定 `not_implemented` / `deferred_to_ticket_06`，不发明分支语义。
2. **并行 HTTP 服务**：新增 `skills/runtime_server.py`（stdlib `ThreadingHTTPServer` + SSE），**不** import 或 patch `skills/server.py`。legacy bridge 与 new runtime path 并存；迁移由 Session/服务启动的 feature 控制（规格 Decision 37），本票只交付 new path 的 tracer bullet。
3. **SSE 是 durable event 的投影**：`id` = 单调 `sequence`，`event` = runtime type，`data` = JSON（含 sequence/type）。连接时回放 `sequence > after`，再 live-tail；重连至少一次补齐 gap，客户端按 sequence 去重。
4. **submit 非阻塞 SSE**：HTTP `submit` 在后台线程跑 `service.submit`，请求线程只等到 durable task 行出现即返回 `202`，以便 SSE 在 director 运行期间推送 `narrative.preview.delta`。
5. **不触碰 legacy 信号文件**：new path 禁止写入 `input.txt` / `.pending`，禁止调用 `/api/wait_pending`。

## Consequences

- 浏览器/集成测试可黑盒驱动 command + SSE，而不启动 Claude Code loop。
- Ticket 06 可在 command service 上实现真实 reroll/rollback，而不改 SSE 线格式。
- Ticket 07 的 crash recovery / lease 仍可挂在同一 durable events/tasks 上；本票不分类 abandoned leased/running。
- 真实 Pi/Node sidecar 与 DeepSeek E2E 仍属后续门槛（ADR-0005）；本票 SSE 契约以 FakeProvider/ScriptedDirector 为充分条件。
