# Agent 协作指南

## 开始工作前

按顺序阅读：

1. [`CONTEXT.md`](../../CONTEXT.md)
2. [`docs/README.md`](../README.md)
3. [`docs/status/feature-status.md`](../status/feature-status.md)
4. 当前任务相关的架构文档和 ADR
5. 最后才读实现源码

不要把 roadmap 中的 Planned 写成当前实现，不要把未验证的设计写成 Proven。

## 模块边界

| Module | 调用者应知道的 interface | 不应跨越的实现细节 |
|---|---|---|
| `engine.card` | 回合、状态和日志的读写/回退 | JSON 文件位置和差分格式 |
| `engine.render` | 将持久状态生成前端载荷 | HTML、宏和 beautify 细节 |
| `engine.mvu` | 变量命令解析、验证、执行和审计 | JSONPatch/路径/schema 实现 |
| `engine.tokens` | checkpoint 与 usage 统计 | transcript 文件扫描 |
| `engine.worldbook` | catalog 与 usage | 索引持久格式 |
| `engine.context` | import/round 汇总文本 | 文件枚举和路径格式 |

修改一个模块时，优先扩大它内部的实现深度，而不是让更多调用者了解其文件格式或私有 helper。

## 代码与文档同改

以下变化必须同步更新 wiki：

- 用户可见行为或功能状态；
- engine module interface；
- 卡片目录、世界书、记忆或 MVU 数据格式；
- runtime/context/token 策略；
- 新的硬到逆转架构决定；
- 已知限制被修复或新发现。

具体动作：

1. 更新相关 `docs/status/feature-status.md`；
2. 更新对应 architecture 文档；
3. 新硬决策增加 ADR；
4. 在 PR/commit 描述说明验证方式。

## 验证最低要求

- 纯 engine 改动：对模块写或运行行为测试；
- import/round/deliver 改动：至少跑一次导入→开局→回合准备；
- 前端改动：启动服务，在浏览器走黄金路径；
- 修改世界书/context：检查 `round_context.txt`，确认 catalog 和动态后缀符合预期；
- 修改 MVU：检查变量、`.var_diff.json`、`chat_log.json` 与前端渲染是否一致。

## 资料检索规则

外部检索得到的是候选资料，不是玩家事实。任何自主检索都应记录来源、摘要、适用范围与是否被玩家采纳。玩家卡片和世界书优先于外部资料。

## 不要做的事

- 不要重新引入“每轮预塞全部世界书全文”的上下文策略；
- 不要让多个 agent 直接无协调写同一份状态；
- 不要把当前 Claude Code runtime 当作长期产品架构；
- 不要绕过 engine module interface 在多个 CLI 脚本里重复文件读写；
- 不要把运行时产物、卡片私有资料或凭据提交进仓库。
