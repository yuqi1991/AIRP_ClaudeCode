# AIRP ClaudeCode — 项目事实入口

> 本文件是人类与 agent 接手项目时的第一阅读点。它只记录已确认的项目事实、术语与方向；实现细节见 [`docs/README.md`](docs/README.md)。

## 产品一句话

AIRP 是面向玩家的本地独立角色扮演引擎：玩家可导入和游玩角色卡/世界书，在长期剧情中实时调整角色与世界设定；引擎负责叙事、世界演化、状态持久化与可控的多 agent 编排。

## 当前阶段

- **当前实现**：以 Claude Code 为直驱编排层的可运行原型。
- **下一代方向**：优先替换 Claude Code 的 loop/runtime 为更轻量、可控的 harness；保留角色卡、世界书、MVU、前端与现有引擎经验。
- **目标产品**：独立 RP 引擎 + 角色卡/世界书管理编辑 + 多 agent 编排与通信。

## 核心用户

第一目标用户是长期游玩的 RP 玩家。玩家不是只读消费者：他们应能在游玩中修改故事、角色和世界设定，并看到修改立即影响后续回合。

## 成功标准

1. **持续沉浸**：长回合剧情、角色与记忆不漂移，世界持续演化。
2. **卡片兼容**：尽可能准确导入和承载既有角色卡、世界书与相关资产。
3. **低成本稳定**：上下文、token、运行时循环可控；回合流程可靠。
4. **可共同开发**：其他 agent 能从文档理解事实、边界与接口，安全扩展。

## 已确认的产品决策

| 决策 | 状态 | 依据 |
|---|---|---|
| 世界书采用 skill 模式：catalog + usage + 主 agent 按需加载全文 | 已实现 | ADR-0002 |
| 引擎模块采用 deep module 形态 | 已实现 | ADR-0003 |
| 当前 Claude Code runtime 只是原型，不是长期产品 runtime | 已确认 | ADR-0001 |
| 下一代首先建设可控 harness/runtime | 已确认 | `docs/status/roadmap.md` |
| 独立 Runtime/Harness 拥有从 submit 到唯一 commit、投影和事件的完整回合生命周期 | 已确认 | ADR-0021；Provider/长会话可靠性仍待验收 |
| Provider 采用 OpenAI-compatible 能力契约；真实 Provider 需 opt-in qualification，默认 CI 无外部密钥 | 已确认 | ADR-0022；20 回合 soak、SSE 重连和重启是发布门槛 |
| Project 拥有私有运行态；Workspace 可存多 Project/Session，但进程只维护一个 active projection | 已实现 | ADR-0023；删除清理、回滚与单进程语义已验证 |
| 角色卡与内嵌世界书导入使用 versioned `airp.import-diagnostics` 报告，区分 success/degraded/failed 与逐字段 provenance | 已实现 | ADR-0024；生产 emitter 与游戏抽屉展示已验证 |
| Session 状态写入通过获授权工具修改 Task Working Snapshot；原生 JSON operations 与可选 MVU 兼容输入统一经过冻结 schema/wildcard 严格校验；角色卡脚本默认不执行，Runtime 默认回环绑定并要求受控 Origin/授权 | 已确认 | ADR-0025；Wayfinder #40/#41 |
| 设定编辑使用 Project/Library revision，故事提交使用 Session revision；Task 冻结配置快照，未保存表单切换直接丢弃 | 已实现 | ADR-0026；revision conflict、audit 与延迟 rebind 已验证 |
| 多 Agent 不内置叙事导演、写手、质检等一等职责；职责、交付条件与接力规范由用户 Prompt 配置 | 已确认 | `docs/adr/0027-ephemeral-multi-agent-collaboration.md` |
| agent 可在角色资料明显不足时自主检索外部资料，并记录来源/摘要 | 已确认 | `docs/product/principles.md` |
| 代码变更必须同步更新相应 wiki、状态表和 ADR | 已确认 | `docs/development/agent-guide.md` |
| Studio 集成进游戏工作区，以顶部抽屉编辑配置并由右侧 Monitor 持续观察运行 | 已确认 | ADR-0020 |

