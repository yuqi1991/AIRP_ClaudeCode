# ADR-0003：引擎采用 Deep Module 分层

- **状态**：Accepted
- **日期**：2026-07-19

## Context

原 `handler.py` 约 1151 行，混合 response 解析、日志与状态、MVU、渲染、HTTP bridge、开场和 CLI。`skills/` 是平铺 CLI 脚本，难以测试和局部修改。

## Decision

提取 `skills/engine/` 深模块：

- `card`：卡片状态和回合编辑；
- `render`：前端渲染；
- `mvu`：变量命令与审计；
- `tokens`：token 记账；
- `worldbook`：catalog；
- `context`：上下文构建。

`handler.py` 保留回合编排和 CLI seam，缩至约 649 行。CLI 路径保持不变。

## Consequences

- 调用者面对更小的 interface，文件格式和 HTML/MVU 复杂度集中在 engine 内；
- 模块可单独验证；
- 数据目录尚未重组，styles/与卡片目录的双写问题留给后续阶段。
