## Problem Statement

AIRP 当前已经能完成角色卡导入、世界书按需加载、MVU 状态更新、记忆沉淀和浏览器回合交互，但玩家每次输入仍由 Claude Code session、`ScheduleWakeup`、长轮询和文件信号驱动。对玩家来说，这意味着游戏是否能继续运行取决于一个外部 coding-agent 会话是否仍活着；等待、取消、失败恢复和重连缺少产品自身的明确语义。

更重要的是，AIRP 只能主动生成一份回合上下文，却不能完整决定模型最终看到的内容。Claude Code 的追加式 transcript 会继续携带旧对话、文件读取、工具结果和曾经加载过的世界书内容。维护者无法准确回答“这一轮模型看到了什么、为什么看到了它”，也无法可靠控制 token 成本、重放一次生成或在多个写作职能 Agent 之间实施不同的上下文权限。

本次改造需要先完成一个可运行的 tracer bullet：用 Pi Agent Core 和 Pi AI 替换 Claude Code 的 Agent loop/provider 承载，让 AIRP 自己拥有输入事件、任务状态、上下文编译、模型调用、唯一回合提交和运行 telemetry。它必须继续兼容现有角色卡、世界书、MVU、记忆和前端主路径，同时为后续“写作团队职能分工型”多 Agent 架构建立正确的 session、proposal 和 commit 基础。

## Solution

建立一个由 AIRP 自己控制的、单 Session、单叙事导演 Agent 的本地 runtime。浏览器提交玩家消息后，API 将其保存为不可变的 Session Event，并创建可恢复的 narrative task；worker 直接调用 Pi Agent Core，不再依赖 Claude Code 唤醒或轮询。

每次模型调用前，AIRP 的 Context Compiler 从已提交的 session revision、角色卡、当前世界状态、近期有效回合、记忆、设置和世界书 catalog 中重新构造有限上下文，并保存可审计的 Context Manifest。叙事导演只能通过少量 typed domain tools 读取世界书、检查状态和提交结构化回合草稿；它不能直接修改卡片文件、聊天记录或正式世界状态。

结构化草稿通过质量和 MVU 验证后，由唯一的 Turn Commit Service 以 optimistic revision 提交。提交成功后，再生成现有前端所需的兼容投影。流式文本只是预览，只有 commit 成功的回合才是玩家事实。stop、reroll 和 rollback 都通过显式 task/revision 语义实现，不再依赖删除历史或重新触发 pending 文件。

首个版本只包含一个“叙事导演”制作职能，不实现每个角色一个 Agent，也暂不实现多写作职能协作。Pi 是候选 Agent Execution Layer；AIRP 仍拥有 session、event、task、context、commit、projection 和 observability。

## User Stories

