# 移除 Pi Execution Dependency

- **状态**：Accepted
- **日期**：2026-08-01
- **关联**：ADR-0004、ADR-0010、ADR-0014、ADR-0018

AIRP 不再把 Pi Agent Core、Pi AI 或 Node sidecar 作为运行时依赖。当前执行边界由 AIRP 自己拥有：`GraphRuntime` 调度冻结的 `ExecutionPlan`，`ProviderNodeRunner` 处理单节点的提示词、流式 provider 调用、工具回合和 Regex Collection，`OpenAICompatibleProviderAdapter` 只负责 `/v1/chat/completions` 与 `/v1/responses` 协议适配。

Pi 之前验证过 Agent loop、工具和 provider 的形态，但没有提供 AIRP 所需的持久 task、revision、Graph Trace、Project/Session 或世界书所有权。继续保留它会产生两个执行事实源、额外的 Node 进程和不可见的上下文边界，因此已删除 package 依赖、sidecar 资源和旧真实 provider bridge。

## 能力边界

- Graph Runtime 不发现文件、不理解世界书、不注册工具，也不读取 RP 数据。
- Host 在一次任务启动时编译并冻结 Project、Agent、Provider、Worldbook 和工具权限。
- Agent Definition 的 tool allowlist 只决定节点可以请求哪些已注册只读工具；Host 通过 `NodeExecutionContext` 注入实际 handler。
- 当前 RP Host 注册 session snapshot、recent memory、exact-title worldbook entry 三个只读工具。工具调用结果进入下一次 provider request，并持久化到 Node Trace。
- 世界书不是隐式 prompt 解析器：Context Manifest 只提供 catalog，Agent 需要显式调用按标题加载工具；整本世界书不会自动塞进上下文。
- 所谓 skill-mode 是世界书的 catalog/on-demand 约定，不是扫描 `skills/` 目录的隐藏执行机制。未来其他 skill/capability 必须通过显式 Host registry 注入，不得进入 Graph Runtime。

旧 `engine.director` / `SequentialAgentGraph` 已删除。生产 server 始终配置 `ExecutionPlanCompiler + GraphRuntime + ProviderNodeRunner`；唯一保留的旧格式兼容代码是一次性卡片回合导入。
