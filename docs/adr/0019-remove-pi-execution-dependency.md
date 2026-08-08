# 移除 Pi Execution Dependency

- **状态**：Superseded by ADR-0027
- **日期**：2026-08-01
- **关联**：ADR-0004、ADR-0010、ADR-0014、ADR-0018

本 ADR 记录当时移除 Pi 依赖的决定。随后确认的临时多 Agent 接力需要一个真正的、可保留工具循环上下文的单 Agent 执行器；ADR-0027 已以受限的 `PiCoreNodeRunner` + 本地 Node sidecar 取代本决定。AIRP 仍拥有冻结计划、Graph 调度、工具权限、Regex、Trace 与唯一提交；Pi 不拥有持久任务或存档事实。

Pi 之前验证过 Agent loop、工具和 provider 的形态，但没有提供 AIRP 所需的持久 task、revision、Graph Trace、Project/Session 或世界书所有权。继续保留它会产生两个执行事实源、额外的 Node 进程和不可见的上下文边界，因此已删除 package 依赖、sidecar 资源和旧真实 provider bridge。

## 能力边界

- Graph Runtime 不发现文件、不理解世界书、不注册工具，也不读取 RP 数据。
- Host 在一次任务启动时编译并冻结 Project、Agent、Provider、Worldbook 和工具权限。
- Agent Definition 的 tool allowlist 只决定节点可以请求哪些已注册只读工具；Host 通过 `NodeExecutionContext` 注入实际 handler。
- 当前 RP Host 注册 session snapshot、recent memory、exact-title worldbook entry 三个只读工具。工具调用结果进入下一次 provider request，并持久化到 Node Trace。
- 世界书不是隐式 prompt 解析器：Context Manifest 只提供 catalog，Agent 需要显式调用按标题加载工具；整本世界书不会自动塞进上下文。
- 所谓 skill-mode 是世界书的 catalog/on-demand 约定，不是扫描 `skills/` 目录的隐藏执行机制。未来其他 skill/capability 必须通过显式 Host registry 注入，不得进入 Graph Runtime。

旧 `engine.director` / `SequentialAgentGraph` 已删除。当前生产默认配置 `ExecutionPlanCompiler + GraphRuntime + PiCoreNodeRunner`；`ProviderNodeRunner` 保留为确定性测试和显式 `AIRP_AGENT_EXECUTOR=provider` 回退适配器。唯一保留的旧格式兼容代码是一次性卡片回合导入。