1. As a player, I want my submitted action to start processing immediately, so that gameplay does not depend on an external Claude Code polling session.
2. As a player, I want to see clear queued, generating, committing, completed, cancelled, and failed states, so that I know what the engine is doing.
3. As a player, I want narrative text to stream as a preview, so that long generations feel responsive.
4. As a player, I want only successfully committed text to become part of the story, so that partial or failed output cannot corrupt my save.
5. As a player, I want to stop a running generation, so that I can interrupt an unwanted direction without waiting for the model to finish.
6. As a player, I want stopping a generation to leave the last committed turn unchanged, so that cancellation is safe.
7. As a player, I want repeated clicks or retried network requests to create only one input event, so that accidental duplicate turns do not occur.
8. As a player, I want my browser to reconnect and recover current task status and committed output, so that a page refresh does not lose the turn.
9. As a player, I want a failed provider request to produce a visible recoverable error, so that the game does not silently hang.
10. As a player, I want retryable failures to be retried within a bounded policy, so that temporary model errors do not require manual save repair.
11. As a player, I want a reroll to reuse the same original player input, so that only the assistant outcome changes.
12. As a player, I want reroll history to remain auditable while only the newest branch is active, so that experimentation does not destroy prior facts.
13. As a player, I want rollback to move the active story head to a selected revision, so that subsequent input continues from exactly that point.
14. As a player, I want rollback to update chat, variables, memory-facing projections and frontend state consistently, so that no future-turn residue remains active.
15. As a player, I want current role cards and worldbooks to remain usable, so that runtime migration does not invalidate existing saves.
16. As a player, I want the current browser turn experience and action options to remain recognizable, so that the migration does not require relearning the product.
17. As a player, I want story settings from my local card and worldbook to remain authoritative, so that the runtime cannot silently replace my canon.
18. As a player, I want the narrative director to load only relevant worldbook entries, so that context remains focused and affordable.
19. As a player, I want invalid MVU changes to be rejected before commit, so that model mistakes cannot damage persistent state.
20. As a player, I want a quality-gate retry to avoid creating duplicate turns, so that retries are invisible in committed history.
21. As a maintainer, I want AIRP to own the user-input event loop, so that runtime liveness no longer depends on `ScheduleWakeup` or long polling.
22. As a maintainer, I want AIRP to own durable task state, so that process restarts have explicit recovery semantics.
23. As a maintainer, I want every task to have a correlation ID and causation chain, so that one player command can be traced through model calls, tools and commit.
24. As a maintainer, I want every model call to reference a Context Manifest, so that I can inspect exactly what was supplied to the model.
25. As a maintainer, I want Context Manifests to include source references, versions, hashes, inclusion reasons and budgets, so that context behavior is reproducible.
26. As a maintainer, I want old Pi execution messages excluded unless selected by Context Compiler policy, so that transcript drift cannot reappear under a different library.
27. As a maintainer, I want tool results governed by an explicit retention policy, so that large worldbook entries do not leak into every later call.
28. As a maintainer, I want stable prompt sections separated from dynamic sections, so that provider-side caching can be evaluated without changing semantics.
29. As a maintainer, I want provider selection behind an AIRP-owned adapter, so that Pi types and provider differences do not spread into domain code.
30. As a maintainer, I want model selection centralized by role and capability, so that later writing-team Agents can use different models without rewriting workflows.
31. As a maintainer, I want provider-reported usage recorded per model call, so that token accounting no longer depends on Claude Code transcript parsing.
32. As a maintainer, I want Pi catalog cost recorded explicitly as an estimate with a rate version, so that it is not confused with provider billing.
33. As a maintainer, I want latency measured at the AIRP request boundary, so that performance telemetry is independent of Pi message fields.
34. As a maintainer, I want typed domain tools with schema validation, so that the model cannot write arbitrary files or bypass state validation.
35. As a maintainer, I want only one tool/service to commit a turn, so that model retries and parallel tool calls cannot produce multiple state writes.
36. As a maintainer, I want optimistic revision checks on every commit, so that stale generations cannot overwrite newer player actions.
37. As a maintainer, I want committed domain facts separated from rebuildable frontend projections, so that projection failures can be repaired without regenerating story content.
38. As a maintainer, I want compatibility projection writes to be idempotent by commit ID, so that recovery cannot duplicate chat entries or counters.
39. As a maintainer, I want runtime configuration and secrets outside card data and logs, so that provider credentials are not exposed through saves or traces.
40. As a maintainer, I want a feature-controlled migration path, so that the legacy runtime remains available during validation without becoming the new implementation’s fallback inside a task.
41. As a maintainer, I want an experimental ADR before implementation, so that the candidate boundary and message/commit protocol are explicit without prematurely accepting Pi.
42. As a maintainer, I want a separate final selection ADR after real DeepSeek validation, so that adoption is evidence-based.
43. As an agent developer, I want a deterministic fake narrative executor, so that runtime behavior can be tested without paying for or depending on a real model.
44. As an agent developer, I want scripted model and tool responses, so that abort, retry, malformed drafts and provider errors are reproducible.
45. As an agent developer, I want context compilation test fixtures, so that worldbook selection and token-budget changes are visible in review.
46. As an agent developer, I want one high-level Session Turn Runtime Contract, so that tests protect user behavior rather than Pi internals.
47. As an agent developer, I want the existing engine behind a narrow adapter, so that card, MVU and render compatibility can be reused during migration.
48. As an agent developer, I want errors to carry stable categories and retryability, so that tests and UI do not depend on provider-specific strings.
49. As an operator, I want startup recovery to classify leased or running tasks after a crash, so that no task remains permanently stuck.
50. As an operator, I want task attempts and terminal failures retained, so that production-like failures can be diagnosed locally.
51. As an operator, I want active-run cancellation and process shutdown to propagate through abort signals, so that the service stops cleanly.
52. As an operator, I want health and readiness to distinguish API availability from provider readiness, so that the UI can show actionable status.
53. As a future coordinator developer, I want Agent Run to include an explicit writing-team role, so that later narrative, world simulation, character-consistency and style-editing tasks can remain separate.
54. As a future coordinator developer, I want state changes to use proposal and commit semantics from the first version, so that adding more writing-team Agents cannot introduce direct write races.
55. As a future coordinator developer, I want story characters to remain domain objects rather than Agent identities, so that multi-Agent means production-role collaboration, not one Agent per character.

