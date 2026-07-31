# Agent Studio 目标架构

## 目的与范围

Agent Studio 是 AIRP 的独立宽屏配置工作区。它负责编辑可复用的模型连接、Agent、编排图、角色卡项目和世界书；游戏界面右侧栏只负责观察当前运行、最近一次运行和打开节点调试详情。

本文描述目标架构，而非当前实现状态。第一阶段优先实现完整、可理解、可调试的创作工作流，不引入任意 DAG、运行历史浏览、世界书版本管理、自动重试或模型 fallback。

## 信息架构

Studio 包含六个一级视图：

| View | 管理对象 | 主要能力 |
|---|---|---|
| Providers | `Provider Profile` | 新建、编辑、删除、启停、保存 Key、测试连接、获取模型列表 |
| Agents | `Agent Definition` | 编辑名称、instruction 宏提示词、模型参数、高级参数和工具权限 |
| Graphs | `Graph Definition` | 选择 Agent、增删节点、排序、启停节点、指定输出节点 |
| Projects | `AIRP Project` | 编辑角色卡内容、Openings、变量与素材入口、绑定世界书 |
| Worldbooks | `Worldbook Definition` | 导入、新建、复制、重命名、编辑条目和导出 AIRP JSON |
| Regex Collections | `Regex Collection` | 编辑按顺序的 JavaScript 正则规则并绑定到 Agent 输入、输出或两者 |

游戏视图不承载这些复杂表单。它的 Agent Graph 面板只展示当前或最近一次运行的节点状态、流转路径、总耗时/token、失败位置，以及进入节点详情的入口。

## 数据所有权

### Studio Library

以下对象属于全局 Studio Library，可跨 AIRP Project 复用：

- `Provider Profile`
- `Agent Definition`
- `Graph Definition`
- `Worldbook Definition`

引用关系不复制被引用对象。需要项目专用变体时，用户复制对象后再修改。删除规则保持简单且可预测：Provider 被 Agent 引用、Agent 被 Graph 引用、Worldbook 被 Project 绑定时，阻止删除并列出引用对象；不做级联删除。游戏当前激活的 Graph 是独立运行时选择；删除它会清除选择而不改写 Project。

### AIRP Project 与 Session

一个 `AIRP Project` 对应一张角色卡所代表的角色与故事框架。Project 拥有规范化角色卡数据、Openings、变量基线、素材和 Worldbook Bindings，不拥有 Graph 选择或文本解析配置。导入源只属于导入边界；导入完成后，AIRP 内部数据结构成为编辑与运行的事实源。

`Session` 是 Project 内的故事存档或分支。一个 Project 可以拥有多个 Session，但角色卡内容和世界书绑定由 Project 共享，不提供 Session 级世界书覆盖。游戏界面的激活 Graph 为当前 Project 的独立运行时状态，只决定下一次 Run。

角色卡内嵌的世界书在导入时转换为独立 `Worldbook Definition` 并自动绑定到 Project。Project 还可以绑定任意多个全局世界书，例如角色专属设定、通用境界词汇、文风规则或资料库。

## Provider Profiles

Provider Profile 只管理连接与模型目录，不拥有 Agent 的生成参数。第一阶段支持多个真正可调用的 OpenAI-compatible Provider，并明确区分两种 API format：

- `responses`：调用 `/v1/responses`；
- `chat_completions`：调用 `/v1/chat/completions` 或 Provider Base URL 对应路径，以兼容 DeepSeek 等服务。

每个 Profile 至少包含名称、Base URL、API format、API Key、启用状态和已获取的模型 ID 列表。保存 Provider 时自动获取模型列表，也提供连接测试和手动刷新。获取失败会显示完整错误，但不阻止保存；Agent 编辑器始终允许手动填写模型 ID。

第一阶段将 API Key 持久化到独立的本地 `secrets.json`，文件只允许当前用户读写并加入忽略规则。后端不向前端回传原 Key，只返回是否已配置；Key 不进入 Graph、Agent、Execution Plan、Trace、日志、运行数据库或导出文件。Provider 通过 `SecretStore` 接口读取 Key，以便未来替换为系统凭证存储而不影响上层。

