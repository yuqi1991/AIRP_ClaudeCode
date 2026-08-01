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

Project 删除现在会在 idle 边界清理 `runtime/projects/<project_id>`、
`sessions/projects/<project_id>.sqlite3` 和 active/recent metadata，并有删除后重启回归。

## P1：上下文仍不可精确控制

**状态：Working**

世界书 skill 模式已降低每轮预塞成本，但 Claude Code 的追加 transcript 会累积历史与工具读取结果。独立 harness 必须提供显式 context builder、窗口策略和可观测 token 预算。

## P1：世界模拟质量未独立验证

**状态：Broken**

后台 NPC、时间推进、事件、伏笔和角色关系仍主要依赖单个叙事 agent 按提示执行；缺少独立世界模拟 agent、可解释状态提案和长期回归测试。

## P1：卡片兼容诊断缺失

**状态：Resolved**

`import_card.py`、`import_prepare.py` 和 Project import API 已发出
`airp.import-diagnostics` v1；游戏抽屉显示状态、世界书计数和可定位 finding。后续只需
继续扩充真实 PNG/JSON/TXT 兼容 fixture。

## P1：设定编辑缺少 revision 与 audit

**状态：Resolved**

Project、Provider、Agent、Graph、Worldbook、Regex Collection 保存均持久化 revision，支持
`expected_revision` 冲突；`.audit.jsonl` 只记录变更路径和 before/after hash。Task 的
Execution Plan 还会冻结 Project/Library revision 与配置 hash provenance，Session revision
保持独立。

## P1：MVU 校验过宽

**状态：Resolved**

兼容 handler 与正式 Runtime 共用 strict schema builder；只有卡片 schema 显式 wildcard 才能
创建动态子键，其余未知路径拒绝并返回稳定校验错误。

## P2：自动化测试不足

**状态：Working**

Python 回归、Studio API、Graph trace、浏览器黄金路径和 opt-in Provider 测试已集中在
`tests/`，默认 `pytest` 可运行。跨 provider、断线/代理 SSE 和长会话故障注入仍待补齐。

Provider 发布边界已由 [ADR-0022](../adr/0022-provider-release-qualification.md) 固定：
默认 CI 不使用真实 key；每个准备发布的 route 仍需 20 回合 bounded soak、两次 SSE 重连
和一次 Runtime 重启 qualification。

## P2：本地安全边界需审视

**状态：Resolved**

[`ADR-0025`](../adr/0025-mvu-and-local-security-boundary.md) 已固定威胁模型和目标边界：
默认 loopback、Origin allowlist、per-process capability、卡片脚本禁用、Markdown/HTML
allowlist、静态路径和 body 限制，以及 secret 不出 Store。CLI 默认 loopback；显式暴露时动态
API/SSE/DELETE 需要 per-process capability，Origin 不在 allowlist 时拒绝；主页面采用
Markdown/HTML allowlist 且不重执行卡片脚本。loopback 下无 Origin 的本地兼容请求保留免 token
行为，带 Origin 的请求仍执行同源校验。