## Implementation Decisions

1. **The first deliverable is a tracer bullet, not the complete future runtime.** It supports one local card, one active Session and one narrative-director Agent. It must traverse the real browser-to-commit path and preserve existing gameplay behavior.

2. **Pi is an execution dependency, not the domain runtime.** Pi Agent Core owns a short-lived Agent loop, typed tool dispatch, streaming lifecycle and abort propagation. Pi AI supplies the provider/model abstraction. AIRP owns durable events, tasks, context policy, state validation, commit, projection and recovery.

3. **Pi coding-agent CLI is not embedded.** The runtime uses the library packages directly. It does not inherit the coding CLI’s JSONL session, terminal commands, project resource discovery, built-in file-writing tools or compaction policy.

4. **The current package namespace is used and versions are pinned.** The implementation uses the maintained `@earendil-works` packages, records exact versions and fixes the supported Node.js runtime in development and CI.

5. **An experimental ADR precedes implementation.** It authorizes a Pi candidate experiment, fixes the initial Agent message vocabulary, proposal/commit authority and adapter boundary, and explicitly states that Pi is not yet the accepted product runtime. A separate selection ADR follows the prototype evidence.

6. **The runtime uses explicit domain objects.** At minimum it defines Session, Session Event, Task, Agent Run, Model Call, Context Manifest, Tool Run, Turn Draft, State Proposal, Turn Commit and Projection Checkpoint. Every object carries stable IDs; causal objects also carry correlation and causation IDs.

7. **The local durable store is SQLite for the tracer bullet.** Session events, tasks, runs, manifests, model telemetry, commits and projection checkpoints are transactional and queryable. Card assets may remain in their existing format during migration, but pending work and authoritative runtime history do not live in transient signal files.

8. **A Session has one serialized command stream.** Submit, stop, reroll and rollback commands are accepted with idempotency keys and ordered per Session. At most one narrative task may own the Session’s generation lease. Stale commits fail optimistic revision checks.

9. **The Task lifecycle is explicit:**

   ```text
   queued → leased → running → validating → committing → projecting → succeeded
                  │         │             │             │
                  ├─────────┴─────────────┴─────────────┴→ cancelled
                  ├→ failed_retryable → queued
                  └→ failed_terminal
   ```

   Runtime restart converts abandoned leased/running tasks into a defined recoverable state according to attempt policy. It never leaves an invisible pending file as the only evidence of work.

10. **The API is command- and event-oriented.** Browser commands submit player messages, cancel tasks, request reroll and move the active revision head. Read endpoints expose Session snapshot, current Task state and events after a sequence number. SSE is the initial server-to-browser transport; reconnect resumes from the last event ID.

11. **Legacy browser compatibility is preserved through an adapter.** The current local web UI may continue consuming its existing rendered artifacts during the tracer bullet, but the authoritative completion signal comes from the new Task/Turn Commit lifecycle. Compatibility endpoints cannot recreate pending-file orchestration.

12. **Context Compiler is AIRP-owned and runs before every model call.** It receives Session revision, writing-team role, model capability and token budget. It selects explicit sections and emits both model messages and a persisted Context Manifest.

13. **A Context Manifest records what was included and why.** It contains section type, source identity and version, content hash, inclusion reason, ordering, estimated size/tokens, truncation or summary decisions, tool policy version, model/provider identity and the active Session revision. A separate manifest exists for each model call in a multi-turn tool loop.

