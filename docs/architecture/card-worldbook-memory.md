# 角色卡、世界书与记忆

## 卡片目录是长期事实源

每张卡运行在独立目录中。导入源可以是 PNG、JSON 或 TXT；导入后由引擎生成可运行的持久状态。

| 文件/目录 | 用途 | 生命周期 |
|---|---|---|
| `.card_data.json` | 导入后的完整卡片元数据归档 | 导入时生成，卡片专属 |
| `.initvar.json` | MVU 变量基线 | 导入时生成，回合读取 |
| `.var_diff.json` | 最近变量变更审计 | 每轮覆盖 |
| `chat_log.json` | 完整回合历史、变量快照、摘要和 token | 每轮追加，跨会话持久 |
| `memory/reference.md` | 世界书条目原文，按 `## 标题` 分节 | 导入时生成，按需读取 |
| `memory/.worldbook_index.json` | catalog：`title/section/usage` | 导入时生成，usage 可由 agent 回写 |
| `memory/project.md` | 近期剧情摘要 | 每轮追加 |
| `memory/story_plan.md` | 中期剧情规划 | 周期性更新 |
| `memory/feedback.md` | 用户偏好和边界 | 偶发更新 |

## 世界书 skill 模式

世界书条目不再每轮全文注入。导入时，agent 为每条条目生成 usage：

```text
标题 — 讲什么；何时加载。
```

回合时：

1. 叙事 agent 读 `WORLDBOOK_CATALOG`；
2. 对照用户输入、当前场景和剧情需求选择真正需要的条目；
3. 用完整标题从 `reference.md` 按需读取正文；
4. 读取到的正文严格约束对应主题的描写。

这减少了动态上下文体积，但当前 Claude Code transcript 仍会保存按需读取结果；独立 harness 应把上下文装配完全纳入自己的控制。

## 记忆职责分离

- **chat log**：事实级回合记录与变量快照；用于回退和重新渲染。
- **project memory**：压缩的近期剧情连续性；用于下一轮快速理解。
- **story plan**：中期节奏、伏笔和角色弧线；不是逐回合事实源。
- **feedback**：玩家偏好与边界；影响生成策略，不改写故事事实。
- **worldbook**：相对静态的设定知识；按需加载。

未来多 agent 必须遵守这份职责分离：叙事导演不能把临时剧情直接写进静态世界书；世界模拟不能把推测当作 chat log 已发生事实。
