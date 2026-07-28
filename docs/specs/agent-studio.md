# Spec: Agent Studio And Debuggable Linear Graph Runtime

## Problem Statement

AIRP 玩家目前只能通过零散的运行时配置和原始 JSON 编辑 Provider、模型与 Graph，无法独立管理多个 LLM Provider、为每个 Agent 配置模型与提示词，或可靠观察多 Agent 的实际执行。现有 Agent Trace 是平铺事件，不能展示 Graph 的实时流转，也不能让玩家检查某个 Agent 实际收到的输入、使用的模型参数、工具结果和输出。角色卡与世界书也缺少统一的可视化编辑与跨 Project 复用工作流。

结果是创作者无法判断提示词、上下文、模型配置或世界书为何产生某次输出，AIRP 对他们而言仍是不可调试的黑盒。

## Solution

AIRP 提供一个独立宽屏 Agent Studio 和一个由 Runtime Studio HTTP/SSE 合约承载的可调试线性 Graph Runtime。Studio 管理全局可复用的 Provider Profile、Agent Definition、Graph Definition 与 Worldbook Definition；AIRP Project 管理规范化角色卡、Openings、变量、素材、Graph 选择与多个世界书绑定。

玩家在 Studio 保存配置后回到游戏视图提交回合。Runtime 将当前 Project 与 Agent/Graph 定义编译为不可变 Execution Plan，顺序执行 Graph Node，并在右侧栏显示极简的实时节点流转。每个 Node Run 可以打开完整调试详情，显示最终 prompt、有效模型参数、输入、流式输出、工具调用和 Artifact；用户还可以在不影响故事的前提下执行 Debug Replay。第一阶段保持线性 Graph、当前与最近一次 Trace、简单世界书文件和用户驱动的完整重试。

## User Stories

1. As a player, I want to open a dedicated Studio, so that complex configuration does not crowd the game view.
2. As a player, I want to create and edit multiple Provider Profiles, so that different Agents can use different LLM services.
3. As a player, I want to select either Responses or Chat Completions for a Provider Profile, so that compatible providers and DeepSeek can both be used correctly.
4. As a player, I want to save an API Key without seeing it returned later, so that I can configure a provider without exposing the secret in ordinary UI data.
5. As a player, I want Provider Profiles to fetch models automatically and allow a manual model ID, so that providers without a model-list endpoint remain usable.
6. As a player, I want failed model discovery or connection tests to explain the provider error without preventing configuration from being saved.
7. As a player, I want to create reusable Agent Definitions, so that a writing team can be shared across Projects and Graphs.
8. As a player, I want to edit an Agent name and instruction directly, so that I can give each Agent an understandable identity and writing behavior.
9. As a player, I want every Agent to edit one instruction template with runtime macros plus its Provider Profile, model and generation parameters, so that its behavior and context assembly are independently debuggable.
10. As a player, I want visual controls for basic generation parameters and a raw advanced JSON object, so that I can use common settings simply and provider-specific settings when needed.
11. As a player, I want invalid advanced provider parameters to produce visible provider errors, so that I can debug my own raw configuration instead of having it silently changed.
12. As a player, I want each Agent Definition to own its tool allowlist, so that tool access is intentional and visible.
13. As a player, I want a compiled prompt preview, so that I can inspect the instruction template, expanded macros, Runtime input and handoff data that combine into the final prompt.
14. As a player, I want to create a visual linear Graph from Agent Definitions, so that I can control execution order without editing raw JSON.
15. As a player, I want the same Agent to appear multiple times in a Graph, so that it can revise work after another Agent has reviewed it.
16. As a player, I want to choose an explicit output node, so that the Runtime knows which Artifact becomes the story draft.
17. As a player, I want to send a game message and see Graph nodes activate in order, so that I know the writing team is actually running.
18. As a player, I want each node to show only idle, running, succeeded or failed, so that Graph status remains easy to scan.
19. As a player, I want a failed node highlighted and the entire Graph Run stopped, so that failure semantics are predictable.
20. As a player, I want a retry action to rerun the complete Graph with my latest saved configuration, so that I can fix a prompt or model setting before retrying.
21. As a player, I want to click any started node and inspect complete input, effective configuration, prompt sources, tools, model calls, output and Artifact, so that I can debug my creative configuration.
22. As a player, I want a running node detail view to receive streaming output, so that I can inspect generation before completion.
23. As a player, I want current and most recent Graph Run details to survive refresh and restart, so that a debugging session is not lost.
24. As a player, I want to run a Debug Replay with frozen prior input and current Agent configuration, so that I can compare prompt or model changes without changing the story.
25. As a player, I want Debug Replay tools to read the original read-only snapshot, so that the comparison isolates changes in Agent configuration.
26. As a player, I want to edit normalized character-card fields and Openings in a Project view, so that imported card data becomes practical to maintain in AIRP.
27. As a player, I want multiple Openings without SillyTavern-specific terminology, so that alternate starts remain useful but simple.
28. As a player, I want to import, create, edit, copy and export Worldbook Definitions, so that general setting and style material can be shared across character Projects.
29. As a player, I want a Project to bind multiple Worldbook Definitions while all of its Sessions share those bindings, so that one story can combine character-specific and general reference material.
30. As a player, I want a saved shared Worldbook edit to affect all bound Projects on their next Graph Run, so that reusable material stays simple.
31. As a player, I want duplicate Worldbook entry titles renamed automatically with a copy suffix, so that exact loading remains unambiguous without namespace management.
32. As a player, I want deletion blocked when a Provider, Agent, Graph or Worldbook is still referenced, so that shared configuration is not accidentally broken.

