# AIRP Wiki

本 wiki 记录产品事实、实现架构、功能成熟度、技术债和协作规则。它服务于玩家、维护者和参与开发的 agent。

## 快速入口

- [项目事实入口](../CONTEXT.md)
- [产品定位](product/positioning.md)
- [功能成熟度](status/feature-status.md)
- [架构总览](architecture/overview.md)
- [路线图](status/roadmap.md)
- [Agent 协作指南](development/agent-guide.md)

## 文档地图

### 产品

- [产品定位与边界](product/positioning.md)
- [产品原则](product/principles.md)

### 架构

- [总体架构与回合数据流](architecture/overview.md)
- [当前 runtime 与目标 runtime](architecture/future-runtime.md)
- [角色卡、世界书与记忆](architecture/card-worldbook-memory.md)

### 状态与规划

- [功能成熟度矩阵](status/feature-status.md)
- [技术债与已知限制](status/technical-debt.md)
- [路线图](status/roadmap.md)

### 开发

- [Agent 协作指南](development/agent-guide.md)
- [架构决策记录](adr/)

## 文档状态约定

| 状态 | 含义 |
|---|---|
| **Proven** | 已有端到端路径且被实际验证。 |
| **Working** | 主路径可用，但边界情况、兼容性或自动测试不足。 |
| **Experimental** | 已实现或已设计，但尚未充分验证。 |
| **Planned** | 已确认方向，尚未实现。 |
| **Broken** | 当前已知不能可靠工作。 |
| **Deprecated** | 不应继续依赖，等待替换或删除。 |

## 维护规则

- 代码改动必须同步更新对应文档、功能状态和 ADR。
- 未经验证的判断必须标为 `Experimental` 或“假设”，不能写成事实。
- 新的硬到逆转决定写入 `docs/adr/`。
- 新 agent 开始工作前，至少阅读 `CONTEXT.md`、本页、功能状态表和相关 ADR。
