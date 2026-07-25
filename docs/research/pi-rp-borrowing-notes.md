# 借鉴备忘：pi-rp 的设计值得 AIRP 参考

- **状态**：备忘 / 未决策（不是 ADR，不约束当前票）
- **日期**：2026-07-25
- **来源**：`2722550596/pi-rp`（Pi coding-agent 的深度魔改分支，作者 `plan.md`）
- **用途**：在 ADR-0007（Ticket 04 Session Event Stream）已落定后，作为未来架构讨论的输入。下面每条都是「pi-rp 已验证可行 + AIRP 当前缺或做得更硬」的方向，**不等于要照搬**。

> 前提不变（ADR-0001/0004）：AIRP 仍自拥 session/event/task/commit/projection，Pi 只作候选执行层。借鉴只针对「AIRP 自己该补的能力」，不是去绑定 Pi 内部数据结构。

## 对照结论（一句话）

pi-rp 证明了 Pi 的 agent loop / typed tools / provider 抽象**足够做 RP**，但它的所有问题域（reroll 消息粒度、abort 失效、compact 丢上下文、状态随 transcript 漂移）正是 ADR-0001/spec 担心、AIRP **显式绕开**的东西。两条路线方向相反：pi-rp 深度绑定 Pi 内部、住进 Pi CLI；AIRP 自建 runtime、把 Pi 隔在 adapter 后。借鉴只取它的**功能形态**，不取它的**架构耦合**。

## 1. PromptPreset 的 slot + 宏模型（高价值，可能改变 Manifest 形态）

**pi-rp 做法**：12 个内置 slot（tools / project-context / skills / date-cwd / variables / chat-history …）+ 宏引擎（`{{date}}`/`{{time}}`/`{{cwd}}`/`{{user}}`/`{{tools}}`/`{{activeModel}}` + 自定义宏注册）+ `compileSystemPrompt()`/`compileMessages()` + `/prompt` 实时查看编译后完整 provider 负载。preset 可 replace/append/prepend，`.pi/prompt-presets/*.json` 加载、ID 去重、autoActivate。

**AIRP 现状**：Ticket 02 的 Context Manifest 是**固定 11 section**（narrative_policy / card_facts / settings / worldbook_catalog / card_structure / variable_baseline / current_state / recent_memory / recent_turns / worldbook_entries / player_input）。section 顺序、内容来源硬编码在 `context_compiler.py`。

**值得讨论**：CLAUDE.md 已要求「文风/NSFW/防抢话/人称 可独立启停、同维度互斥」的模块化指令。当前靠 `settings.json` + `profiles/{style}.md` + 硬编码 section 拼，不够灵活。slot/宏能让「切文风」「关 NSFW」「注入临时指令」变成 Manifest 层的声明式组合，而非改 compiler 代码。
- **红线**：AIRP 的 Manifest 已经是确定性、可哈希、带 provenance 的。slot/宏不能破坏这三条——宏展开必须在**编译期**完成并计入 `content_hash`，运行期不可变；slot 的启用/排序本身要进 stable payload hash。

## 2. Compact + Recall（长会话上下文治理）

**pi-rp 做法**：保留 Pi 原生 compact，但要求「压缩后内容必须可检索，不能真丢」，提供 `recall` 工具检索已 compact 的内容。

**AIRP 现状**：Ticket 02 只有 `recent_memory` 的确定性截断（`_fit_budget` / `_fit_payload_budget`），截掉就没了。长会话里早期剧情细节会永久退出 active context。

**值得讨论**：AIRP 已有 SQLite event store + revision snapshot + 跨会话 `memory/project.md`。可加一层「summarize 旧 revision 段 → 存为可检索 recall 候选 → 提供 `recall` typed tool（加入 Ticket 03 的封闭 allowlist）」。这比 pi-rp 挂 session jsonl 更干净——权威源仍是 AIRP 自己的 event store，recall 候选带 source revision。

## 3. state_update / 状态可见性的工具形态（基本不用借鉴，仅记录）

**pi-rp 做法**：`state_update(path, op, val)` 工具 + `/state` 命令查看，状态挂 session jsonl。

**AIRP 现状**：已有 `validate_state_proposal`（dry-run）+ `commit_turn_draft`（唯一写工具，含 MVU/schema 严格校验，Ticket 05）。**AIRP 版本更严格**：写权限只挂在 commit 边界，不开放运行期任意改状态。

**结论**：设计更强，无需动作。唯一可参考的是「玩家/调试者直接看当前状态」的命令面（`/state` 等价物），对玩家实时编辑（spec Planned 项）有用。**警惕**：pi-rp 把状态挂 session jsonl = 把权威交给 Pi session 文件，正是 ADR-0001 要避开的漂移——AIRP 不能这么做。

## 4. 两阶段「改状态 → 启 run」提交（AIRP 已暗合，供 Ticket 06 主动遵循）

**pi-rp 教训 2**：`reroll()` 第一版把「改 session state」和「启动 agent run」捆在一个方法里，UI 没机会在中间刷新，旧 trace 残留。解法：拆两阶段——先只改 session state，再启 run，中间留给 UI 同步。作者称这是「所有改变当前路径操作的标准模式」。

**AIRP 现状**：Ticket 03/05 已把 **commit（改权威 state）和 projection（前端投影）分离**——durable commit/revision 先落，projection 后做且幂等可重试，失败不伪装成功。同一洞察的另一个面。

**结论**：已做对。记录此条是为了让 **Ticket 06（reroll/rollback）实现时主动遵循**：reroll = 先移动 active head（state 层），再让 projection/SSE 跟随，中间留事件 seam，不要把两者捆在一个不可中断的调用里。

## 5. 「动手前读完整运行时链路」的元教训（验证 seam 价值）

**pi-rp 教训 1**：`/reroll` 的大半问题（消息粒度、prompt/continue 选择、abort 失效、post-agent-run 循环缺失）都因只读了 session-manager.ts 存储层，没完整读 `agent-loop.ts` / `agent.ts` 的事件循环。

**对 AIRP 的映射**：这正是 ADR-0004「Pi 是候选执行层、领域层不依赖 Pi 类型」要规避的——pi-rp 深度绑定 Pi 内部（`runAgentLoop` vs `runAgentLoopContinue` 的 emit 差异），所以改 RP 功能必须先吃透 Pi 运行时。AIRP 通过 `ProviderAdapter` / `DirectorHandle` seam 把这层隔开，Ticket 03 的 abort 契约是自定义 `AbortSignal`，不依赖 Pi 内部循环。

**结论**：验证了当前 seam 设计。Ticket 07 接真实 Pi sidecar 时，仍要把「Pi 事件循环差异」挡在 adapter 内，不渗入领域层。

---

## 讨论时机

- 本备忘**不是 ADR**，不约束任何当前票。
- 第 1、2 条是真正可能改变 AIRP 架构的（Manifest slot 化、recall 层），应在 Ticket 06/07 落定、真实 DeepSeek E2E 之前讨论，避免改两次。
- 第 3、4、5 条是「已做对 / 保持警惕」，无即时行动项，仅作记录。