14. **Pi messages are execution-scoped.** An Agent Run starts from Context Compiler output and does not resume an unbounded Pi transcript. Prior conversation, tool output and summaries enter a later call only through an explicit context policy.

15. **The first Context Compiler policy preserves current product semantics.** It supplies narrative system policy, card facts, style and safety settings, worldbook catalog, current state/variable paths, recent active-branch turns, current memory summary and the current player input. Stable and dynamic sections remain distinguishable.

16. **Worldbook remains skill-style and on demand.** The model sees `title — usage` catalog entries and may call a typed `load_worldbook_entry` read tool using the exact title. The runtime enforces the per-run entry limit and records selected entry identity/version in subsequent manifests. No automatic full-worldbook injection is reintroduced.

17. **The Agent receives a minimal tool allowlist.** Initial read tools expose Session snapshot, recent memory and worldbook entry content. Validation tools may check a State Proposal. A single commit tool accepts a structured Turn Draft with expected revision. No generic Bash, arbitrary filesystem write or unrestricted network tool is available.

18. **The Turn Draft is structured.** It separates optional polished input, narrative content, summary, exactly three player action options, proposed MVU operations and metadata needed by quality gates. Compatibility formatting is produced after validation rather than parsed as the primary domain contract.

19. **Tool parameters are schema-validated before domain work.** Invalid tool arguments return a stable validation error to the Agent. Tool hooks record invocation, block unauthorized operations and redact sensitive values from trace payloads.

20. **Only Turn Commit Service writes authoritative story state.** The model and Pi tool implementation can propose a draft but cannot directly append chat history, alter variables, update memory or generate final frontend state. Commit validates expected revision, quality gates, MVU schema and proposal consistency.

21. **A draft may be retried but committed at most once.** Quality or schema rejection is returned to the active Agent Run for bounded correction. Each Task has a unique commit key; duplicate commit calls return the existing result. Exhausted retries fail without changing the active story head.

22. **Existing engine logic is reused behind a narrow Legacy Engine Adapter.** The adapter provides snapshot/context inputs, MVU validation/application and render projection without exposing internal file layouts to the TypeScript runtime. New orchestration does not duplicate MVU or rendering rules.

23. **Committed facts and compatibility projections are distinct.** A successful Turn Commit advances the Session revision transactionally. Chat-log, state, memory-facing and frontend artifacts are rebuildable projections associated with the commit ID. Projection operations are idempotent and retryable.

24. **The UI sees a turn as final only after required projections succeed.** Streaming deltas are preview events. If validation, commit or projection fails, the UI clearly marks the preview as uncommitted and keeps the previous committed revision active.

25. **Stop uses abort semantics, not steering.** A stop command cancels a queued Task or propagates an AbortSignal into the active Pi/provider/tool execution. Partial output is retained only in trace as non-authoritative preview. `steer()` may later support queued correction after a tool batch, but is not treated as immediate interruption.

26. **Reroll creates a new branch revision.** It references the original player-message event and the pre-response parent revision, starts a new narrative Task and supersedes the prior assistant branch only when the new Turn Commit succeeds. The old branch remains auditable.

27. **Rollback moves the active head rather than deleting events.** It selects an existing committed revision, invalidates/rebuilds active projections from that head and makes the next player message descend from it. Token/cost telemetry remains historical audit data, while active-story projections exclude superseded future turns.

28. **Memory obeys active revision semantics.** Memory candidates and summaries reference source revisions. Reroll or rollback cannot leave future-branch facts in the active Context Compiler view. The tracer bullet may rebuild or invalidate summaries rather than trying to mutate prose in place.

29. **Provider access is wrapped by an AIRP Provider Adapter.** Domain and coordinator code do not depend on Pi model types. The adapter centralizes model lookup, credentials, timeouts, aborts, retry classification, capability checks and normalized results.

30. **The first real provider contract targets the project’s DeepSeek route.** It verifies streaming, long Chinese narrative output, tool calling, structured arguments, abort, stop reasons, normalized usage and error behavior. Provider differences remain explicit rather than assumed away by Pi.

