# Agent Studio 目标架构

## 目的与范围

Agent Studio 集成在 AIRP 游戏界面中。游戏始终是底层主工作区；点击顶栏入口时，对应 Studio 模块从顶栏向下展开，关闭后回到原有游玩上下文，不再跳转到独立页面。

本文描述目标架构，而非当前实现状态。第一阶段优先实现完整、可理解、可调试的创作工作流，不引入任意 DAG、运行历史浏览、世界书版本管理、自动重试或模型 fallback。

## 视觉方向

第一版只维护一套暗色视觉系统，不提供主题切换。整体以深海军蓝为背景、低饱和金色为强调、暖白色为正文，配合细边框、克制阴影和适合长篇阅读的衬线正文；Studio 表单沿用同一套视觉语言，但保持紧凑、清晰和工作台式的信息密度。

界面统一使用 `AIRP` 作为当前品牌名，不再显示“话本RP”“Claude Code RP”，也不把“Agent Studio”作为独立品牌。浏览器标题可按当前游戏动态显示为 `{游戏名} · AIRP`；未来正式命名时应能集中替换。

界面文案以中文为主，操作、状态、错误和表单说明不再整页使用英文；`Agent`、`Graph`、`Provider`、`Regex Collection`、`Token` 等领域对象保留英文名称。

桌面顶栏只保留 `AIRP` 字标、当前游戏名称和五个 Studio 抽屉入口。生成状态、Token、连接细节、存档信息和执行详情全部进入 Monitor；主题按钮删除，正常状态不显示装饰性连接指示。移动端额外保留 Monitor 图标入口，错误时可显示状态徽标。

### 主对话阅读

主对话采用长文优先的混合排版：AI 叙事使用接近全宽、弱边框的深色文本区和衬线正文；玩家输入使用较短、右对齐的金色描边消息块；不重复显示醒目的 `AI / USER` 标签。正文必须正确保留段落和换行，并支持加粗、斜体、删除线、一至三级标题、有序与无序列表、引用、链接、行内代码、代码块、分隔线和 GFM 表格。表格在可用宽度不足时于自身容器内横向滚动，不得撑宽主对话布局。

消息正文不渲染 Markdown 图片，原始 HTML 一律转义为文本。角色头像、立绘和角色卡美化 HTML 继续使用独立的受控资产与兼容渲染路径，不进入 Markdown 渲染器。

输入区固定在主对话底部，textarea 自动增高但最多约六行；右侧发送按钮在生成中原位切换为停止按钮。AI 行动选项显示在输入框上方，点击只填入输入框而不立即发送。“你的角色名”移入游戏的角色卡设置，输入区不显示上次提交时间等状态文案。

历史消息操作使用轻量图标工具栏：AI 消息提供复制、重roll和回退到此处，玩家消息提供复制和回退到此处。桌面端仅在消息悬停或键盘聚焦时显示，触屏端收入省略号菜单；会改变历史的操作保留一次简短确认。

角色卡视觉资产克制使用：游戏列表显示头像缩略图，Monitor 顶部显示当前游戏头像与名称，游戏抽屉的角色卡页提供较大原图预览。主对话区不使用模糊立绘背景或全屏壁纸；卡片自带状态面板和立绘仍可作为正文内受控内容显示。

## 信息架构

顶栏包含五个 Studio 入口。所有入口共用一个抽屉宿主，任一时刻只显示一个模块；点击另一入口替换抽屉内容，点击当前入口、关闭按钮或按 `Esc` 关闭抽屉。

桌面端抽屉只覆盖左侧主工作区，高度约为可用视口的 `72vh`，底部保留一小段游戏对话作为上下文提示，抽屉内容独立滚动；右侧 Monitor 保持可见并持续更新。移动端抽屉占满顶栏以下的剩余空间。

| 入口 | 管理对象 | 主要能力 |
|---|---|---|
| 游戏 | `AIRP Project` | 浏览和切换已导入游戏；编辑当前游戏的角色卡、开场、世界书绑定和编排选择 |
| 世界书 | `Worldbook Definition` | 导入、新建、复制、重命名、编辑条目和导出 AIRP JSON |
| Agents 与编排 | `Agent Definition`、`Graph Definition` | 编辑 Agent instruction、模型参数和工具权限；组织、排序和配置执行节点 |
| Regex Collections | `Regex Collection` | 管理 Agent 可绑定的有序输入、输出替换与提取规则，并测试转换结果 |
| 模型 | `Provider Profile` | 新建、编辑、删除、启停、保存 Key、测试连接、获取模型列表 |

Studio 抽屉从顶栏向下展开，在桌面端使用足够宽的编辑面积，但不卸载或替换底层游戏界面。游戏界面右侧的 `Monitor` 只观察当前存档状态和编排执行，不承载 Studio 配置表单；移动端 Monitor 收起为可打开的覆盖层。

Monitor 的存档区展示当前存档名称、Revision 和最后保存时间，并提供存档切换、新建、重命名和删除的紧凑图标操作。生成期间禁用这些管理操作；角色卡和其他游戏内容仍只在顶部“游戏”抽屉中编辑。

