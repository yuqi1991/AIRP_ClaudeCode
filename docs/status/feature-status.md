# 功能成熟度矩阵

> 状态定义见 [Wiki 首页](../README.md)。本表区分“已经有代码”“已经验证有效”和“未来目标”。

## Proven

| 功能 | 状态 | 用户价值 | 证据/入口 | 限制 |
|---|---|---|---|---|
| 卡片导入与存档 | Proven | 导入 PNG/JSON/TXT 素材，生成卡片目录、开场、世界书、变量和记忆文件 | `skills/import_card.py`、`skills/import_prepare.py`、`/rp` | 兼容范围未自动测试，复杂卡片仍需诊断 |
| MVU 变量与记忆 | Proven | 回合中执行变量更新，保存 chat log、变量差分和剧情摘要 | `skills/engine/mvu.py`、`engine/card.py`、`write_memory.py` | 未知路径和 server 不可用时校验较宽松 |
| 前端回合体验 | Proven | 浏览器输入、开场切换、重roll、回退、token 展示和内容渲染 | `skills/server.py`、`skills/styles/index.html` | 单卡全局运行时，无法多实例 |
| 世界书 skill 模式 | Proven | catalog + usage + 按需加载条目，显著降低动态上下文体积 | `engine/worldbook.py`、`round_prepare.py`、ADR-0002 | 长会话中 Grep 结果仍进入追加式 transcript |
| engine 深模块 | Proven | token、MVU、卡片存储、渲染、世界书和 context 已拆为独立模块 | `skills/engine/`、ADR-0003 | 数据目录仍未重组 |
| Pi Runtime Spine | Experimental | 单 Session 的命令、任务、revision、唯一提交和兼容投影可通过确定性 executor 端到端验证 | `engine/runtime.py`、Session Turn Runtime Contract、ADR-0004 | 尚未接入 Pi、真实 provider、SSE、恢复或 revision branch |
| Context Manifest / Runtime Preset | Experimental | 编译器在执行前冻结 revision-scoped snapshot，持久化可重放 payload、section source/hash、预算决策、worldbook provenance；文件型 preset 支持稳定 entry ID、role、启停、placement/depth/order、内联或 Markdown source 及确定性 placeholder；`settings.json.runtime.preset_id` 与浏览器编辑器已接入真实 task freeze | `engine/context_compiler.py`、`engine/runtime_config.py`、`skills/styles/presets/default.json`、`skills/tests/test_runtime_config.py`、ADR-0009 | 不支持 autoActivate、date/time/cwd 等非确定性宏；配置编辑只影响下一 task；真实 DeepSeek 对新增 preset/graph 的 E2E 本轮未执行 |
| Narrative Director Execution Layer | Experimental | 叙事导演作为单一写作职能 Agent：通过封闭 typed tools 提交结构化回合草稿，流式 preview、abort、provider 错误分类与 telemetry 端到端可验证；Python + FakeProvider 范围 | `engine/director.py`、`engine/tools.py`、`engine/provider.py`、`engine/runtime.py`、Ticket 03 contract、ADR-0005 | 真实 DeepSeek E2E 仍为选型门槛（见 Real Provider 行）；SSE/reroll/rollback 已在并行 Experimental 行 |
| Real Provider (Pi Node sidecar + DeepSeek) | Experimental | `RealProviderAdapter` 经 stdio line-JSON 驱动 Node sidecar（`@earendil-works/pi-ai@0.82.1` 薄 `stream` 路径），默认 model `deepseek-v4-flash` / base `https://api.deepseek.com`；mock 模式覆盖 IPC/abort/crash/no-leak；真实 E2E opt-in | `engine/provider.py`、`skills/sidecar/pi_provider_sidecar.mjs`、`package.json`、`skills/tests/test_real_provider_sidecar.py`、`skills/tests/test_real_deepseek_e2e.py`、ADR-0010 | **真实 DeepSeek E2E 已通过**（2026-07-26，中文流式+多轮工具+commit+真实 usage/cost）；凭证仅 env、永不入 IPC/events/manifests；Pi 类型不泄漏过 adapter；多轮 tool_calls 回放 bug 已修并加 FakeProvider 回归；未接 browser golden path / generation lease（Ticket 07） |
| Atomic Turn Commit | Experimental | commit 前只强制技术完整性：非空可见正文、严格 MVU/schema、optimistic revision、唯一 commit 与可重试幂等投影；字数、文风、人称、NSFW、摘要/选项等属于可编辑 preset 策略，不阻止 commit | `engine/runtime.py`、`engine/quality.py`、`engine/mvu.py`、`skills/tests/test_session_turn_runtime.py`、`skills/tests/test_narrative_director.py`、Ticket 05 contract、ADR-0006 | 崩溃 recovery 已能重投影并分类 abandoned task，但完整 generation lease 仍未实现；分支语义见 Revision Branch 行 |
| Session Event Stream / Command API | Experimental | 玩家命令经幂等 `SessionCommandService` 进入 runtime；stdlib HTTP 暴露 submit/cancel/snapshot 与 SSE（按 sequence 续接 queued→preview→committed→terminal）；刷新后可从同一 DB 恢复 snapshot/commit/projection；不依赖 legacy `.pending`/`input.txt`/`wait_pending` | `engine/commands.py`、`skills/runtime_server.py`、`skills/tests/test_session_event_stream.py`、Ticket 04 contract、ADR-0007 | 单 Session tracer bullet；无 crash lease/recovery（Ticket 07）；SSE 以 FakeProvider/ScriptedDirector 验证，真实浏览器/DeepSeek 仍属最终选型门槛 |
| Revision Branch / Reroll-Rollback | Experimental | 全局单调唯一 revision + `parent_revision` DAG + 单 active head；reroll 复用原输入/parent 建新 tip；rollback 移 head 不删历史；active-lineage 沿 parent 链读上下文；superseded 分支可审计但不进 chat/manifest | `engine/runtime.py`、`engine/commands.py`、`skills/runtime_server.py`、`skills/tests/test_revision_branch.py`、Ticket 06 contract、ADR-0008 | 单 Session；无 generation lease——reroll/rollback 释放锁致 head 中间态窗口（commit 边界仍原子，最坏 stale+浪费，不损坏数据），真正 lease/崩溃恢复属 Ticket 07；无 multi-head UI；真实浏览器/DeepSeek E2E 未做 |
| Sequential Writing-Agent Graph | Experimental | 文件型 graph 以稳定 node ID/role/order/model 驱动受限顺序 writing-role pipeline；角色/NPC 仍是世界状态实体而非 Agent；默认仅启用 narrative director，浏览器可编辑并选择 graph | `engine/agent_graph.py`、`engine/runtime_config.py`、`skills/styles/graphs/default.json`、`skills/tests/test_agent_graph.py` | 明确不支持任意条件 DSL；多节点会线性增加 provider 调用与延迟；新增 graph 的真实 DeepSeek E2E 未执行 |
| Turn-Flow Decoupled from Model | Experimental | commit 是 harness 动作而非模型工具：模型只产叙事文本（`<content>/<summary>/<options>/<UpdateVariable>`），harness 解析+门禁+提交；模型工具面收窄为 3 个 read-only；基本流转不依赖模型「听话」——验收测试证明模型零工具调用仍完成回合 | `engine/turn_parser.py`、`engine/director.py`、`engine/runtime.py`、`engine/tools.py`、ADR-0011（取代 ADR-0005/0006/0010 的 commit-as-tool 描述） | 取代 commit-as-tool；generation lease/crash recovery 仍属 Ticket 07；模型低质/非法 MVU 由技术门禁+有界修订处理，主观写作质量由 preset 管理 |