31. **Telemetry distinguishes reported and calculated values.** Provider-reported usage is persisted. AIRP measures latency around each request. Cost is stored as a Pi catalog-rate estimate with currency and rate-version metadata; it is not labeled as provider billing.

32. **A centralized Model Policy selects by writing-team role.** The first policy contains only narrative director. Its shape already supports later world simulation, character-consistency editing, style editing, research and card diagnosis roles choosing different models and budgets.

33. **Multi-Agent means writing-team production roles.** Story characters and NPCs remain entities in shared world state. The initial schema must not create Agent identity, transcript or model assignment per character.

34. **Runtime events are durable; streaming is a projection.** Each event has a monotonic Session sequence. SSE consumers can reconnect from a sequence/event ID and reconstruct Task progress without consulting process memory.

35. **Error categories are stable and user-actionable.** At minimum: invalid command, stale revision, context compilation failure, provider unavailable, provider rejected, tool validation failure, quality exhaustion, commit conflict, projection failure, cancelled and terminal internal failure. Retryability is data, not inferred from text.

36. **Secrets are process configuration.** Provider credentials are loaded from the environment or an existing secret mechanism, never stored in Session Events, card folders, Context Manifests, tool traces or browser payloads. Logs redact credentials and sensitive headers.

37. **Migration is feature-controlled at Session creation or service startup.** A Session is assigned either legacy or Pi runtime before processing. A failed Pi Task does not silently fall back to Claude Code because that would create duplicate or divergent writes. Rollback to legacy is an explicit operator action before a new command.

38. **The legacy runtime remains temporarily available but is deprecated for new validation Sessions.** Completion of the tracer bullet does not immediately delete legacy scripts; removal occurs only after the contract suite and real browser/DeepSeek golden path pass.

39. **Documentation changes are part of implementation.** The runtime architecture, feature status, technical debt, roadmap and ADRs must distinguish proven behavior from candidate design after every tracer-bullet slice.

## Testing Decisions

1. **The primary seam is one black-box Session Turn Runtime Contract.** Tests issue the same external commands as the browser and observe durable events, active Session snapshot, committed domain turn and frontend-compatible projection. They do not assert Pi internal methods, private queues, SQLite table layout or intermediate files.

2. **The contract uses a deterministic fake narrative executor.** It can stream fixed deltas, request typed tools, return a valid or invalid Turn Draft, block until aborted, fail with classified provider errors and expose scripted usage. Most tests run without network access or model credentials.

3. **Normal-submit contract:** one idempotent player command creates one Task, emits ordered progress events, compiles an inspectable Context Manifest, commits exactly one turn, advances revision once and produces consistent chat/state/frontend projections.

4. **Duplicate-submit contract:** repeated command delivery with the same idempotency key returns the same event/task identity and cannot create a second model run or commit.

5. **Quality-retry contract:** an invalid first draft may be corrected within the same Task; only the final valid draft is committed. Retry exhaustion leaves revision and projections unchanged.

6. **Abort contract:** cancelling queued and running Tasks reaches a terminal cancelled state, propagates abort to the executor, emits no Turn Commit and leaves partial preview uncommitted.

7. **Provider-failure contract:** retryable failures follow bounded attempts and recover after restart; terminal failures remain inspectable and do not mutate story state.

8. **Crash-recovery contract:** simulate process death after event append, during model execution, after commit and during projection. Recovery must avoid lost commands, duplicate commits and duplicate projection entries.

9. **Optimistic-conflict contract:** a stale Task cannot commit after another command advanced the Session revision. The stale result is classified and remains non-authoritative.

10. **Reroll contract:** reroll reuses the exact original player input, preserves the prior branch for audit, activates only the new successful commit and keeps every active projection aligned with the new head.

11. **Rollback contract:** moving to a selected revision removes later turns, state deltas and memory candidates from the active view; a subsequent player command descends from the selected head while historical events and usage remain available for audit.

12. **Reconnect contract:** an SSE client reconnecting from its last event ID receives every later event once in order and can recover the final committed snapshot.

13. **Context ownership contract:** manifests contain only policy-selected active-branch sources. Superseded turns, stale memory, prior unselected worldbook tool results and arbitrary Pi execution history are absent.

