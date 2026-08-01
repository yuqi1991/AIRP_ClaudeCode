# ADR-0026：游玩中的设定编辑与追溯语义

- **状态**：Accepted（编辑/运行隔离、revision、audit 与 provenance enforcement 已实现）
- **日期**：2026-08-02
- **关联**：Wayfinder #12、ADR-0016、ADR-0021、ADR-0023、`CONTEXT.md`

## Context

AIRP 的 Project 编辑器、Workspace Library、Session 故事状态和 Graph Runtime 现在已经
分开存储，但保存接口仍主要依靠 `updated_at`，没有 Project/Library revision、乐观冲突
检查或可查询的设定审计。`ProjectRuntimeStore.refresh()` 会立即 materialize 当前
Project，Graph 选择接口也会重新配置 Runtime；这容易让“正在运行的回合使用什么配置”与
“下一回合使用什么配置”混淆。

ADR-0021 已规定 Task 创建时冻结角色卡、Worldbook、Graph、Agent、Provider、工具和
active revision 的 source snapshot/Execution Plan。#12 需要把 Studio 的保存、未保存表单、
冲突、回退和来源追踪接到这条冻结边界上，同时遵守用户已经确认的简单交互：未保存内容在
切换时直接丢弃，不建立复杂的草稿恢复系统。

## Decision

### 1. 三种 revision 不混用

| revision | 权威所有者 | 记录什么 | 是否推进 Session 故事历史 |
|---|---|---|---|
| Project Revision | 一个 Project | 角色卡事实、Openings、变量基线、素材、Worldbook bindings | 否 |
| Library Revision | 一个 Provider/Agent/Graph/Worldbook/Regex Collection | 可复用配置或世界书正文的变更 | 否 |
| Session Revision | 一个存档 Session | 玩家回合、AI 文本、MVU 状态、投影和分支 head | 是 |

Project/Library revision 是配置事实；Session revision 是故事事实。配置保存不能伪造一
条玩家回合，也不能因为角色卡字段变化而改写既有 chat、变量或 Trace。

### 2. 编辑类型与生效时机

- 角色名、描述、personality、scenario、card prompt、Opening 模板和 Worldbook bindings
  保存为新的 Project Revision，影响下一个新建 Task。
- Worldbook 正文/条目、Agent instruction、Graph 拓扑/节点配置、Provider Profile、
  Regex Collection 保存为各自 Library Revision；绑定该对象的 Project 在下一个 Task
  读取新版本。未开始的下一回合不需要手工重启服务。
- Project 的 Graph selection 是 Project 的运行配置指针；保存后成为下一个 Task 的选择，
  不替换已经创建或正在执行的 Graph Run。
- 变量基线属于 Project Revision，只用于新的 Session 或显式的 Session reset/opening
  初始化；编辑基线不能直接覆盖当前 Session 的 `current_state`。当前 Session 变量只能
  通过受 Runtime 控制的状态命令/回合提交改变。
- Opening 模板的编辑不改写已经保存的 opening turn。切换 Opening 只允许在 Session 尚未
  出现玩家回合时进行，并记录 `session.opening_switched`；已有玩家历史需要新建 Session
  或显式分支，不通过“保存角色卡”静默重写。

### 3. Task 冻结与运行中编辑

- 用户点击保存时，配置可以立即写入 Project/Library 的事实源；未保存的浏览器表单只在
  当前抽屉内存在，切换 Project、关闭抽屉或刷新时直接丢弃。
- 保存发生在 Graph Run 期间时，不阻塞配置编辑，但不得重新编译或替换当前 Task 的
  Execution Plan、Provider、工具、Worldbook snapshot 或 Monitor Trace。运行中的 Task
  继续使用创建时的 source snapshot；保存后的配置从下一个 Task 起生效。
- Runtime 在 generation lease 空闲后再刷新 active projection/缓存。若实现需要延迟
  materialization 或 Graph rebind，延迟是可见的“下一回合生效”，不是隐式修改当前回合。
- Task、Context Manifest、Node Run 和 Trace 必须记录 Project/Library revision id 与
  content hash；旧 Trace/replay 永远读取保存的 snapshot，而不是当前 Library 的内容。

### 4. 未保存内容与冲突

- 客户端 draft 不是事实源，也不产生 revision、事件或 Session revision；当前产品不提供
  草稿跨 Project 恢复和切换前确认。
- Project/Library 更新必须带 `expected_revision`（或等价的 source version）。版本不匹配
  返回稳定 `revision_conflict` 4xx，并携带当前 revision、冲突对象和可重新加载的来源
  信息；服务不做静默 last-write-wins 或字段级猜测合并。
- 冲突只针对已经写入事实源的其它 Writer/API/进程；同一浏览器内的 draft 切换仍按上面的
  丢弃规则处理。单 active projection 不等于允许多进程同时写同一 Workspace。

### 5. 审计、恢复与回退

- 每次 Project/Library 保存追加不可变 audit record：对象类型/id、parent revision、new
  revision、时间、来源（UI/API/import/migration）、变更路径和 before/after hash。可安全
  展示的字段差异可以保留；Provider secret、capability token 和其它敏感值永不进入审计。
- 恢复旧设定是显式的“从 revision 复制并保存”，产生新的 revision；不删除或重写旧
  revision，也不改变已经完成的 Session/Trace。
- Session reroll/rollback 只操作故事 revision/head；它不会回退 Project/Library 配置。
  要验证旧配置，使用旧 Trace 的 replay；要在当前故事继续使用旧配置，显式恢复为新的
  Project/Library revision 后再发起新 Task。

## Current implementation status

已有证据：Task 创建时的 source snapshot、Context Manifest、Node Run/Trace 持久化；Project
和 Session 分层；未保存表单只存在浏览器内。Project/Library 文件现在持久化 revision，保存
支持 `expected_revision` 冲突，`.audit.jsonl` 追加对象类型、父子 revision、变更路径和
before/after hash；Execution Plan/source snapshot 注入 Project、Graph、Agent、Provider 与
Worldbook 的 revision/content hash provenance。运行中的 Task 使用已持久化的冻结 snapshot，
保存后的配置从下一 Task 生效；Session revision 仍独立。多进程同时写同一 Workspace 和
任意 DAG 运行语义仍不在本 ADR 范围内。

## Consequences

- 玩家可以在游玩时编辑设定，但不会让当前回合“半途换 prompt”或污染历史 Trace。
- 配置恢复和故事回退有清晰的不同入口，审计可解释但不会变成隐式版本管理系统。
- 运行快照增加 provenance 字段会带来少量存储开销，但可让 Monitor/Trace 证明一次 Task
  使用的配置来源；完整的配置恢复 UI 仍可在后续独立增加。
- 本 ADR 不决定任意 DAG、多 Agent 世界模拟或 Session 级 Worldbook 覆盖。
