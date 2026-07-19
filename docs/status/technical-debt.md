# 技术债与已知限制

## P0：独立 runtime 缺失

**状态：Broken**

当前依赖 Claude Code session、ScheduleWakeup 和 `wait_pending`。它限制了产品独立性、上下文控制和多 agent 编排。下一代第一优先级是独立 harness/runtime。

## P0：全局单例运行态

**状态：Broken**

`skills/styles/.card_path`、`state.js`、`content.js` 和输入/上下文文件是当前激活卡的全局单例；多卡并发会互相覆盖。`state.js/content.js` 同时写在 styles 与卡片目录，缺乏单一事实源。

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

**状态：Broken**

当前主要依赖人工 E2E。没有可用项目测试套件，`npm test` 不提供通过路径。应为 import、MVU、context、render、server API 和跨回合场景建立测试。

## P2：本地安全边界需审视

**状态：Working**

服务器仅绑定本机，但 CORS 与前端脚本执行面需要重新审计；不可信卡片内容和本机恶意页面不应能无约束触发操作。此项不应阻塞本地原型，但独立 runtime 前必须明确威胁模型。
