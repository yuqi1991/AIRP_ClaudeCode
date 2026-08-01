# 实时运行状态

当前运行状态不再由 Claude 手工编辑 HTML，也不再依赖旧的 `skills/styles` 目录。游戏页通过 AIRP server 的 snapshot、SSE event stream 和 Workspace runtime projection 显示当前 session。

## 可观察入口

- 游戏页：`http://localhost:8765/`
- Studio：`http://localhost:8765/studio`
- Snapshot：`GET /v1/session/snapshot`
- Durable events：`GET /v1/session/events?after=<sequence>`
- SSE：`GET /v1/session/events/stream?after=<sequence>`
- Graph/Agent Trace：Studio 的当前/最近 Graph Run 与节点详情面板

节点事件包括 `agent_node.started/finished`、`model_call.started/finished`、`tool_run.*`、`narrative.preview.delta` 和最终 commit/terminal 事件。节点失败会终止整图，并在 Trace 中标出失败节点；重试由用户点击整图重跑。