Provider Adapter 将 Responses 和 Chat Completions 转换为统一的内部请求、流式事件、最终响应和错误模型。Agent、Graph Runtime 与 UI 调试模型不依赖具体协议。

## Agent Definitions

`Agent Definition` 是可复用的执行实体，独立于 Graph。它至少拥有：

- 稳定且唯一的 `agent_id`；
- 用户可编辑且不要求唯一的 `name`；
- 完整可编辑的 `instruction` 模板；
- 默认 `provider_profile_id` 与 `model_id`；
- 基础生成参数；
- 自定义高级参数 JSON object；
- 工具 allowlist。

Agent 不再拥有 `role` 字段，Runtime 也不得根据名称或职责标签改变行为。用户可以自由命名 Agent；执行顺序由 Graph 决定，最终输出由 Graph 的 `output_node_id` 决定。

基础生成参数使用清晰控件编辑，包括 `temperature`、`max_output_tokens`、`top_p`、`stop`、`reasoning_effort` 和 `seed`，并支持使用 Provider 默认值。内部统一使用 `max_output_tokens`，由 Provider Adapter 转换成目标协议字段。

高级参数是一个自由 JSON dict 编辑框，不做字段名、字段类型或 Provider 能力校验，内容直接合并到 Provider 请求顶层；Provider 返回的错误由用户负责修正，并完整记录到 Node Run。最低限度的 Runtime 保留字段不可覆盖，包括实际输入、模型、工具定义、流式开关、认证、Base URL 和内部追踪字段。高级参数与基础控件重名时，高级参数优先，UI 明确显示该基础值已被覆盖。

工具权限只由 Agent Definition 的 allowlist 决定。Graph Node 不增加第二层工具权限，不允许 Prompt 声明或扩大权限。Execution Plan 只冻结 Agent 当前允许的工具集合。

## Prompt 装配

用户只编辑 Agent `instruction` 模板；不再选择或维护 Prompt Preset。Runtime 在 Graph Run 开始时用只读运行时上下文展开模板，并追加玩家输入、上游 Agent Artifact、工具协议和输出契约。宏支持直接根字段和点路径，例如 `{{player_input}}`、`{{settings.style}}`、`{{card_facts.name}}`、`{{current_state.phase}}`、`{{recent_turns}}`、`{{worldbook_catalog}}` 和 `{{handoff}}`；缺失路径保留占位符，便于发现拼写错误。预览结果同时显示原始模板、展开后的 instruction、可用宏根和每段 provenance。

Context Manifest 内部使用无写作含义的 `ContextLayout` 编译为固定 section。它不是 Studio 对象、不是 Agent 字段，也不参与用户侧配置选择。

Studio 提供编译预览，展示最终发送给模型的完整 messages，并逐段标明来源和合并顺序。用户可以充分控制 Agent 行为与上下文选择，但不能用自由文本删除或伪造 Runtime 的输入、handoff、工具和输出契约。

## Graph Definitions

第一阶段 Graph 是可视化的线性执行流水线，不是任意 DAG。它支持增删节点、排序、启停、连线展示，以及显式指定 `output_node_id`；暂不支持分支、并行、条件边、循环或合并策略。

Graph Node 只引用 Agent Definition，并拥有独立稳定的 `node_id`。同一个 Agent 可以在同一张 Graph 中出现多次，例如先生成初稿、经过审稿后再次执行修订；每次出现都会产生独立 Node Run。节点可设置本地显示标签，用于区分同一 Agent 的多次引用。

Graph Node 可以有限覆盖 Agent 的 Provider、model 和基础/高级模型参数。Graph 不拥有工具覆盖，节点也不依据 Agent 名称产生隐式执行语义。`output_node_id` 明确指定最终交给回合 harness 的节点；在线性第一阶段，它必须是有效执行链的末端节点。

## 编译与运行边界

定义层与执行层通过不可变的 Execution Plan 隔开：

