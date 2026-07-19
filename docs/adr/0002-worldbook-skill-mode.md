# ADR-0002：世界书采用 Skill 模式按需加载

- **状态**：Accepted
- **日期**：2026-07-19

## Context

旧方案按变量和关键词自动匹配，并把世界书全文预塞进每轮上下文。实测动态后缀可达约 32.9KB，存在冗余匹配和同一角色卡重复注入。

## Decision

每个世界书条目生成 `usage`（“讲什么 + 何时读”）。`WORLDBOOK_CATALOG` 每轮提供 `title — usage` 清单；叙事 agent 自主选择本轮需要的 2–3 条，以完整标题按需读取 `reference.md`。

## Consequences

- 动态上下文实测从约 32.9KB 降至约 2.8KB；
- 叙事 agent 对检索决定承担更多责任；
- usage 成为世界书条目的协作 interface；
- 当前 Claude Code transcript 仍会累积按需读取结果，独立 runtime 应进一步控制上下文。
