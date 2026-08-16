# 路线图

> 这是方向与依赖关系，不是承诺日期。实现前应转成规格和可验证 ticket。

## 当前下一项

已确认的下一项 runtime cutover 是[Task Working Snapshot 与 Agent 工具](../specs/task-working-snapshot-and-agent-tools.md)：
最终正文保持 opaque，Agent 通过公开工具修改单一 Task Working Snapshot，成功时连同正文原子
提交 Memory/State Snapshot。它取代正常路径从正文解析 MVU 的行为；不等同于新增 Graph 角色、
质量门禁或默认套件特权机制。

## Phase 0 — 稳定当前原型

- 维护 engine 深模块边界；
- 为现有导入、MVU、context、render 和 server API 建最小自动化测试；
- 建卡片兼容测试集并实现 [`ADR-0024`](../adr/0024-import-diagnostics-report.md) 定义的导入诊断报告；
- 按 [`ADR-0025`](../adr/0025-mvu-and-local-security-boundary.md) 收回卡片脚本、MVU 和本地 HTTP 的安全边界；
- 记录当前人工 E2E 路径。

**完成标志**：现有 Claude Code 原型可被可靠回归验证，不再只靠手工经验。

## Phase 1 — 独立 harness/runtime（最高优先级）

- 用独立 runtime 替换 Claude Code 的 `wait_pending`/session loop；
- 建显式 session、事件、任务和 context builder；
- 抽象模型 provider/tool adapter；
- 独立 token、日志和可观测性；
- 消除对追加式 Claude transcript 的功能依赖。

**已锁定的基础契约**：ADR-0021 规定 canonical Runtime/Harness 独占从 `submit` 到唯一
commit、projection 和 durable events 的回合生命周期。真实 Provider、断线和长会话恢复
按 ADR-0022 的 qualification 验收；这不等同于任意 DAG 或多 Agent 世界模拟已经决定。

**已锁定的 Provider 发布边界**：ADR-0022 采用 OpenAI-compatible 能力契约；默认 CI 使用
无密钥 fixture，真实发布 route 需要 opt-in qualification，包括 20 回合 bounded soak、
两次 SSE 重连和一次 Runtime 重启。未通过的 Provider 保持 Experimental。

**当前执行边界**：[`ADR-0027`](../adr/0027-ephemeral-multi-agent-collaboration.md) 采用 AIRP 自有的 GraphRuntime 作为任务与交接编排者，并以 Pi Agent Core 作为每个临时 Agent 会话的工具循环。Pi 不是持久 runtime、不会读取或写入 session 文件。

**已完成决策**：Graph/Provider/Tool/Worldbook 边界已在 AIRP 内部验证；Pi Core sidecar、Pi package 和本地 JSONL bridge 只承担单 Agent 运行，不改变唯一 commit 和配置冻结所有权。

**依赖**：原型前先定义实验范围与 agent 通信协议 ADR；原型通过后再作正式 runtime 选型决策。

## Phase 2 — 用户定义的多 Agent 创作

- 支持由用户 Prompt 和 Handoff Prompt 定义的接力规范；
- 扩展静态固定循环为受限的最大次数策略，而不解析角色职责；
- 建立多 Agent、工具调用和 Regex 交接的回归场景；
- 将 MCP 与外部检索作为显式项目 capability，记录来源和玩家采纳。

## Phase 3 — 玩家实时编辑与卡片诊断

- 玩家可在游玩中轻量修改角色、故事、世界书和变量；
- 修改进入可追溯的设定层，并立即影响后续回合；
- 提供结构化卡片/世界书调试器；
- 接入 [`ADR-0024`](../adr/0024-import-diagnostics-report.md) 的导入兼容报告、错误定位和修复建议。
- 按 [`ADR-0026`](../adr/0026-project-editing-and-trace-semantics.md) 为游玩中设定编辑增加 revision、冲突、审计和配置快照追溯。

## Phase 4 — 数据与多会话重构

- 以 session/card 为单位隔离运行态；
- 消除 styles 与卡片目录中的双写；
- 支持多卡并行、多会话和安全的存档迁移；
- 重新设计前端状态协议。

**已锁定的持久化边界**：ADR-0023 规定 Project 拥有私有 runtime/session 状态，Workspace
可保存多个 Project/Session，但一个进程只维护一个 active projection；删除清理、孤儿状态
恢复和重启后的 active/recent metadata 回归已实现。