14. **Worldbook contract:** catalog usage is present, exact-title tool loading returns the correct version, per-run limits are enforced and loaded entry provenance appears in the next model-call manifest.

15. **MVU/commit contract:** malformed operations, prohibited paths, stale base revisions and engine validation errors cannot advance Session revision. Valid operations advance state once and render the resulting values.

16. **Projection idempotency contract:** applying the same commit projection repeatedly produces byte-/structure-equivalent active artifacts without duplicate turns or counters.

17. **Telemetry contract:** each model call records normalized usage, AIRP-measured latency, stop reason and a rate-versioned cost estimate; secrets and raw authorization headers never appear.

18. **Provider Adapter contract:** use a fake Pi provider to verify stream assembly, tool arguments, abort, timeout and error classification. Tests target AIRP adapter behavior, not Pi’s own implementation.

19. **Real DeepSeek E2E is opt-in but required for selection.** Against a dedicated test card, verify Chinese long-form streaming, worldbook tool use, structured Turn Draft, MVU proposal, usage, abort and one full browser-visible commit. It is not part of every fast test run.

20. **Browser golden-path smoke:** start the local services, submit one action, observe streaming state, receive the committed turn, reroll it, roll back and submit a new branch. Browser behavior is checked at the user-visible seam.

21. **Compatibility fixtures cover representative existing assets.** Include at least one JSON/PNG-derived card with worldbook, nested MVU variables, beautify/render settings, memory and multiple openings. Fixtures contain no private player data.

22. **Tests assert external invariants, not file placement.** Good tests prove one command/one commit, active-head consistency, no partial writes, recoverability and explicit context. They do not freeze module names or implementation-specific serialization.

23. **There is no prior automated project test suite to copy.** Existing manual import → opening → round preparation behavior and byte-equivalence checks become fixtures, but this contract suite establishes the first durable testing convention.

24. **Documentation verification is automated.** Internal links, maturity labels and ADR references must pass; a Planned capability cannot be presented as Proven before its real E2E gate succeeds.

## Out of Scope

- Multiple concurrent player Sessions or multiple active cards in one process.
- The full filesystem/data-layout migration that eliminates every legacy global artifact.
- Writing-team multi-Agent orchestration beyond reserving role, proposal and commit concepts.
- One Agent per story character or NPC; this is explicitly not part of the product model.
- Separate world-simulation, character-consistency, style-editing, research or card-diagnosis Agents.
- Autonomous Web Search and Source Record adoption workflow.
- Player real-time editing of cards, worldbooks, story settings or variables.
- Cloud deployment, distributed queues, horizontal workers, multi-host leases or high availability.
- Authentication and multi-user authorization beyond preserving the current local-only security boundary.
- Replacing all Python engine modules with TypeScript.
- Redesigning the browser visual language or adding unrelated frontend features.
- Guaranteeing semantic equivalence across every provider; only the DeepSeek route is a selection gate.
- Treating Pi catalog cost estimates as provider invoices.
- Deleting the legacy Claude Code runtime before the Pi tracer bullet passes all acceptance gates.

## Further Notes

- The implementation should be split into blocker-aware tracer-bullet tickets after this spec. Suggested dependency order is: experimental ADR and contracts; deterministic runtime harness; context/manifest; Pi/DeepSeek adapter; commit/projection integration; browser command/event migration; reroll/rollback and recovery; final E2E and selection ADR.
- The current reroll/rollback implementation mutates and truncates legacy projections rather than preserving branch/revision semantics. This spec intentionally defines the desired external behavior instead of preserving those implementation details.
- The research conclusion remains conditional: Pi’s exposed context, tool and lifecycle seams are sufficient to justify a prototype, but they do not prove DeepSeek compatibility or provide AIRP’s durable runtime automatically.
- “Ready for agent” means the feature intent and observable contract are specified. It does not mean every ticket can be implemented independently before `/to-tickets` defines blocking edges.
- Completion requires evidence for both the deterministic Session Turn Runtime Contract and the opt-in real DeepSeek/browser path. Only then may the formal selection ADR mark Pi accepted or rejected.
