# 总体架构与回合数据流

## 当前架构

当前系统由浏览器前端、本地 HTTP bridge、CLI 编排脚本和 `skills/engine/` 深模块组成。

```text
Browser
  │ POST /api/submit
  ▼
server.py ── input.txt/.pending ──► runtime loop (Claude Code 当前实现)
                                      │
                                      ▼
                               round_prepare.py
                                      │ round_context.txt
                                      ▼
                              Narrative agent writes response.txt
                                      │
                                      ▼
                               round_deliver.py
                         ┌────────────┼────────────┐
                         ▼            ▼            ▼
                    handler.py   write_memory   token checkpoint
                         │
                         ▼
                 chat_log/state/content → Browser
```

## 深模块

| Module | Interface | 隐藏的实现职责 |
|---|---|---|
| `engine.card` | 读取/写入卡片状态与回合 | `chat_log.json`、state.js、变量差分、回退与重roll |
| `engine.render` | 将回合/变量渲染为前端载荷 | 宏替换、beautify panel、状态条、content.js |
| `engine.mvu` | 解析、验证、执行变量命令 | JSONPatch、路径解析、schema、变量审计、checklist |
| `engine.tokens` | checkpoint 与 token delta | Claude transcript 定位、usage 解析、累计记账 |
| `engine.worldbook` | 构建世界书 catalog | usage 保留、条目索引 |
| `engine.context` | 构建启动/回合上下文 | import_context、变量路径列表、catalog 展示 |

这些模块的原则是：调用方只需知道少量 interface，文件格式、HTML、MVU 路径与 transcript 细节留在模块内部。

## 导入流程

1. `import_prepare.py` 清理旧运行态；
2. `import_card.run_import()` 解析 PNG/JSON/TXT；
3. 写入卡片目录中的卡数据、世界书正文、变量基线、开场和记忆文件；
4. 初始化当前前端 state/content 与 `.card_path`；
5. `engine.context.build_import_context()` 写入 `import_context.txt`；
6. 叙事 agent 补齐世界书 usage 后交付开场。

## 回合流程

1. 浏览器将用户输入写入 `skills/styles/input.txt`；
2. `round_prepare.py` 读取 settings、catalog、变量、近期记忆和近三轮对话，写 `round_context.txt`；
3. 当前 Claude Code agent 读取上下文，按 catalog 按需加载最多 2–3 个世界书条目，写 `response.txt`；
4. `round_deliver.py` 执行字数门禁、token 收集、调用 handler、写记忆；
5. `handler.append_turn()` 执行 MVU 命令，保存 turn，重建 content.js/state.js，通知前端。

## 运行时数据分层

| 位置 | 归属 | 例子 |
|---|---|---|
| `<card>/` | 卡片专属持久状态 | `chat_log.json`、`.initvar.json`、`.var_diff.json`、`memory/` |
| `<card>/memory/` | 跨会话叙事与设定 | `reference.md`、`project.md`、`story_plan.md`、`.worldbook_index.json` |
| `skills/styles/` | 当前激活卡的前端和瞬态运行态 | `input.txt`、`response.txt`、`round_context.txt`、`content.js`、`state.js` |
| `skills/engine/` | 可复用纯逻辑代码 | card/render/mvu/tokens/worldbook/context |

> 当前 `styles/` 与卡片目录仍存在 state/content 双写与全局单例问题；这是下一阶段的数据分层重构对象，详见 [技术债](../status/technical-debt.md)。
