# ADR-0023：Project 所有运行态与单 active projection

- **状态**：Accepted（所有权、并发与删除清理均已实现；多进程并发仍不在范围内）
- **日期**：2026-08-02
- **关联**：Wayfinder #15、ADR-0012、ADR-0020、ADR-0021、`CONTEXT.md`

## Context

AIRP 同时维护全局 Studio Library、多个游戏 Project、每个 Project 的多个存档 Session，
以及为现有网页兼容而保留的 `chat_log.json`/`state.js`/`content.js` projection。若这些
表面被当成同一事实源，切换游戏、重启、回退和删除就会产生串档或残留状态。

当前实现已经将 Project 定义、Project materialization 和 Session SQLite 分开，但浏览器
仍依赖一个共享的 active projection；Project 删除也还没有完整清理私有 runtime 文件。这个
ADR 先固定事实所有权和生命周期，避免后续实现继续扩大单例边界。

## Decision

### 1. Ownership table

| 数据 | 权威所有者 | 作用域 |
|---|---|---|
| Provider/Agent/Graph/Worldbook/Regex Collection | Workspace Studio Library | 全局，可被多个 Project 引用 |
| 角色卡事实、Openings、变量基线、素材、Worldbook bindings、Graph selection | Project Definition | 一个 Project |
| card materialization | Project Runtime State | 一个 Project，供 RP host 读取 |
| task、event、commit、revision、state snapshot、idempotency、Trace | Session Runtime DB | 一个 Project 内的一个 Session |
| active Project pointer | Workspace runtime metadata | 一个进程/Workspace |
| active Session pointer | Project 的 Session catalog/DB | 一个 Project |
| `chat_log.json`、`state.js`、`content.js`、前端 snapshot | Projection | 当前 active Project + Session 的可重建视图 |

Project 和 Session 的 SQLite/JSON 状态是事实源；浏览器 projection 永远不能反向成为
回合、变量或存档事实。全局 Library 删除不随 Project 级联，引用保护规则继续有效。

### 2. 多 Project/Session 持久化，单 active 执行

- Workspace 可以持久化任意多个 Project；每个 Project 可以持久化多个 Session。
- 一个 server 进程只允许一个 active Project 和一个 active Session，因为兼容 projection、
  Monitor 和当前 Runtime 都只提供一个 active surface。
- 这不是多实例并发协议；两个 server 进程不应同时写同一个 Workspace。并行 Project 只
  代表可切换的 durable 数据，不代表同时生成。
- 切换 Project 或 Session 必须在 generation lease 空闲时进行。Runtime 先确认目标事实
  存在，再载入目标 context、更新 active pointer、从 active lineage 重建 projection，最后
  替换 Command/Monitor 绑定；失败不能改变原 active surface。

### 3. Restart and migration

启动时先校验 Workspace 的 active Project pointer，再从目标 Project 的 Session DB 恢复
`active_session_id`；任一 pointer 指向已删除或损坏对象时，选择确定性的现存 fallback，写回
pointer，并从其 active revision 重建 projection。不会从浏览器缓存、旧 `chat_log.json` 或
进程内队列推断事实，也不会隐式复制 Session。

Project copy 只复制 Project Definition 和其 Library bindings，生成新的稳定 Project ID；
不复制历史 Session、Task、Trace 或 active revision。

### 4. Delete semantics

- 删除 Project 只能在 idle 状态进行。
- 删除非 active Project 应删除其 Project Definition、card materialization、Session DB
  和相关 active/recent metadata；全局 Library 对象不级联删除。
- 删除 active Project 时，先选择一个确定性的剩余 Project 并完成 idle 切换，再删除原 Project
  的私有运行态；如果没有剩余 Project，清空 active pointer，服务进入“没有游戏”的空工作区。
- 删除 Session 只能在 idle 状态进行；删除 active Session 时先切换到另一个 Session；最后
  一个 Session 不可删除。
- 删除操作不得留下一个仍会被 `restore_*` 发现的孤儿 pointer 或 Project-owned runtime
  文件。失败时应保留原 active surface并返回可操作错误。

### 5. Editing and running isolation

Project/Library 编辑只影响后续 Task。运行中的 Task 使用 ADR-0021 定义的冻结 source
snapshot/Execution Plan；切换、删除和编辑不能修改已经开始的回合，也不能让新 Project
读取旧 Project 的 projection。

## Current implementation status

已实现并有测试证据：Workspace/Project/Session 分层、每 Project Session DB、active Project
恢复、active Session 恢复、单 active projection、生成期间拒绝切换、删除 active Project
时切换剩余 Project、最后一个 Session 保护，以及删除时清理
`runtime/projects/<project_id>`、`sessions/projects/<project_id>.sqlite3` 和
active/recent metadata。删除后的重启/孤儿状态回归位于
`tests/test_game_project_drawer.py`。

## Out of scope

- 同一 Workspace 的多进程并行写入协议；
- 多 active projection、云端同步和跨设备迁移；
- Session 级 Worldbook/Graph 覆盖；
- 任意 DAG 或多 Agent 世界模拟语义。
