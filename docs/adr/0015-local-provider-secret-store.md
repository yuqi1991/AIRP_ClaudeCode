# 阶段一使用本地 Provider Secret Store

- **状态**：Proposed
- **日期**：2026-07-28
- **关联**：ADR-0010、ADR-0013、`docs/architecture/agent-studio.md`

为优先完成可用的多 Provider Profile 管理，阶段一通过独立本地 `secrets.json` 持久化 API Key，并以 `SecretStore` 接口隔离具体存储方式。密钥文件限制为当前用户读写且不得进入版本控制、导出包、Agent、Graph、Execution Plan、事件、Trace、运行数据库或日志；前端只能覆盖、测试和删除密钥，后端永不回传原值。

该方案只防止意外泄漏，不抵御拥有本机文件访问权的用户或进程。未来可以在不改变 Provider Profile、Agent 或 Graph 契约的前提下替换为系统凭证存储。此决策在目标架构层修订 ADR-0013 中“浏览器密钥只保存在进程内存”的阶段性限制；ADR-0013 仍准确描述当前已实现行为，直到本 ADR 落地。
