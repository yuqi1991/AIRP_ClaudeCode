# 技术债与已知限制

## P0：独立 runtime 缺失

**状态：Resolved**

canonical runtime 已由 `airp.launcher`/`SessionRuntimeServer` 独立驱动，不再依赖
Claude Code session、ScheduleWakeup、`wait_pending` 或 `skills` 目录。

## P0：全局单例运行态

**状态：Working**

Session、事件、revision 和事实状态已进入卡片本地 SQLite；Workspace 持有可复用
Studio 配置。`state.js`/`content.js` 仍是单张卡的可重建兼容 projection，不能作为
事实源，但不再依赖仓库级 `skills/styles` 单例。多卡并发隔离和更彻底的 projection
替换仍是后续工作。

## P1：上下文仍不可精确控制

**状态：Broken**

世界书 skill 模式已降低每轮预塞成本，但 Claude Code 的追加 transcript 会累积历史与工具读取结果。独立 harness 必须提供显式 context builder、窗口策略和可观测 token 预算。

## P1：世界模拟质量未独立验证

**状态：Broken**

后台 NPC、时间推进、事件、伏笔和角色关系仍主要依赖单个叙事 agent 按提示执行；缺少独立世界模拟 agent、可解释状态提案和长期回归测试。

## P1：卡片兼容诊断缺失

**状态：Broken**

导入支持多种资产，但失败和降级往往缺少面向玩家的报告。需要建立：支持矩阵、导入诊断、变量/正则/美化兼容报告和最小复现卡测试集。

## P1：MVU 校验过宽

**状态：Working**

已知行为包括未知变量路径放行、MVU server 不可用时回退放行；`round_prepare` 对 checklist 异常采取吞错回退。需要明确 schema 严格度、错误展示和安全的修复策略。

## P2：自动化测试不足

**状态：Working**

Python 回归、Studio API、Graph trace、浏览器黄金路径和 opt-in Provider 测试已集中在
`tests/`，默认 `pytest` 可运行。跨 provider、断线/代理 SSE 和长会话故障注入仍待补齐。

Provider 发布边界已由 [ADR-0022](../adr/0022-provider-release-qualification.md) 固定：
默认 CI 不使用真实 key；每个准备发布的 route 仍需 20 回合 bounded soak、两次 SSE 重连
和一次 Runtime 重启 qualification。

## P2：本地安全边界需审视

**状态：Working**

服务器仅绑定本机，但 CORS 与前端脚本执行面需要重新审计；不可信卡片内容和本机恶意页面不应能无约束触发操作。此项不应阻塞本地原型，但独立 runtime 前必须明确威胁模型。
