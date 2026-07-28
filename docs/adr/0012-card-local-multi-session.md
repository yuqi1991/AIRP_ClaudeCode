# ADR-0012：Card-local 多 Session 与单活动投影

- **状态**：Experimental
- **日期**：2026-07-28
- **关联**：ADR-0004、ADR-0007、ADR-0008、`docs/architecture/card-worldbook-memory.md`

## Context

SQLite runtime 已能持久化一个 session 的 task、event、commit、revision 与 opening，但浏览器和启动器固定使用 `local`。`chat_log.json`、`content.js` 和 `state.js` 又是单份兼容文件；直接把它们复制成多个存档会产生多个事实源，并让恢复、回退与幂等语义漂移。

## Decision

引入 `SessionManager` 作为存档生命周期 seam：

1. 同一卡片的 session 共用 `<card>/.runtime.sqlite3`，但 opening、task、event、commit、revision、state snapshot 与幂等键均按 `session_id` 隔离。
2. `session_catalog` 只保存显示标题和时间；`runtime_metadata.active_session_id` 保存重启后恢复的活动指针。runtime 仍是各 session lineage 的权威实现。
3. 浏览器一次只操作一个活动 runtime。创建或切换后，根据目标 active lineage 重建单份 legacy projection；projection 不是事实源。
4. 生成 lease 活动时拒绝创建、切换和删除。删除清理该 session 的 durable rows；删除活动 session 时先选择并投影替代存档。最后一个 session 不可删除。
5. 新 session 从卡片默认 opening 和 revision 0 开始。卡片事实、世界书、变量基线、settings/preset 与当前长期 memory 仍共享。

## Consequences

- 调用方通过小型 manager interface 获得完整的存档生命周期；SQLite 细节、runtime 替换和投影恢复保持局部化。
- 同一幂等键可在不同 session 独立提交；重启可恢复活动 session、标题和各自 active revision。
- 单活动投影保留现有 renderer/frontend 兼容性，也意味着一个 server 进程尚不能同时展示或生成同一卡片的多个 session。
- `memory/project.md` 尚未 session-scoped。需要长期记忆分叉时，应把 memory 写入带 revision 来源的持久 store，而不是扩散新的文件副本。

## Evidence

- `skills/engine/session_manager.py`
- `skills/tests/test_session_management.py`
- `skills/tests/test_import_compatibility.py`
- `刻晴.json` 的 JSON v2 导入、双存档游玩、浏览器切换、进程重启与桌面/390px 移动端验证（2026-07-28）
