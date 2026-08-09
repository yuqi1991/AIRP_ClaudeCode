# ADR-0022：Provider 发布能力契约与长会话 qualification

- **状态**：Accepted（发布边界已锁定；真实 Provider qualification 仍是发布门槛）
- **日期**：2026-08-02
- **关联**：Wayfinder #17、ADR-0019、ADR-0021、`docs/specs/agent-studio.md`

## Context

AIRP 的 Provider 入口是 `OpenAICompatibleProviderAdapter`，当前实现覆盖
`chat_completions` 和 `responses` 两种流式协议。Node Runner 负责有界工具循环、取消、
Regex 和 Artifact 边界；Runtime 负责 Task、commit、projection、Trace 和事件。已有
本地协议测试和一个 opt-in DeepSeek smoke，但这不足以说明任意供应商或任意模型都能
达到可发布可靠性。

“支持 Provider”需要同时回答两个问题：默认 CI 如何在没有外部密钥时保护协议不变量，
以及准备上线的真实 Provider 如何证明流式、工具、取消、Token、重连和长会话没有只在
FakeProvider 上成立。

## Decision

### 1. Canonical support is capability-based

AIRP 的正式 Provider transport contract 只包含：

- OpenAI-compatible `chat_completions` SSE；
- OpenAI-compatible `responses` SSE；
- 文本 delta、结构化 tool call、终态 usage、stop reason；
- timeout/abort、稳定错误分类（`provider_unavailable`、`provider_rejected`、
  `terminal_internal`）和 retryability；
- Provider adapter 负责认证与协议转换，凭据不进入 Request、Event、Trace、Manifest、
  Projection 或 SQLite。

供应商的原生专有协议不属于 canonical support，除非未来新增独立 adapter 和对应 ADR。
Provider Profile 可以保存未 qualification 的 endpoint，但 UI/发布清单必须将其标为
`Experimental / 未验证`，不能把配置存在误报为产品支持。

### 2. Default CI is deterministic and keyless

每次默认 CI 必须运行 FakeProvider 和 loopback HTTP adapter 套件，覆盖：

1. 两种协议的请求映射、流式 delta 拼接和终态 usage/stop reason；
2. assistant tool-call 与 tool result 的多轮回放、有界工具轮数和工具失败；
3. timeout、AbortSignal、retryable/terminal 错误和“不自动 fallback”；
4. Token/latency/cost telemetry、secret redaction 和请求参数保护；
5. durable SSE 从 `after`/`Last-Event-ID` 重连、Task snapshot、唯一 commit 和投影一致性；
6. 固定卡片上的短回合、取消、Runtime 重启和恢复语义。

默认 CI 不需要真实 Provider key，也不因为外部服务波动而变红。

### 3. Real Provider qualification is opt-in but required for release

每个要列入“已支持”清单的 Provider route/model family 都必须在专用测试卡上通过
qualification。最小 baseline 是：

- 真实中文或目标语言流式文本；
- 至少一次真实工具调用及下一轮工具结果回放；
- provider-reported usage、stop reason、latency 和成本估算可观察；
- 取消或 transport timeout 后没有 commit，且前一 revision 仍是 active；
- 一次完整的 Graph → Turn Draft → commit → projection 回合；
- 至少 **20 个连续回合** 的 bounded soak，过程中至少 **2 次 SSE 重连** 和 **1 次
  Runtime 重启**，最终 revision、active projection、Token 统计和事件 sequence 一致。

该套件由环境变量/显式命令 opt-in，不进入每次默认 CI；未通过的 Provider 只能以
`Experimental / 未验证` 运行，并在失败时展示可操作分类。

### 4. Failure semantics are visible and non-magical

- `provider_unavailable`、超时和可重试 transport failure：任务保持未提交，可由用户重试，
  但不自动换模型或切 legacy runtime；
- `provider_rejected`、工具参数错误和不可重试错误：任务进入明确失败态并保留 Trace；
- `cancelled`：部分文本只存在于 Trace/preview，不推进 revision；
- commit/projection 失败：active head 保持原值，任务显示非提交错误或 `projection_pending`，
  可由 Runtime 安全恢复；
- 所有失败都要保留 task、event、Trace 和最后一个 committed snapshot，不能只留下浏览器
  的 toast 或进程内状态。

## Evidence and maturity

当前 `tests/test_provider_execution.py` 覆盖两种 loopback 协议、工具消息映射、错误分类、
timeout 配置和 secret redaction；`tests/test_provider_qualification.py` 在无密钥默认 CI
中覆盖 20 回合 bounded soak、两次 `Last-Event-ID` SSE 重连、Runtime 重启、任务级
Token/latency/cost、失败 Trace 和重试分类；Playwright 发布矩阵覆盖桌面/移动几何门槛。
`tests/test_real_deepseek_e2e.py` 已通过 opt-in Chat Completions 中文流式 usage smoke。
真实 20 回合 route 测试也已提供，但必须由维护者显式运行并保存证据；在此之前功能状态
保持 `Experimental`。

2026-08-10 的[DeepSeek 刻晴双回合最小闭环验收](../reports/deepseek-keqing-two-turn-qualification-2026-08-10.md)
补充了默认协作套件经 Pi Core、真实 DeepSeek 流式调用和角色卡导入的两次连续提交证据。
它只证明最小游玩闭环，不能替代本 ADR 的 20 回合、两次 SSE 重连和一次 Runtime 重启发布门槛；
该路由仍是 `Experimental / 未验证`。

## Out of scope

- 为每个供应商维护一套原生 SDK/protocol adapter；
- 在默认 CI 中存放或使用真实 API key；
- 以当前线性 Graph 的 qualification 推断任意 DAG 或多 Agent 世界模拟已经确定。
