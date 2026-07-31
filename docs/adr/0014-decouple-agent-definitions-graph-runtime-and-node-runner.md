# Agent 定义、Graph Runtime 与 Node Runner 解耦

- **状态**：Proposed
- **日期**：2026-07-28
- **关联**：ADR-0004、ADR-0009、ADR-0013、`docs/architecture/agent-studio.md`

AIRP 将可复用的 Agent Definition 和线性 Graph Definition 编译为任务级不可变 Execution Plan；Graph Runtime 只调度节点顺序、状态、终止和 Agent Artifact 传递，Node Runner 只执行一个已解析节点并通过 Provider Adapter 调用模型或工具。Graph Runtime 不直接依赖具体 Provider，Node Runner 不感知 Graph 拓扑，节点之间不再传递含义不明的裸字符串。

第一阶段仍只实现线性 Graph，但 Execution Plan、Graph Run、Node Run 和 Agent Artifact 接口不依赖线性假设。Agent 名称不参与执行语义，Graph 通过稳定 node ID 和显式 `output_node_id` 决定流转与最终产物；同一个 Agent 可以在同一 Graph 中被多次引用。任一节点失败即终止整个 Graph Run，用户重试时以原始任务输入和当前定义重新编译计划并完整重跑，不做自动重试、跳过或模型 fallback。

此边界的代价是需要显式的编译步骤、Artifact envelope 和运行记录，但换来定义编辑、Graph 调度、单节点执行、Provider 集成与前端调试可以独立演进。运行中的 Studio 修改只影响下一次 Graph Run。