```text
Agent Definition + Graph Definition + Project Snapshot
                         |
                       compile
                         v
               Immutable Execution Plan
                         |
                         v
                   Graph Runtime
                         |
                      dispatch
                         v
                    Node Runner
                         |
                         v
              Provider / Model / Tools
```

`Execution Plan` 在 Graph Run 启动时解析并冻结本次运行所需的 Agent instruction（含宏展开结果）、Provider、模型参数、工具、角色卡和已绑定世界书内容。Studio 中途保存的修改只影响下一次 Graph Run，不改变正在运行的任务。

`Graph Runtime` 只理解节点顺序、状态迁移、终止和 Artifact 传递；它不理解 prompt、模型厂商、角色名称或 UI 表单，也不得直接调用具体 Provider。

`Node Runner` 接收一个已解析节点、输入 Artifact 和冻结配置，执行该节点并返回 Node Result。它不理解自己位于哪张 Graph，也不决定下一个节点。Provider、模型调用、流式协议与工具执行都隐藏在 Node Runner 及其下层 adapter 中。

## Artifact 交接

节点之间只传递显式、不可变的 `Agent Artifact`，不传递含义不明的裸字符串。统一的 Node Result 至少包含：

```text
status
primary_artifact:
  kind
  content_type
  content
  content_hash
diagnostics_ref
```

第一阶段主要使用文本 Artifact，但 `kind` 由用户定义的 Graph 约定，例如 `plan` 或 `review`。Agent Framework 只读取状态和 Artifact，不解析模型原始响应，也不默认注入写作输出契约。最终 `output_node_id` 的 Artifact 经用户配置的 Agent Regex Collection 处理后，由 Host Commit 原样作为回合正文提交；不解析标签、摘要、选项或 MVU 语义。

## 失败与重试

失败语义保持单一：任一 Node Run 失败，整个 Graph Run 立即失败并停止后续节点。此前成功节点的 Trace 和 Artifact 仍可查看。Graph 不自动重试、不跳过节点、不切换 fallback 模型，也不提供复杂恢复策略。

游戏右栏突出失败节点；节点详情展示 Provider 错误、实际请求参数和调用记录。用户点击“重试”时，以原始任务输入和当前最新已保存定义重新编译 Execution Plan，创建一次全新的完整 Graph Run，并用 `retry_of` 关联失败运行。旧运行保留其原 Execution Plan，便于比较修改前后的行为。

## Graph 可视化

游戏右栏和 Studio Graph 视图只把 Agent Node 画成拓扑节点。模型调用与工具调用属于节点内部 Trace，不作为临时拓扑节点。

节点表面只显示四种状态：未运行、运行中、成功、失败。运行中节点使用高亮与旋转动画；当前流转边可高亮或动画显示。节点不显示“模型调用中”“读取世界书”等子状态，也不要求用户定义状态。点击任意已开始的节点打开完整调试窗口。

## 完整节点调试

Node Trace 的目标是让创作者判断自己编写的提示词和模型配置为何产生当前输出，而不是提供一份运维摘要。节点详情必须完整展示：

- 最终生效的 Agent、Provider、模型和全部生成参数；
- Prompt 各来源层、合并顺序和最终完整 messages；
- 上游 Agent Artifacts；
- 每次模型调用的完整输入、流式输出、最终输出和停止原因；
- 每次工具调用的名称、参数、完整结果、耗时与错误；
- 最终 Agent Artifact、内容哈希、耗时与 token 使用；
- Execution Plan 标识、定义标识以及失败堆栈或 Provider 原始错误。

运行中的节点详情实时追加模型文本 delta 和已完成的工具调用。节点完成后，临时流式内容以最终持久 Artifact 为准。浏览器断线重连后，从持久 Node Run 恢复已接收内容，而不是从空白开始。API Key、认证头等秘密始终脱敏。

大段调试数据不通过常驻 SSE 反复传输。SSE 发布节点激活、状态、关联 ID 和文本 delta；完整输入输出由节点详情 API 按需加载。

## Trace 生命周期

第一阶段不提供完整历史运行浏览。每个 Session 只保留：

