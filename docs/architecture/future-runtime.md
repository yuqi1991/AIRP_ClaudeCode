# 当前 Runtime 与目标 Runtime

## 当前：Claude Code 直驱原型

当前 runtime 使用 Claude Code 的 session、ScheduleWakeup 和 `/api/wait_pending` 长轮询来等待玩家输入并驱动回合。它已经证明了回合 pipeline 的产品价值，但存在产品化障碍：

- agent 生命周期由 Claude Code session 决定；
- `wait_pending` loop 是外部调度约束，不是引擎自身能力；
- 上下文通过追加式 transcript 累积，世界书 Grep 和历史位置无法由引擎完全控制；
- token 计账依赖本机 transcript；
- 多 agent 协作难以拥有明确的状态权限、消息协议与可观测性。

## 目标：独立 harness/runtime

下一代优先建设独立 harness，保留现有卡片、世界书、MVU、前端和 engine 模块的经验。它应：

1. 直接接收浏览器事件，不依赖 Claude Code 的 `wait_pending` loop；
2. 以显式 context builder 组装每个 agent 的上下文；
3. 以持久消息/任务模型编排 agent；
4. 以 provider-agnostic adapter 调用模型和工具；
5. 记录 token、工具调用、资料来源和状态写入；
6. 为玩家实时编辑和多卡并发预留 session 隔离。

Pi 是已否决的历史 harness 候选。ADR-0019 已确认 AIRP 自有 Graph Runtime + OpenAI-compatible Provider Adapter，新的 runtime 设计不得重新引入 Pi 作为执行依赖。

### 已锁定的最小回合边界

ADR-0021 将上述目标收敛为一条可观察的 Session Turn Runtime Contract：Runtime/Harness
独占 `submit → Task → Context Manifest → Graph/Provider/Tool → Turn Draft → commit →
projection/events` 的生命周期。浏览器只通过 Command API 和可重放事件交互；Provider、Tool
和模型没有 authoritative write 权限；同一 Session 不允许 canonical Runtime 失败后静默
回到 Claude Code/file-loop。

这一契约已经由当前 Python Runtime 的 durable Task、generation lease、revision/commit、
Trace、SSE 和 projection 代码承载，但真实 Provider 的跨协议/长会话可靠性仍不是本节的
完成证明，按 ADR-0022 的发布 qualification 继续验收。任意 DAG 和多 Agent 世界模拟保持未决。

Provider 发布不以“能发出一次请求”为标准。ADR-0022 规定 adapter 能力契约和两层测试：
默认 CI 用无密钥 deterministic/loopback fixture，准备发布的真实 Provider 需通过 opt-in
qualification（含 20 回合 bounded soak、SSE 重连和 Runtime 重启）。未通过的 endpoint
可以供本地实验使用，但不能被产品清单标成已支持。

## 未来一等 agent 职责

| Agent | 输入 | 输出/权限 | 责任 |
|---|---|---|---|
| 叙事导演 | 玩家输入、场景状态、检索结果、近期记忆 | 回合叙事、候选行动、建议变量变更 | 决定本轮戏剧与 NPC 反应，不直接篡改历史 |
| 世界模拟 | 当前世界状态、时间、后台 NPC、伏笔和事件 | 后台推进、事件建议、状态变更提案 | 保持世界独立于玩家视野的持续演化 |
| 角色演化/文风润色 | 草稿、角色档案、风格约束、反馈 | 修订稿、角色一致性/文风问题 | 保持声口、关系弧线与文本质量 |
| 卡片诊断（后续） | 原始卡片、导入产物、运行错误 | 兼容报告、修复建议 | 解释何处无法承载，不静默失败 |

所有 agent 的写入必须通过明确 seam：世界模拟提交状态提案，叙事导演消费已批准状态，渲染模块只读取已持久化事实。

## 外部资料补全

叙事需要且本地资料不足时，agent 可以自主检索角色/作品资料。runtime 必须记录：

- 查询意图；
- 来源 URL 与时间；
- 提取摘要；
- 可信度/不确定性；
- 是否被玩家采纳为本地设定。

外部资料默认是候选参考，不能自动覆盖玩家卡片或世界书。
