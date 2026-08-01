# ADR-0013：浏览器 Provider 配置、流式投影与 Agent Trace

- **状态**：Superseded by the canonical Studio/Workspace implementation
- **日期**：2026-07-28
- **关联**：ADR-0007、ADR-0010、ADR-0011、ADR-0012

## Decision

1. 前端按 task id 累积 `narrative.preview.delta` 为临时 AI 回合；终态、开场和存档切换时清除，只有 runtime commit 后的 projection 写入历史。
2. canonical Studio `/v1/studio/*` 管理 Workspace 中的 provider profile、Agent、Graph 和 Project；模型列表由 OpenAI-compatible Provider 获取。
3. API key 由本地 Secret Store 管理，不进入 definition、events、manifests、SQLite、projection 或响应。
4. `SequentialAgentGraph` 对单节点和多节点统一运行，发布 `agent_node.started/finished`；既有 `model_call.*` 和 `tool_run.*` 事件携带节点身份，前端 Agent Trace 按 sequence 展示生命周期、耗时、token 和工具结果。

## Consequences

- 流式 UI 不是事实源，正式恢复仍来自 durable snapshot/projection。
- URL/model 只影响后续冻结 task；key 重启后从本地 Secret Store 或环境变量读取。
- graph 仍是受限线性 writing-role pipeline，不是通用工作流 DSL。
- `/models` 失败只返回稳定错误，不回显请求详情或 key。

## Verification

`python3 -m pytest -q`：147 passed。真实 DeepSeek 浏览器命令路径在 `0.0.0.0:8765` 成功提交到 revision 10，并观测到 node、preview、model、commit 和 terminal events。

## Remaining Limits

真实浏览器长会话、断线/代理 SSE 行为、多 provider profile 和多节点独立配置仍待验证。
