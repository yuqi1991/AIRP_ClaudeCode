# ADR-0027：临时多 Agent 接力运行

- **状态**：Accepted
- **日期**：2026-08-02
- **关联**：ADR-0021、ADR-0026

一次玩家输入仍只创建一个持久 Turn Task，并且最多产生一个正式 Turn Commit。Task 内部的 Pi Agent transcript、草稿与 Agent Run 状态属于临时协作运行：它们不成为存档会话 revision，也不在进程退出后恢复。为 Monitor 保留的最新 Graph/Node trace（节点输入输出、模型和工具事件）可有限持久化，但只用于查看，不用于恢复或续跑协作。启动恢复发现未完成协作时，将 Task 标记为中断；玩家通过 reroll 从原玩家输入和最后一个正式 revision 创建新的协作运行。

协作配置指定一个可配置的入口 Agent 和一个唯一的交付 Agent。两者都是普通 Agent Definition：Instruction Prompt 在冻结 Task snapshot 上为每个 Agent Run 独立展开。用户配置固定接力链与每条连接的 Handoff Prompt；一站的原始终止输出先经过该 Agent 绑定的 Regex Collection 转换，再与 Handoff Prompt（同样在冻结 Task snapshot 上展开）组合为下一站的上游输入，运行时不从模型文本推断或调度下一站。每个 Agent 在协作运行期间保留完整、仅内存存在的个人会话记忆，完整 transcript 不会自动继承。一个 Task 采用严格串行调度：同一时刻只允许一个 Agent Run 调用模型或工具；所有 Agent 共同受模型调用、工具调用、接力次数、token 与时长的协作预算约束。

接力链可包含有明确出口的重复片段。当前版本只执行用户配置的精确次数，绝不因模型文本提前跳出；循环配置保留模式与迭代上限字段，以便未来增加只配置最大次数的受限循环，而不迁移既有配置。

Studio 第一版以纵向接力编辑器配置步骤、连接 Handoff Prompt 与重复片段，不提供自由画布。该界面与严格串行的实际执行顺序一一对应；动态条件分支与图编辑器保留给未来的受限循环能力。

Monitor 同时展示计划接力链与本次运行的实际进度：当前 Agent 高亮、已完成连接与循环次数可见，调试浮窗可查看实时文本、工具调用、Regex 转换与 Handoff 内容。浮窗提供仅文本视图，按阅读顺序保留 Agent 输入、模型文本、Regex 转换结果和 Handoff 文本，隐藏 ID、时间、token、状态机与其他结构化元数据。

接力调度只依赖窄的 AgentExecutor seam，以启动、继续、取消和观察单个 Agent 的临时会话。现有 Python ProviderNodeRunner 保留为确定性测试适配器；通过验证后，Pi Agent Core sidecar 是生产适配器。AIRP 保留接力顺序、Task snapshot、预算、最终提交和投影所有权。这取代 ADR-0019 对此协作运行的 Pi 拒绝，但不授权将 Pi Coding Agent CLI、其 session 文件或内置文件写入工具嵌入 AIRP。

交付 Agent 的普通终止输出即为候选正文，并先通过其绑定的 Regex Collection 转换后提交。Regex 规则未匹配时保留原文，运行时不额外定义格式标签、交付工具或质量门；协作职责、输出协议和何时结束均由用户的 Prompt 与 Regex 配置承担。

本 ADR 细化并局部取代 ADR-0021 的运行事件恢复要求：Task 身份、最终状态、正式提交和有限 Monitor trace 是持久事实；完整内部协作 transcript 不是可恢复的持久事实。这样保留唯一提交和 reroll 的安全性，同时避免为用户定义的开放式 Agent 协作构建 transcript 重放和邮箱恢复协议。
