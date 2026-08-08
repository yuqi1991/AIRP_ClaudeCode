# 当前 Runtime 与演进边界

## 当前：独立 AIRP Runtime

当前 runtime 直接接收浏览器命令，由 AIRP 持有 Task、冻结 Context、Graph、Trace、唯一 commit 与投影。GraphRuntime 以严格串行的静态 Handoff 链调度节点；Pi Agent Core 只在本机 sidecar 中执行单个 Agent 的模型-工具多轮，并在 Graph Run 结束、失败或取消时销毁全部私有 transcript。

- 不依赖 Claude Code session、`wait_pending` 或追加式 transcript；
- Agent 输入来自显式冻结 snapshot、上游 Handoff 与受控工具结果；
- token、工具、模型调用与交接通过 AIRP Trace/SSE 观察；
- Pi 不读取或写入 AIRP Session/Revision，也不拥有提交权；
- 每次玩家输入最多产生一个正式 revision，中间草稿不进入故事历史。

## 演进：用户定义的协作

当前产品提供固定顺序、Handoff Prompt 和精确次数循环。角色职责、何时调用工具、何时以普通文本结束以及下游如何处理结果，均由用户编辑的 Agent Prompt 和 Handoff Prompt 定义。框架不内置“导演”“写手”“质检”等身份，也不从模型文本推断动态路由。

### 已锁定的最小回合边界

ADR-0021 将上述目标收敛为一条可观察的 Session Turn Runtime Contract：Runtime/Harness
独占 `submit → Task → Context Manifest → Graph/Provider/Tool → Turn Draft → commit →
projection/events` 的生命周期。浏览器只通过 Command API 和可重放事件交互；Provider、Tool
和模型没有 authoritative write 权限；同一 Session 不允许 canonical Runtime 失败后静默
回到 Claude Code/file-loop。

这一契约已经由当前 Python Runtime 的 durable Task、generation lease、revision/commit、
Trace、SSE 和 projection 代码承载，但真实 Provider 的跨协议/长会话可靠性仍不是本节的
完成证明，按 ADR-0022 的发布 qualification 继续验收。任意条件分支、动态路由和最大次数循环保持未决。

Provider 发布不以“能发出一次请求”为标准。ADR-0022 规定 adapter 能力契约和两层测试：
默认 CI 用无密钥 deterministic/loopback fixture，准备发布的真实 Provider 需通过 opt-in
qualification（含 20 回合 bounded soak、SSE 重连和 Runtime 重启）。未通过的 endpoint
可以供本地实验使用，但不能被产品清单标成已支持。

## 不变的权限边界

所有 Agent 都只能使用 Host 注册且被其 allowlist 允许的 capability。普通终止输出经 Regex Collection 后成为下游 Handoff 或最终候选正文；只有 harness 才能将最终正文提交为故事 revision。任何未来的动态路由或额外 capability 都必须保持这一边界。

## 外部资料补全

叙事需要且本地资料不足时，agent 可以自主检索角色/作品资料。runtime 必须记录：

- 查询意图；
- 来源 URL 与时间；
- 提取摘要；
- 可信度/不确定性；
- 是否被玩家采纳为本地设定。

外部资料默认是候选参考，不能自动覆盖玩家卡片或世界书。