## Working

| 功能 | 状态 | 当前情况 | 主要缺口 |
|---|---|---|---|
| 卡片/世界书兼容 | Working | 可解析 PNG/JSON/TXT、世界书、正则和美化相关资产 | 缺少统一兼容报告、自动测试和可视化调试 |
| 后台 NPC 与剧情规划 | Working | 规则和回合流程已定义，存在 story plan 与记忆文件 | 长期演化质量、可解释性和独立验证不足 |
| 文风与防抢话 | Working | settings/profile/硬性门禁已存在 | 仍依赖单 agent 自觉执行，缺少独立审校闭环 |

## Broken / 关键缺口

| 功能/问题 | 状态 | 为什么重要 | 当前证据 |
|---|---|---|---|
| Claude Code runtime | Broken | `wait_pending`/session loop 不适合作为独立产品 runtime | 当前流程依赖 ScheduleWakeup 与 Claude session |
| 上下文精确控制 | Broken | 追加式 transcript 会累积历史和按需 Grep tool result，无法完全由引擎决定 prompt 形状 | ADR-0001 |
| 世界模拟 | Broken | 后台 NPC、事件和伏笔尚不够自主、稳定或可解释 | 主要依赖单 agent 每轮遵守规则 |
| 卡片兼容诊断 | Broken | 导入失败、变量规则、正则、美化问题难以定位和向玩家解释 | 无完整诊断报告/测试矩阵 |
| 自动角色资料补全 | Planned | agent 尚未形成可追溯的 Web Search→资料摘要→设定采纳流程 | 产品原则已确认，未实现 |
| 玩家实时编辑 | Planned | 玩家尚不能有结构化、即时、可追溯地编辑设定 | 产品方向已确认，未实现 |

## Planned

- 独立 harness/runtime，替换 Claude Code 直驱 loop；
- 多 agent 编排：叙事导演、世界模拟、角色演化/文风润色；
- 玩家实时设定编辑；
- 卡片/世界书兼容诊断与修复建议；
- 外部角色资料的自主检索、来源记录和玩家采纳机制；
- 自动化测试与端到端验收。
