# Spec: Task Working Snapshot 与 Agent 工具

## Status

Planned. 本规格定义正常 Task 的目标提交模型，取代“从最终正文解析状态命令”的运行路径。
它不授权在此阶段重做默认协作套件的安装/Project 生命周期 implementation。

## Problem

当前正常 Graph Run 将最终 Artifact 包装为 `TurnDraft`，再从 `TurnDraft.content`
或 `mvu_commands` 读取 MVU。投影重建也可能再次解释这些内容。正文因此既是玩家可见
文本，又是 State 写入协议。

这与以下已确认事实冲突：

- 最终交付 Artifact 是 opaque content；
- 写入权来自用户配置的 Agent 工具，而不是 Prompt、Handoff 或正文标签；
- 一个 Task 成功时原子提交最终内容和 Memory/State Snapshot；
- Graph 只调度普通 Agent，不拥有 Memory/State 业务规则。

## Goals

1. 每个 Task 拥有从 base revision 复制出的单一 Task Working Snapshot。
2. Agent 只能通过 Studio 中可见且已获 allowlist 授权的工具读写该 Snapshot。
3. 只有最终交付节点经过其 Regex Collection 后的 Artifact 可以成为正式正文。
4. Graph 成功时，正文、Memory Snapshot 和 State Snapshot 原子形成一个 Turn Commit。
5. 失败、取消、stale、retry 或进程退出不留下部分 Memory/State 写入。
6. 新正文、Opening、Prompt、Handoff 和 projection 不再解释或隐藏 MVU。
7. 旧存档仍可读取；旧 MVU 仅保留给显式兼容导入。

## Non-goals

- 不做内容质量判断、非空正文门禁、模型 fallback 或自动修复。
- 不让模型调用 `commit_turn`，也不让任意节点直接写 Session DB、projection 或文件。
- 不提供 Memory 索引、查询语言、框架截断上限、分页或用户不可见的摘要策略。
- 不定义 Agent 角色、Graph 内置 Memory Agent、世界模拟、DAG、并发节点或条件边。
- 不迁移或修复已物化的默认协作套件；它们仍是普通用户对象。
- 不在本规格中收缩 Default Collaboration Suite 的安装事务和 Project lifecycle implementation；
  那是独立的过度设计收敛决策。

## Domain Model

### Task Context Snapshot

Task 创建时冻结 base revision、玩家输入、Project/Library/Graph/Agent/Provider 配置与
只读上下文来源。它不可修改。

参数宏和历史读取必须固定在该 Task 的 `session_id + base_revision` active lineage。
已开始的 Task 不因 Studio 保存、rollback、reroll 或其他 Session 的变化而改变。

### Task Working Snapshot

Task 从 base revision 的完整 Memory Snapshot 和 State Snapshot 复制一个临时工作副本：

```text
Task Context Snapshot          Task Working Snapshot
---------------------          ---------------------
frozen inputs/config           mutable memory entries
frozen base revision           mutable state document
read-only history lineage      no durable identity
```

它只在本次 Task 内存在。Graph 严格串行执行，因此获授权工具按调用顺序直接读取或原子
修改同一副本；不引入 Effect、Buffer、Overlay、proposal/approval 或第二套暂存对象。

### Session Commit

Session Commit 是 active lineage 上的权威事实节点。

- **Turn Commit**：正式玩家回合；携带最终正文、完整 Memory Snapshot 与完整 State Snapshot。
- **memory_edit / state_edit Commit**：玩家在 Studio 执行显式存档命令时产生；没有正文，
  不进入 `recent_turns`。

每个 Commit 都带 expected Session revision 与 parent revision。rollback 直接选择历史
Snapshot；reroll 从被替换故事回合的 parent revision 派生。不得通过重放正文、Prompt、
Handoff 或工具调用重建状态。

## Context And Read Tools

### `{{recent_turns:N}}`

`N` 是正整数参数，按故事回合计数，不按 Session revision 计数。结果按时间顺序展开为
最基础的 LLM messages：

```json
[
  {"role": "assistant", "content": "opening"},
  {"role": "user", "content": "player input"},
  {"role": "assistant", "content": "committed content"}
]
```

- Opening 是第一条 assistant story message。
- 正式回合通常展开为 user 与 assistant 两条 message。
- `memory_edit` 与 `state_edit` 不计入 N。
- 不设框架截断、token cap 或静默裁剪；用户通过宏参数和 Agent Prompt 管理成本。

旧无参数 `{{recent_turns}}`、固定三回合和文件尾部近期记忆不再是正常 Task 的隐式来源。

### `get_turns`

`get_turns({"revision": number})` 只读取 frozen lineage 上的一个精确故事 revision，返回
同样的 `{role, content}` messages。目标不是故事 revision 时返回稳定错误；不提供范围、
分页或查询语言。

### `get_memory`

`get_memory({"entry_ids": ["uuid"]})` 返回指定 Memory entries；省略 `entry_ids` 时返回
当前 Task Working Snapshot 的全部 entries。每条 entry 是：

```json
{"id": "uuid", "title": "可重名标题", "tags": ["string"], "content": ""}
```

不设框架大小限制。如何拆分条目、何时读取全部记忆、如何控制模型上下文，是用户配置和
Agent Prompt 的责任。

## Write Tools

Host 注册公开工具 schema；Agent Definition 的 `tool_allowlist` 是唯一授权来源。Graph
Node 不能覆盖、增加或按角色隐式取得工具权限。

| Tool | 作用 | 没有授权时 |
|---|---|---|
| `update_memory` | 对 Working Snapshot 的 entry ID 执行 upsert 或 delete | 稳定未授权错误，不修改 Snapshot |
| `update_state` | 对 Working Snapshot 执行 `set`、`add`、`delete`、`insert` 或 `move` | 稳定未授权错误，不修改 Snapshot |