“Agents 与编排”抽屉内使用 `Agents` 和 `编排` 两个标签页，不同时挤压在一个画布中。从编排节点打开其 Agent 时，界面切换到 `Agents` 标签并定位对应定义。

`编排` 标签采用三栏布局：左侧为 Graph 列表，中间为从上到下的线性节点拓扑，右侧为当前节点设置、输出节点设置和保存操作。中间拓扑支持选择节点和调整顺序，但不提供自由连线；节点外观与 Monitor 使用同一套视觉语言。

`Agents` 标签采用三栏布局：左侧为 Agent 列表与搜索，中间在 `Instruction` 和 `Prompt 预览` 之间切换，右侧编辑模型参数、工具权限和 Regex Collection 绑定。名称、复制、删除和保存等对象操作集中在工作区顶栏。

### 游戏抽屉

游戏抽屉左侧显示所有已导入的游戏 Project，右侧以更大面积编辑当前正在游玩的游戏。编辑区包含角色卡、开场、世界书绑定和编排配置选择。

左侧游戏列表提供搜索和导入入口。右侧编辑区使用 `角色卡`、`开场`、`世界书绑定`、`编排选择` 四个标签页；活动 Graph 选择按游戏独立保存为运行配置，但不写入角色卡 Project 内容。

点击另一游戏会将其设为当前游戏，并恢复该游戏最后使用的存档会话。如果当前游戏正在生成回复，切换前必须确认；确认后先取消生成并等待任务进入不提交半成品的终态，再执行切换，取消确认则留在当前游戏。

当前服务尚无跨 Project 切换 Runtime 的接口。UI 分支允许补充完成该工作流所需的最小后端契约：切换活动 Project、取消进行中的生成、恢复目标 Project 最后活动的 Session，并返回新的游戏快照；不借此重构其他 Runtime 或领域模型。

应用启动时恢复上次活动的游戏及其最后活动存档。若记录指向已删除游戏，则回退到最近使用的现存游戏；没有任何游戏时自动展开“游戏”抽屉，显示空状态和导入入口。

游戏编辑器使用显式保存。未保存修改只存在于当前表单；切换游戏时直接丢弃，不自动保存，也不显示未保存修改确认。

## 数据所有权

### Studio Library

以下对象属于全局 Studio Library，可跨 AIRP Project 复用：

- `Provider Profile`
- `Agent Definition`
- `Graph Definition`
- `Worldbook Definition`
- `Regex Collection`

引用关系不复制被引用对象。需要项目专用变体时，用户复制对象后再修改。删除规则保持简单且可预测：Provider 被 Agent 引用、Agent 被 Graph 引用、Graph 被 Project 使用、Worldbook 被 Project 绑定时，阻止删除并列出引用对象；不做级联删除。

### AIRP Project 与 Session

一个 `AIRP Project` 对应一张角色卡所代表的角色与故事框架。Project 拥有规范化角色卡数据、Openings、变量基线、素材、Graph 选择和 Worldbook Bindings。导入源只属于导入边界；导入完成后，AIRP 内部数据结构成为编辑与运行的事实源。

`Session` 是 Project 内的故事存档或分支。一个 Project 可以拥有多个 Session，但角色卡内容、Graph 选择和世界书绑定由 Project 共享，不提供 Session 级世界书覆盖。

角色卡内嵌的世界书在导入时转换为独立 `Worldbook Definition` 并自动绑定到 Project。Project 还可以绑定任意多个全局世界书，例如角色专属设定、通用境界词汇、文风规则或资料库。

导入边界同时产生 [`airp.import-diagnostics` v1 报告](../adr/0024-import-diagnostics-report.md)。报告记录源路径、目标路径、导入/规范化/默认/跳过/失败状态，以及内嵌 Worldbook 的条目计数和绑定结果；`success`、`degraded`、`failed` 只描述本次导入结果，不替代 Project 或 Worldbook 事实。没有 AIRP 运行时映射的 SillyTavern extensions 只能作为兼容性 finding 展示，不能静默伪装成已执行能力。

角色卡内容和 Studio API 遵循 [`ADR-0025`](../adr/0025-mvu-and-local-security-boundary.md)：
卡片脚本默认不在主页面执行，Runtime commit 只接受 schema wildcard 允许的动态 MVU
路径；本地服务默认 loopback，并以受控 Origin/capability 保护 API、SSE 和动态投影。

## Provider Profiles

Provider Profile 只管理连接与模型目录，不拥有 Agent 的生成参数。第一阶段支持多个真正可调用的 OpenAI-compatible Provider，并明确区分两种 API format：

- `responses`：调用 `/v1/responses`；
- `chat_completions`：调用 `/v1/chat/completions` 或 Provider Base URL 对应路径，以兼容 DeepSeek 等服务。

每个 Profile 至少包含名称、Base URL、API format、API Key、启用状态和已获取的模型 ID 列表。保存 Provider 时自动获取模型列表，也提供连接测试和手动刷新。获取失败会显示完整错误，但不阻止保存；Agent 编辑器始终允许手动填写模型 ID。

