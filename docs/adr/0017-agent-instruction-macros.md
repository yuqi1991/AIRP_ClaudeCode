# ADR-0017：Agent instruction-only 与运行时宏装配

- **状态**：Accepted
- **日期**：2026-07-29
- **关联**：ADR-0009、ADR-0014、ADR-0016、`docs/specs/agent-studio.md`

## Context

Agent Studio 原先允许 Agent 引用 Prompt Preset。这个额外层级把用户真正想调试的 instruction、上下文来源和运行时协议拆散，且旧 preset 中的写作偏好容易重新变成游戏页的隐式全局配置。Agent 需要一个可直接编辑、可预览、可在 Graph Run 中冻结的 instruction 来源。

世界书继续采用 catalog + 按需读取正文的 skill 模式。宏不能因为“通用上下文”而把 `reference.md` 或用户全文预加载到每个 Agent。

## Decision

1. 新建和 Studio 保存的 Agent 只持久化一个用户可编辑的 `instruction` 模板，不写默认 `prompt_preset_id`。
2. 模板使用确定性的 `{{name}}` 和 `{{dotted.path}}` 宏。运行时上下文包含项目卡、设置、变量基线、当前状态、近期记忆/回合、玩家输入、世界书 catalog、Graph handoff、输出契约和工具协议等已冻结值。
3. 缺失宏路径保留原占位符，预览返回原始模板、展开文本和可用宏根，避免拼写错误静默消失。
4. Graph plan 编译时冻结当前可用上下文；下游节点获得 Artifact 后，Node Runner 在真实 Provider 请求前只补展开 handoff/input artifact 宏。
5. Agent Definition、Graph Runtime 和 Node Runner 保持边界独立：宏实现是纯值转换，Graph Runtime 不解析模板；Node Runner 只在 Provider 请求边界补充当前 Artifact。
6. 旧显式 `prompt_preset_id` 仅作为存量文件/API 的兼容读取路径，新 UI、迁移和新 Agent 不生成它。Context Manifest 内部的默认 `PromptPreset` 仍是 ADR-0009 的 replay 兼容实现，不是 Studio 对象。

## Consequences

- 用户可以在一个 instruction 编辑框中调试软约束和上下文装配，Studio prompt preview 能显示宏展开证据。
- Agent prompt 不再需要用户理解或维护 preset library；旧项目可渐进迁移，不影响存量回放。
- 世界书正文仍必须通过 catalog 和显式工具/加载结果进入上下文；宏默认不暴露全文。
- 宏上下文随 Execution Plan 冻结，Studio 修改只影响下一次 Graph Run；当前与最近一次 Trace 可比较实际展开结果。