`update_state` 可选 `mvu` 字段。它只把显式 MVU 兼容输入规范化为上述 JSON operations，
再在冻结 schema/wildcard 上校验并原子应用。MVU 不再从最终正文或任何非工具文本提取。

工具参数不合法、schema 拒绝或工具调用失败时，Pi Agent Core 按其正常多步工具循环获得
结构化错误。Host 忠实写入 Trace；失败调用不改变 Working Snapshot。是否继续、改写参数
或让哪个 Agent 承担 Memory/State 更新，是用户的 Graph 与 Prompt 选择。

## Turn Lifecycle

```text
submit
  -> freeze Task Context Snapshot + clone Task Working Snapshot
  -> linear Graph / Agent / Tool loop
  -> final delivery Artifact after the delivery Agent's Regex Collection
  -> atomic Turn Commit(content + Memory Snapshot + State Snapshot)
  -> rebuild compatibility projection from Commit facts
```

1. 仅 output node 的终止 Artifact 是候选正文；中间 Artifact 永不提交。
2. 空字符串或仅空白的最终正文仍是合法 Turn Commit。框架不评价文笔或剧情逻辑。
3. 任一节点失败、取消、stale、retry、进程退出或原子提交失败，Working Snapshot 整体丢弃。
4. 成功后，投影仅从 committed content 和对应 Snapshot 生成；投影失败不创建第二个 Commit。
5. 每个 Task 最多产生一个 Turn Commit。没有模型可见的提交工具。

## Opaque Content Cutover

新正常路径中，下列内容一律是 opaque text：最终正文、Opening、Prompt、Handoff、
Agent Artifact、Projection 重建输入。

- 不扫描 `<UpdateVariable>`、`_.set(...)` 或其他 MVU 文本；
- 不从正文移除 MVU 片段；
- 不执行历史正文中的 MVU；
- 不因文本内容修改 State Snapshot。

`TurnDraft` 兼容 decoder 可以继续读取旧存档的 `content`、`summary`、`options`、
`polished_input` 与 `mvu_commands` 字段，但新正常运行与 projection 必须把
`mvu_commands` 视为 inert data。

`airp.compat.legacy_turn_import` 是唯一可在一次性旧聊天记录导入中使用 MVU parser 的
adapter：仅当旧记录没有已保存 State Snapshot 时，才在导入事务中补齐历史 Snapshot。导入
完成后，日常 rollback、启动、切换和 projection 不得再走该 parser。

## Trace And Debugging

既有 Node Detail 和 Tool Call Trace 是调试 interface，不新增 Working Snapshot 调试模块。

- 每次写工具调用记录安全的 operation、目标、成功/失败和 before/after diff；
- Trace 继续脱敏 Provider secret、Authorization 与其他 Secret；
- Task 终态与 Commit identity 已能表达 Working Snapshot 是 committed 还是 discarded；
- 默认 Monitor 保持轻量，完整输入、输出与工具结果按需从 Node Detail 读取。

## Default Suite Compatibility

默认协作套件是已物化的普通 Library 对象：完整 receipt 后不检查、不覆盖、不修复、不复活。

本规格实现时：

- 已安装 Workspace 不迁移其 Agent、Regex 或 Graph；
- 新 recipe 只影响尚未完成一次性安装的 Workspace；
- recipe 是否使用新宏/工具，由用户显式编辑配置决定；引擎不为旧 recipe 设置 fallback。

## Acceptance Criteria

1. 正常 Task 的最终正文包含 MVU-like text 时，正文原样提交，State 不因该文本变化。
2. 只有获授权 Agent 的 `update_memory` / `update_state` 能改变 Working Snapshot。
3. 一次成功 Task 原子提交最终正文与两类完整 Snapshot；失败/取消/retry 不留下其中任一部分。
4. `{{recent_turns:N}}` 只含 frozen active-lineage story messages，并保留空白 assistant content。
5. `get_turns` 不能越过 frozen lineage；`get_memory` 读取本 Task 的工作视图。
6. 同一 Task 中前一 Agent 对 Memory/State 的成功写入，对后一获授权 Agent 可见。
7. Studio 保存工具权限、Prompt preview 与 Execution Plan provenance 一致；Graph 不能覆盖权限。
8. rollback/reroll、启动恢复和 projection rebuild 不重演历史正文 MVU。
9. Node Detail 显示写工具的安全 diff、错误与最终 Task/Commit 状态；没有 secret 泄漏。
10. 旧 SQLite/聊天记录仍可读；唯一的兼容 MVU 执行只发生在一次性 legacy import。

## Implementation Slices

1. **Paired commit cutover**：持久化 Memory/State Snapshot，并同时停止正常 `TurnDraft`
   正文 MVU 解析与 projection 重演。它们不得作为可独立发布的两半；保留 legacy
   decoder/import，并用新旧存档 fixture 锁定。
2. **Context selection**：实现 `{{recent_turns:N}}`、`get_turns`、`get_memory`，删除固定三回合、
   固定截断与文件尾部隐式注入。
3. **Write capabilities**：注册 `update_memory` / `update_state`、schema 校验、allowlist、
   Pi 工具循环、Trace diff 与 Studio 可见选择。
4. **Recipe and acceptance**：仅为未安装 Workspace 更新示例 recipe；完成 deterministic、
   browser 和 Pi 工具循环差量验收。

每个 slice 只跨这个规格定义的 module interface。Default Collaboration Suite 特权控制面的
收敛另行决定，不能借这些切换扩张 scope。
