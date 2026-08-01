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

Project 删除目前只移除 Workspace Project definition，尚未保证同步清理
`runtime/projects/<project_id>`、`sessions/projects/<project_id>.sqlite3` 和 recent
metadata；ADR-0023 已锁定该清理不变量，补齐实现和孤儿状态回归后才能标为 Proven。

## P1：上下文仍不可精确控制

**状态：Broken**

世界书 skill 模式已降低每轮预塞成本，但 Claude Code 的追加 transcript 会累积历史与工具读取结果。独立 harness 必须提供显式 context builder、窗口策略和可观测 token 预算。

## P1：世界模拟质量未独立验证

**状态：Broken**

后台 NPC、时间推进、事件、伏笔和角色关系仍主要依赖单个叙事 agent 按提示执行；缺少独立世界模拟 agent、可解释状态提案和长期回归测试。

## P1：卡片兼容诊断缺失

**状态：Broken**

导入支持多种资产，但失败和降级往往缺少面向玩家的报告。`ADR-0024` 已锁定
`airp.import-diagnostics` v1、逐字段 provenance、内嵌 Worldbook 绑定计数和
success/degraded/failed 语义；原型已验证三类报告形状。仍需把 emitter 接入
`import_card.py`/`import_prepare.py`，建立 PNG/JSON/TXT 和变量/正则/美化兼容 fixture，
并在游戏抽屉展示可定位的修复建议。

## P1：MVU 校验过宽

**状态：Working**

兼容 handler 仍对未知变量路径宽松，正式 Runtime commit 已有独立 strict validator。
[`ADR-0025`](../adr/0025-mvu-and-local-security-boundary.md) 进一步锁定：只有卡片 schema
显式 wildcard 才能创建动态子键；其余未知路径拒绝并审计。仍需把 wildcard 语义、稳定错误码
和浏览器可见的失败详情接入实现。

## P2：自动化测试不足

**状态：Working**

Python 回归、Studio API、Graph trace、浏览器黄金路径和 opt-in Provider 测试已集中在
`tests/`，默认 `pytest` 可运行。跨 provider、断线/代理 SSE 和长会话故障注入仍待补齐。

Provider 发布边界已由 [ADR-0022](../adr/0022-provider-release-qualification.md) 固定：
默认 CI 不使用真实 key；每个准备发布的 route 仍需 20 回合 bounded soak、两次 SSE 重连
和一次 Runtime 重启 qualification。

## P2：本地安全边界需审视

**状态：Working**

[`ADR-0025`](../adr/0025-mvu-and-local-security-boundary.md) 已固定威胁模型和目标边界：
默认 loopback、Origin allowlist、per-process capability、卡片脚本禁用、Markdown/HTML
allowlist、静态路径和 body 限制，以及 secret 不出 Store。当前 CLI 的 `0.0.0.0`、CORS `*`
和主页面 script 重执行仍需实现级修复；在这些 enforcement 完成前不能宣称本地安全边界 Proven。