## 术语表

| 术语 | 含义 |
|---|---|
| **AIRP** | 当前统一使用的产品与界面品牌名；未来确定正式产品名后可整体替换。避免在新界面中使用“话本RP”“Claude Code RP”或把“Agent Studio”作为独立品牌。 |
| **角色卡** | 用户导入的 PNG/JSON/TXT 素材，包含角色、开场、世界书、变量或前端资产。 |
| **游戏** | 一个可选择并游玩的 AIRP Project；拥有角色卡内容、开场、变量与素材，并选择世界书和编排配置。选择游戏会将其设为当前游戏，并恢复该游戏最后使用的存档会话。避免用“角色卡”指代整个游戏。 |
| **存档会话** | 一个游戏内独立延续的剧情记录或分支；同一游戏可以拥有多个存档会话，并记住最后使用的会话。避免简称为“游戏”。 |
| **存档记忆** | 随一个存档会话的 active lineage 演化、由用户自由命名的记忆条目组成的长线剧情记忆；每个 Session revision 保存完整 Memory Snapshot。rollback 直接恢复目标 revision 的 Snapshot，reroll 从被替换回合的 parent revision 重新派生，未来或旁支的记忆不得泄漏到当前 lineage。它不在同一游戏的不同存档会话之间共享，也不等同于 Project 设定或 Worldbook。引擎不内置人物关系、时间线或伏笔等内容类别。 |
| **记忆条目** | 存档记忆中可独立发现、读取和暂存更新的用户定义条目；以稳定 UUID 作为身份，`title` 可修改和重名，`tags` 是字符串列表，`content` 可为空字符串。写工具以条目 ID 对 Task Working Snapshot 执行 upsert/delete；目标 revision 的完整 Memory Snapshot 只包含仍存在的条目，删除历史由 Trace 审计而不是 tombstone 暴露。 |
| **手动记忆修订** | 玩家在 Studio 对存档记忆执行的显式存档命令；它使用 expected Session revision，成功后产生 `memory_edit` Session Commit 与新的 Memory Snapshot，但不产生故事正文、不计入 `recent_turns`，也不绕过 active-lineage、rollback 或 reroll 语义。运行中已有 Task 时该命令返回 busy，不让已冻结 Task 因外部写入变 stale。 |
| **卡片目录** | 一张卡运行时的持久目录；包含 `chat_log.json`、变量基线与 `memory/`。 |
| **世界书条目** | 卡片内按主题组织的设定正文；导入后正文存于 `memory/reference.md`。 |
| **catalog** | 世界书条目的轻量清单；每条提供标题和 usage，供叙事 agent 决定是否加载。 |
| **usage** | 一句“讲什么 + 何时读”的条目说明，类似 skill description。 |
| **MVU** | 卡作者可选的变量更新兼容语法；它只作为 `update_state` 工具的可选 `mvu` 字段被显式解析，并规范化为与原生 JSON operations 相同的工作快照修改。最终正文、Artifact、Prompt、Handoff 与历史投影均不扫描、执行或隐藏 MVU 文本。 |
| **State Snapshot** | 一个 Session revision 对应的完整存档状态结果；Turn Commit 从 Task Working Snapshot 取得最终状态并持久化，rollback/reroll 直接恢复该结果，不通过重放正文或历史命令重建。历史 revision 缺少所需 Snapshot 是必须忠实呈现的协议错误，不触发猜测、补写或静默降级。 |
| **手动状态修订** | 玩家在 Studio 对存档状态执行的显式存档命令；它使用 expected Session revision，成功后产生 `state_edit` Session Commit 与新的 State Snapshot，但不产生故事正文、不计入 `recent_turns`。运行中已有 Task 时该命令返回 busy。 |
| **Session revision** | 存档会话 active lineage 上一个已提交事实节点的身份；每个节点指向 parent revision，并对应完整 Memory/State Snapshot。它可以来自故事回合或不产生正文的显式存档命令，因此 revision 数量不等于故事回合数量。 |
| **Session Commit** | 推进 Session revision 的上位权威事实；当前分为产生正式故事正文的 Turn Commit，以及不产生正文的 `memory_edit` / `state_edit` Commit。所有类型都遵守 expected revision、active lineage、原子 Snapshot 与 rollback/reroll 语义。 |
| **回合** | 一次玩家输入从上下文准备到唯一最终 Turn Commit 的正式故事单位；只有最终交付会推进存档会话 revision。 |
| **空正文提交** | 正文是空字符串或仅含空白、但类型、revision、事务、工作快照/schema 与安全校验均通过的正常 Turn Commit；它照常推进存档会话 revision，不代表质量失败。游戏界面可显示不进入持久数据和后续上下文的诊断占位。 |
| **协作运行** | 一个回合 Task 内，多个 Agent 按用户配置的接力顺序通过上游结果、工具调用和草稿迭代完成交付的临时过程；其中的中间结果不是正式故事 revision，进程退出后不恢复。玩家通过 reroll 从最后一个正式 revision 发起新的协作运行。避免称为“多回合故事”。 |
| **入口 Agent** | 协作配置指定、接力链中首个接收玩家输入的普通 Agent Definition；它的 Instruction Prompt 与内容宏在每个 Task 的冻结快照上重新展开。它不代表内置的“编排者”或任何固定职责。 |
| **Handoff Prompt** | 用户写在一条接力连接上的提示文本；它在冻结 Task snapshot 上展开后，与前一 Agent 经其绑定 Regex Collection 转换的终止输出组合，成为下一 Agent 的上游输入。它不规定故事格式或角色职责。 |
| **接力链** | 用户配置的固定 Agent 执行顺序与连接；运行时一次只执行一个 Agent，并将每站的输出按该连接的 Handoff Prompt 交给下一站。它不从模型文本推断下一站。 |
| **接力循环** | 接力链中由用户选定、按固定次数重复执行的片段，并有明确出口。当前只支持精确次数；配置结构预留未来只设置最大次数的受限循环策略。 |
| **接力编辑器** | Studio 中以纵向顺序编辑接力链、Handoff Prompt 和重复片段的界面；它展示实际执行顺序，不是自由画布。 |
| **临时 Agent 会话** | 一个 Agent 在单次协作运行内保有的完整个人记忆，包含自身推理、工具结果和上游 handoff 输入；不对其他 Agent 直接可见，并在最终交付、取消或进程退出时丢弃。 |
| **交付 Agent** | 协作配置指定、唯一其终止输出会作为候选正文提交的 Agent Definition；输出先经过用户绑定的 Regex Collection 转换，规则未匹配时沿用原样输出。默认可与入口 Agent 相同，但职责与格式约束由用户配置。其他 Agent 只能产生协作消息或草稿，不能推进存档会话 revision。 |
| **协作预算** | 限制一次协作运行的模型调用、工具调用、接力次数、token 和时长的任务级上限；运行严格串行，同一时刻只允许一个 Agent 调用模型或工具。预算耗尽时不得提交故事正文。 |
| **任务上下文快照** | 一个回合 Task 创建时冻结的实际上下文内容；参数宏实际选中的内容物化于此，而按需历史读取固定为 `session_id + base_revision` 的 lineage 引用。宏、执行计划和只读历史查询都受其 base revision 与来源范围约束。冻结是该快照的不变量，不另设 `Frozen Context` 领域对象。 |
| **Task Working Snapshot** | 一个回合 Task 从 base revision 的 Memory/State Snapshot 复制出的单一临时工作副本；获授权工具按 Graph 串行顺序直接读取或原子修改它。它不改变任务上下文快照，不持久化、不恢复；Graph 失败、取消、stale、retry 或进程退出时整体丢弃，成功时由 Turn Commit 取得最终完整结果。 |
| **Agent 工具** | Studio 中公开给用户选择、并由 Agent Definition 的 tool allowlist 授权的运行能力。只有获配相应写工具的 Agent 才能修改 Task Working Snapshot；Graph 只安排 Agent 的执行和接力，不定义角色权限、Memory/State 业务或隐藏能力。 |
| **上下文清单** | 与任务上下文快照配套的审计事实，记录实际来源、版本、hash、预算和选择结果；它不保存未被选择的隐藏 transcript。 |
| **上下文选择器** | Prompt 参数宏或工具查询中用于从任务上下文快照选择内容的表达式；它不是独立持久对象，也不能越过 Task 的 base revision。`recent_turns:N` 的 N 按故事回合计数，结果是按时间顺序展开的 JSON LLM messages（仅 `role` 与 `content`）；开场白是一条 assistant message，正式回合通常展开为 user 与 assistant 两条 message，非故事 Session revision 被跳过。它不设框架产品上限，也不静默裁剪。`get_turns(revision)` 只读取该 Task 冻结 lineage 上的一个精确故事 revision，返回同一 messages 形态；目标是非故事 revision 时返回稳定错误，不提供范围、分页或查询语言。 |
| **runtime/harness** | 调度 agent、工具、上下文和用户输入的执行环境。当前为 Claude Code；目标是独立实现。 |
| **Agent Framework** | 调度用户定义的 Agent 团队、模型调用、工具、Artifact、Graph Run 和 Trace 的通用框架；不规定故事内容或模型输出格式。 |
| **默认协作套件** | AIRP 首次提供、供用户参考和直接修改的一组普通 Studio Library 配置。它与用户创建的配置拥有相同地位，不触发隐藏行为、fallback 或恢复机制；用户修改或删除后，系统不得覆盖或复活它。 |
| **Artifact** | Agent 节点产生的可传递结果；它是 Graph Run 的交接事实，不代表特定故事内容或文本协议。 |
| **交付 Artifact** | 唯一交付节点的终止输出经过该 Agent 的 Regex Collection 转换后形成的候选交付内容；它可以为空，属于临时协作运行，尚不是正式故事事实。 |
| **Turn Commit** | 产生正式故事正文的 Session Commit；Runtime 将交付 Artifact 与 Task Working Snapshot 中最终的 Memory/State Snapshot 原子写入存档会话。它记录 parent revision，且一个回合 Task 最多产生一个。工具调用和 before/after diff 属于 Trace，Snapshot 表示该 revision 的完整结果，rollback 不重新调用模型或重放工具。 |
| **Regex Collection** | 全局可复用的有序内容处理规则集合，用于对 Agent 的输入或输出进行替换、提取等操作；每个 Agent 可绑定零或一个 Collection。它不是角色卡导入产生的酒馆兼容正则脚本；避免称为“正则集”。 |
| **Studio 抽屉** | 从游戏顶栏向下展开、用于编辑 Studio 模块的临时工作区；所有模块共用一个宿主且互斥显示，以保留当前游玩上下文。避免称为“Studio 页面”或“独立 Studio”。 |
| **Monitor** | 游戏界面中持续观察当前存档状态和 Agent Graph 执行状态的右侧区域；它提供实时节点与调试入口，但不编辑 Studio 配置。调试浮窗可切换为仅文本视图，隐藏运行元数据。避免称为“设置侧栏”。 |

## 阅读顺序

1. [`docs/README.md`](docs/README.md)
2. [`docs/product/positioning.md`](docs/product/positioning.md)
3. [`docs/status/feature-status.md`](docs/status/feature-status.md)
4. [`docs/architecture/overview.md`](docs/architecture/overview.md)
5. 与当前任务相关的 architecture 文档和 ADR