## Implementation Decisions

- The highest acceptance seam is the Runtime Studio HTTP/SSE contract. It owns Studio CRUD, Graph Run submission, lifecycle streaming, Node Run detail retrieval and Debug Replay. Browser flows consume that contract rather than private persistence formats.
- Studio has five primary views: Providers, Agents, Graphs, Projects and Worldbooks. The game right sidebar is an observer for the active or most recent Graph Run, not a configuration editor.
- Studio Library contains reusable Provider Profile, Agent Definition, Graph Definition and Worldbook Definition objects. AIRP Project contains normalized card content, Openings, variables, assets, a selected Graph, Worldbook Bindings and multiple Sessions.
- Provider Profile supports the `responses` and `chat_completions` formats. Both map through one Provider Adapter contract to common request, streaming event, final result and error values. A selected profile can be saved when discovery fails and always allows a manual model ID.
- API Keys are isolated through a SecretStore and saved to a local secret file in phase one. Keys never enter definitions, Execution Plans, events, traces, databases, logs or exports, and are never returned after write.
- Agent Definition owns a stable ID, display name, one editable instruction template, default provider/model, generation configuration, unvalidated advanced parameter object and tool allowlist. The template can use deterministic dotted macros from the frozen Project/Session/runtime context. It has no runtime role field.
- Advanced parameters are forwarded without schema validation but cannot override Runtime-owned input, model identity, tools, stream control, authentication, Base URL or tracing fields. They take precedence over same-named basic controls and the effective configuration must make that visible.
- Graph Definition is linear in phase one. Graph Node references an Agent Definition, has a stable node ID and optional label, and may make limited provider/model/generation overrides. Tool access cannot be overridden by a Graph Node. An explicit output node must be the final enabled node.
- Definition data is compiled into an immutable Execution Plan at Graph Run start. The plan freezes Project content, Worldbook content, the macro-expanded Agent instruction, Agent data, effective provider/model configuration and tool permissions for that run.
- Graph Runtime schedules nodes, state transitions, termination and Agent Artifact handoff. Node Runner executes one resolved node and does not know Graph topology. Graph Runtime does not call a concrete Provider.
- Nodes exchange immutable Agent Artifacts through a typed Node Result. Only the output node Artifact enters the existing story-draft validation and commit path.
- Any Node Run failure terminates the Graph Run. There is no automatic retry, skip, fallback model, branch, loop, condition or parallel execution. User retry recompiles a fresh plan from current saved definitions and links it to the prior run.
- Graph events expose simple node states and streaming text deltas. Full node input/output and large diagnostics are loaded through a detail endpoint, not repeatedly embedded in SSE events.
- Node detail retains complete effective configuration, prompt provenance and messages, Artifacts, model calls, streamed/final output, tool arguments/results, usage, errors and safe redaction of secrets.
- Each Session persists only the active Graph Run and the most recent terminal Graph Run, including their debugging snapshots. Restart marks an incomplete run interrupted; later runs may prune older trace data.
- Debug Replay uses the original Node Run input, upstream Artifacts and read-only Project/Session/Worldbook snapshot together with the current Agent configuration. It cannot run downstream nodes, mutate a Session or commit a story turn.
- Character-card import produces AIRP-normalized data. The editable card fields are name, avatar, description, personality, scenario, card prompt fields, Openings, variable and asset entry points, selected Graph and Worldbook Bindings. Example messages and a free-form extensions editor are not part of phase one.
- Embedded card worldbooks, SillyTavern World Info JSON and AIRP Worldbook JSON import into simple global Worldbook Definitions. Entries use stable IDs and title, usage, content, enabled, order and optional tags. Titles are globally unique and conflicts receive deterministic copy suffixes. AIRP JSON export is supported; lossless SillyTavern export is not.
- A Project may bind multiple Worldbook Definitions. Bindings are Project-scoped and shared by all Sessions. Shared Worldbook changes affect future runs without a version-history system; project-specific changes require copying the Worldbook.
- Deletion is blocked while a referenced object still has dependents, with those dependents exposed to the user. No cascading deletion is performed.