- 当前正在运行的 Graph Run；
- 最近一次达到成功、失败或中断终态的 Graph Run。

这两份记录及其 Node Runs、完整输入输出、Artifacts、错误和调试快照持久化到本地运行数据库，跨页面刷新和应用重启保留。应用重启时，未完成的 Graph Run 标记为中断失败，并突出最后运行节点。新运行结束后可以清理更早的调试记录，维持“当前 + 最近一次”的上限。

这不是世界书或配置的版本管理。可重现性来自 Node Run 保存的实际完整输入、冻结配置和只读工具快照。

## Debug Replay

节点详情支持隔离的 Debug Replay，用于验证修改后的提示词和模型配置：

- 使用原 Node Run 的冻结输入和上游 Artifacts；
- 使用原运行时角色卡、世界书和 Session 状态的只读工具快照；
- 使用当前已保存的 Agent instruction 模板、运行时宏上下文、模型参数和工具 allowlist；
- 不执行下游节点，不修改正式故事或 Session 状态，不产生正式 commit；
- 保存为独立 Debug Run，并与原输出并排比较；
- 明确提示会产生新的模型调用费用。

Replay 窗口展示新旧有效配置差异。如果用户要验证最新角色卡或世界书内容，应回到游戏界面发起新的完整 Graph Run，而不是使用旧 Node Run 的 Debug Replay。

## Project 与角色卡编辑

Project 编辑器使用 AIRP 规范化结构，不暴露导入格式的 extensions JSON，也不保留没有 Runtime 用途的 example messages。第一阶段编辑字段保持简单：

- 名称和头像；
- `description`、`personality`、`scenario`；
- card system prompt 与 post-history instruction；
- Openings；
- 变量与素材入口；
- 当前 Graph 和已绑定世界书列表。

SillyTavern 的 `first_mes` 与 `alternate_greetings` 导入后统一转换为 `Openings`。编辑器提供一条默认开场和可选的其他开场，支持新增、删除、排序和设为默认；不向用户暴露 `alternate_greetings` 术语。

## Worldbook 编辑与交换

`Worldbook Definition` 是全局 Library 中的简单文件对象，不提供版本历史、固定版本、迁移或回滚。保存修改后，所有绑定该世界书的 Project 在下一次启动 Graph Run 时直接读取新内容。需要定制时复制成另一份世界书并重新绑定。

一份世界书包含多个结构化条目。每个条目只保留 AIRP Runtime 所需字段：

```text
id
title
usage
content
enabled
order
tags (optional)
```

AIRP 继续使用 skill-mode 世界书：catalog 展示条目的 `title + usage`，Agent 按需读取正文。SillyTavern 的关键词、position 等兼容字段不参与 AIRP Runtime，也不进入基础编辑器。

条目标题在整个 Studio Worldbook Library 中全局唯一。导入、新建、复制或重命名发生冲突时，自动生成 `标题-copy`、`标题-copy-2` 等名称，并向用户提示自动改名结果。内部仍使用稳定 `entry_id`，标题改变不破坏运行中的引用。

第一阶段支持：

- 从角色卡内嵌 `character_book` 导入；
- 从独立 SillyTavern World Info JSON 导入；
- 从 AIRP 原生 Worldbook JSON 导入；
- 在 Studio 中新建、复制、重命名和编辑；
- 导出 AIRP 原生 JSON。

不承诺无损导回 SillyTavern 格式。

## 第一阶段明确不做

- 任意 DAG、分支、并行、条件边、循环和合并策略；
- 模型/工具调用作为 Graph 拓扑节点；
- 节点子状态配置；
- 自动重试、节点跳过和模型 fallback；
- 完整 Graph Run 历史浏览；
- Session 级世界书覆盖；
- 世界书版本、回滚和绑定层条目覆盖；
- 原始导入格式的自由 extensions JSON 编辑；
- SillyTavern 世界书无损回导；
- 系统 keyring 集成。

这些约束让第一阶段先交付一个功能完整、边界清晰且真正可调试的 Agent 创作环境，同时保留未来扩展 DAG、凭证存储和运行历史的接口空间。