第一阶段将 API Key 持久化到独立的本地 `secrets.json`，文件只允许当前用户读写并加入忽略规则。后端不向前端回传原 Key，只返回是否已配置；Key 不进入 Graph、Agent、Execution Plan、Trace、日志、运行数据库或导出文件。Provider 通过 `SecretStore` 接口读取 Key，以便未来替换为系统凭证存储而不影响上层。

Provider Adapter 将 Responses 和 Chat Completions 转换为统一的内部请求、流式事件、最终响应和错误模型。Agent、Graph Runtime 与 UI 调试模型不依赖具体协议。

模型抽屉采用两栏布局：左侧为 Provider Profile 列表、连接状态与新建入口；右侧编辑 Base URL、API 格式、密钥状态、启停和模型目录，并提供测试连接与刷新模型操作。模型目录保持紧凑列表，不再拆分第三栏。

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

每个 Agent Definition 可以绑定零或一个全局 `Regex Collection`。Collection 中的启用规则按可见顺序执行，目标可为 Agent 输入、输出或两者；输入转换发生在宏展开和 Provider 请求之前，输出转换发生在完整模型文本组装之后。原始值、转换值和逐规则诊断进入 Node Run，Collection 的修改只影响下一次运行。

Regex Collections 抽屉采用三栏布局：左侧为 Collection 列表与搜索，中间为可启停和调整顺序的规则列表，右侧为当前规则编辑器及可折叠的测试输入、转换结果和逐规则诊断。测试使用当前未保存的表单内容。

## Prompt 装配

用户只编辑 Agent `instruction` 模板；不再选择或维护 Prompt Preset。Runtime 在 Graph Run 开始时用只读运行时上下文展开模板，并追加玩家输入、上游 Agent Artifact、工具协议和输出契约。宏支持直接根字段和点路径，例如 `{{player_input}}`、`{{settings.style}}`、`{{card_facts.name}}`、`{{current_state.phase}}`、`{{recent_turns}}`、`{{worldbook_catalog}}` 和 `{{handoff}}`；缺失路径保留占位符，便于发现拼写错误。预览结果同时显示原始模板、展开后的 instruction、可用宏根和每段 provenance。

Context Manifest 内部仍保留 `PromptPreset` 类型作为固定 section 编译兼容层，但它不是 Studio 对象、不是 Agent 字段，也不参与用户侧配置选择。

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

第一阶段主要使用文本 Artifact，但 `kind` 由用户定义的 Graph 或所选 Adapter 约定，例如 `plan`、`review` 或 RP 适配器自己的回合产物。Agent Framework 只读取状态和 Artifact，不解析模型原始响应，也不默认注入 `narrative_draft` 输出契约。只有 `output_node_id` 的 Artifact 才交给所选 Adapter 解释；RP Adapter 再决定是否校验并提交故事状态。

## 失败与重试

失败语义保持单一：任一 Node Run 失败，整个 Graph Run 立即失败并停止后续节点。此前成功节点的 Trace 和 Artifact 仍可查看。Graph 不自动重试、不跳过节点、不切换 fallback 模型，也不提供复杂恢复策略。

游戏右栏突出失败节点；节点详情展示 Provider 错误、实际请求参数和调用记录。用户点击“重试”时，以原始任务输入和当前最新已保存定义重新编译 Execution Plan，创建一次全新的完整 Graph Run，并用 `retry_of` 关联失败运行。旧运行保留其原 Execution Plan，便于比较修改前后的行为。

## Graph 可视化

Monitor 和 Studio Graph 视图只把 Agent Node 画成拓扑节点。Monitor 以从上到下的方向完整展示当前 Graph；正在工作的 Agent 节点高亮。点击已开始的节点时，保持 Monitor 宽度和执行图不变，在左侧主对话区域上方打开足够大的节点调试浮窗，实时展示文字流与工具调用。模型调用与工具调用属于节点内部 Trace，不作为临时拓扑节点。

Monitor 始终显示当前游戏选择的完整 Graph：空闲时节点为待命状态，运行时显示等待、运行中、成功或失败，运行结束后保留最近一次结果直到下一次运行开始；只有未选择 Graph 时显示空状态。

节点调试浮窗为单实例；点击其他 Agent 节点会直接替换浮窗内容。浮窗支持关闭按钮和 `Esc`，第一版不提供拖拽、缩放、最小化或多窗口并排。

当前 UI 分支只把既有线性执行链实现为 DAG 风格的拓扑可视化，不改变 Graph Runtime，不加入分支、并行或汇合。组件的数据与布局边界应允许未来扩展，但本阶段只验收线性图。

动画保持克制：Studio 抽屉约 `220ms` 向下展开或收起；活动 Agent 节点使用柔和金色呼吸边框，当前执行边使用单向流动高光；实时文字流只显示光标，不做整块闪烁或持续背景动画。所有效果遵守 `prefers-reduced-motion`。

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

世界书抽屉采用三栏布局：左侧为世界书列表、搜索、导入和新建；中间为当前世界书的条目列表，可搜索、排序和启停；右侧只编辑当前条目的标题、usage、正文和标签。界面不同时展开整本世界书的所有条目表单。

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