## Testing Decisions

- Primary acceptance tests drive the Runtime Studio HTTP/SSE contract: create configuration, submit a Graph Run, observe ordered node lifecycle events, retrieve complete node detail, execute Debug Replay and verify no story mutation.
- Browser golden-path tests cover the Studio configuration flow, the game-side graph observer, a running-node stream, failure display and node-detail inspection. They assert visible user behavior rather than DOM implementation details.
- Provider tests use deterministic fake adapters for both Responses and Chat Completions mappings, model discovery errors, secret non-leakage, parameter forwarding and preserved Runtime-owned fields.
- Execution tests assert compilation freezes effective configuration, Node Runner receives only resolved input, Artifacts pass in order, final Artifact alone reaches the story commit path and any node failure stops subsequent nodes.
- Trace tests assert complete detail is persisted for current/most recent runs, SSE contains only appropriate live references and deltas, reconnect recovers received output, restart classifies an unfinished run as interrupted, and cleanup preserves the configured retention limit.
- Debug Replay tests assert original read-only snapshots are used, current Agent configuration is applied, output can be compared and no Graph progression, Session write or story commit occurs.
- Project and Worldbook tests cover normalized card import, Openings conversion, multi-binding, shared edit visibility on a subsequent run, global title copy suffixing, supported imports/exports and dependency-protected deletion.
- Existing runtime bridge, session event stream, narrative director, agent graph, runtime configuration, import compatibility and real-provider tests are prior art. New tests should extend their external behavior contracts rather than couple to private storage layouts.

## Out of Scope

- Arbitrary DAGs, branches, parallel execution, conditional edges, loops and Artifact merge policies.
- Model or tool calls represented as Graph topology nodes.
- User-defined node substate machines.
- Automatic retry, skip-on-error, fallback models and partial Graph recovery.
- Full Graph Run history browsing beyond active and most recent terminal runs.
- Session-level Worldbook Binding overrides, Worldbook versioning, rollback or binding-level entry overlays.
- A free-form imported-card extensions editor, example-message support and lossless SillyTavern Worldbook export.
- System keyring integration and protection against a local user or process that can directly read the phase-one secret file.

## Further Notes

- This is a target-state specification. Existing provider configuration, sequential graph execution and flat trace UI remain compatibility foundations, not proof that the full Studio behavior is implemented.
- Story commit remains a Runtime harness responsibility. Agent and Graph configuration can affect writing, but cannot bypass draft validation, MVU validation or commit invariants.
- Character and NPC domain entities remain distinct from Agent Definitions.
- The specification intentionally keeps Worldbook handling in AIRP skill mode: catalog entries guide explicit on-demand loading instead of preloading all reference content on every run.
