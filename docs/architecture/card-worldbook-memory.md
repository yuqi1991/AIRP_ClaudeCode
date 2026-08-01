# 角色卡、世界书与记忆

## 卡片目录是长期事实源

每张卡运行在独立目录中。导入源可以是 PNG、JSON 或 TXT；导入后由引擎生成卡片事实、共享基线和 card-local session store。

导入还应生成 [`airp.import-diagnostics` v1 报告](../adr/0024-import-diagnostics-report.md)，
把源字段到 AIRP 事实的转换、默认值、跳过项和失败原因保留下来。报告是导入审计投影，
不是卡片事实源；内嵌 `character_book` 的条目数量与 Project 绑定结果必须可见。

| 文件/目录 | 用途 | 生命周期 |
|---|---|---|
| `.card_data.json` | 导入后的完整卡片元数据归档 | 导入时生成，卡片专属 |
| `.session_init` | 已完成导入的标记 | 导入时生成，卡片专属 |
| `.initvar.json` | MVU 变量基线 | 导入时生成，回合读取 |
| `.runtime.sqlite3` | session catalog、活动指针、task/event/commit/revision/state lineage | 每轮事务写入，权威 session store |
| `.var_diff.json` | 当前活动 session 的最近变量变更投影 | 提交或切换时覆盖 |
| `chat_log.json` | 当前活动 session 的可见 lineage 投影 | 提交、回退、重roll、切换或启动时重建 |
| `memory/reference.md` | 世界书条目原文，按 `## 标题` 分节 | 导入时生成，按需读取 |
| `memory/.worldbook_index.json` | catalog：`title/section/usage` | 导入时生成，usage 可由 agent 回写 |
| `memory/project.md` | 卡片级共享的近期剧情摘要 | 每轮追加；尚未按 session 隔离 |
| `memory/story_plan.md` | 中期剧情规划 | 周期性更新 |
| `memory/feedback.md` | 用户偏好和边界 | 偶发更新 |

## 世界书按需 capability

世界书条目不再每轮全文注入。导入时，agent 为每条条目生成 usage：

```text
标题 — 讲什么；何时加载。
```

回合时：

1. Context Manifest 只冻结 `WORLDBOOK_CATALOG`；
2. Agent 通过 Host 注册的 exact-title capability 选择真正需要的条目；
3. capability 从 `reference.md`/绑定 Worldbook 按需读取正文；
4. 读取结果进入本次节点的可审计输入，不会隐式扫描源码目录。

这减少了动态上下文体积，并让每次加载都能在 Agent Trace 中定位来源。

普通结构化世界书条目只作为参考事实，不会被猜测为 MVU 初始变量。导入器仅接受显式 `[initvar]` / `<initvar>`、Zod prefault 或 beautify 变量宏作为变量来源。SillyTavern 标准 `{{user}}` / `{{char}}` 宏在开场交付时按当前玩家名和卡片名解析。

## Session 与兼容投影

`SessionManager` 是创建、切换、重命名和删除存档的 interface。每个 session 在 SQLite 中拥有独立 opening、task、event、commit、active revision 和 state snapshot；幂等键也以 session 为作用域。浏览器一次只激活一个 session，切换时 runtime 从该 session 的 active lineage 重建共享 `chat_log.json`、`content.js` 和 `state.js`。

因此这些 JSON/JS 文件不是多 session 的事实源。卡片事实、绑定 Worldbook、变量基线和现有 `memory/*.md` 仍为卡片级共享；需要真正分叉长期记忆时，应先把 memory 纳入 revision/session store，而不是复制投影文件。

### Project 所有权与 active projection

Workspace 的全局 Studio Library 不属于某个 Project；Project Definition 只保存规范化角色卡、
Openings、变量基线、Worldbook bindings 和 Graph selection。每个 Project 在
`runtime/projects/<project_id>` 拥有自己的 card materialization，在
`sessions/projects/<project_id>.sqlite3` 拥有自己的 Session catalog 与 runtime lineage。

一个 server 进程可以保存多个 Project 和 Session，但只激活其中一个 Project/Session。共享
的 `runtime/styles` projection、Monitor 和 Command service 始终指向当前 active surface；
切换时从 durable lineage 重建，不从旧 projection 推断事实。删除 Project 的私有 runtime
和 Session DB 必须与定义删除保持同一生命周期；当前清理实现仍是待办，详见
[ADR-0023](../adr/0023-project-owned-runtime-and-active-projection.md)。

## 记忆职责分离

- **chat log**：事实级回合记录与变量快照；用于回退和重新渲染。
- **project memory**：压缩的近期剧情连续性；用于下一轮快速理解。
- **story plan**：中期节奏、伏笔和角色弧线；不是逐回合事实源。
- **feedback**：玩家偏好与边界；影响生成策略，不改写故事事实。
- **worldbook**：相对静态的设定知识；按需加载。

未来多 agent 必须遵守这份职责分离：叙事导演不能把临时剧情直接写进静态世界书；世界模拟不能把推测当作 chat log 已发生事实。
