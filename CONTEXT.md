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
| 多 agent 一等职责为叙事导演、世界模拟、文风润色和角色演化 | 已确认 | `docs/architecture/future-runtime.md` |
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
| **卡片目录** | 一张卡运行时的持久目录；包含 `chat_log.json`、变量基线与 `memory/`。 |
| **世界书条目** | 卡片内按主题组织的设定正文；导入后正文存于 `memory/reference.md`。 |
| **catalog** | 世界书条目的轻量清单；每条提供标题和 usage，供叙事 agent 决定是否加载。 |
| **usage** | 一句“讲什么 + 何时读”的条目说明，类似 skill description。 |
| **MVU** | 卡作者可选的变量更新协议；由 RP Turn Adapter 从 Artifact 中解释并转换为变量状态和审计差分。 |
| **回合** | 用户输入 → 上下文准备 → 叙事生成 → MVU 执行 → 前端交付 → 记忆更新。 |
| **runtime/harness** | 调度 agent、工具、上下文和用户输入的执行环境。当前为 Claude Code；目标是独立实现。 |
| **Agent Framework** | 调度用户定义的 Agent 团队、模型调用、工具、Artifact、Graph Run 和 Trace 的通用框架；不规定故事内容或模型输出格式。 |
| **Artifact** | Agent 节点产生的可传递结果；它是 Graph Run 的交接事实，不代表特定故事内容或文本协议。 |
| **RP Turn Adapter** | 将 Agent Framework 的 Artifact 按用户选择的角色扮演协议解释为回合、变量变化和前端投影的可选适配层。 |
| **Regex Collection** | 全局可复用的有序内容处理规则集合，用于对 Agent 的输入或输出进行替换、提取等操作；每个 Agent 可绑定零或一个 Collection。它不是角色卡导入产生的酒馆兼容正则脚本；避免称为“正则集”。 |
| **Studio 抽屉** | 从游戏顶栏向下展开、用于编辑 Studio 模块的临时工作区；所有模块共用一个宿主且互斥显示，以保留当前游玩上下文。避免称为“Studio 页面”或“独立 Studio”。 |
| **Monitor** | 游戏界面中持续观察当前存档状态和 Agent Graph 执行状态的右侧区域；它提供实时节点与调试入口，但不编辑 Studio 配置。避免称为“设置侧栏”。 |

## 阅读顺序

1. [`docs/README.md`](docs/README.md)
2. [`docs/product/positioning.md`](docs/product/positioning.md)
3. [`docs/status/feature-status.md`](docs/status/feature-status.md)
4. [`docs/architecture/overview.md`](docs/architecture/overview.md)
5. 与当前任务相关的 architecture 文档和 ADR
