# ADR-0011：回合流转与模型解耦——commit 是 harness 动作，不是模型工具

- **状态**：Experimental（取代 ADR-0005/0006/0010 中「commit 作为模型工具」的描述）
- **日期**：2026-07-26
- **关联**：ADR-0005、ADR-0006、ADR-0010、`docs/specs/pi-agent-core-runtime.md`、`docs/research/pi-rp-borrowing-notes.md`

## Context

ADR-0005/0006/0010 建立的契约是：叙事导演通过调用模型可见的 `commit_turn_draft` 工具来提交回合，引擎只在模型**主动调用**该工具时 commit。真实 DeepSeek E2E（ADR-0010）暴露了问题——模型不一定调 commit，或先绕几轮 read-only 工具再耗尽轮次，导致 `failed_terminal`。

这违反一条根本原则：

> **agent harness 的基本回合流转（submit → 工具循环 → commit → 落盘）绝不能依赖配置模型的能力或 prompt 写法。模型只决定写作质量（文笔/剧情/角色），不决定引擎能否完成一次回合。**

这与项目逃离 Claude Code 直驱的初衷一致：不能让引擎的存活取决于某个外部智能体「是否听话」。换模型、换 prompt、模型抽风，都不应让引擎卡死或失败。

## Decision

**Commit 成为 harness 动作，不再是模型工具。**

1. **模型只产叙事文本**：legacy `response.txt` 形态（`<content>`/`<summary>`/`<options>`/`<UpdateVariable>`）。模型不知道「提交」这个概念。
2. **harness 决定回合边界**：当模型停止发起 read-only 工具调用并产出最终文本（或有界轮次耗尽），harness 解析文本 → 质量门禁 + 严格 MVU/schema 校验（ADR-0006）→ commit。
3. **`commit_turn_draft` 与 `validate_state_proposal` 从模型工具面移除**。模型可见工具只剩 read-only：`get_session_snapshot`、`get_recent_memory`、`load_worldbook_entry`。单写边界（optimistic revision + per-task 幂等 + 投影幂等）保留为 harness 内部方法。
4. **解析确定性**：`engine/turn_parser.parse_turn_text` 把模型文本解析为 `TurnDraft`；缺标签给默认值，**永不抛错**——空内容交质量门禁判定，不挂死流转。模型给出的 `polished_input` 仅作可选编辑元数据；兼容投影与重建必须使用持久 task 的原始玩家输入，模型不能改写玩家历史。
5. **有界修订**：质量/MVU 拒绝时，harness 把拒绝反馈给 director 重产（`DirectorHandle.set_commit_feedback` + 重入），受 `max_commit_validation_retries` 约束；耗尽落 `quality_exhausted`，不推进 revision。修订在提交历史里不可见（一条 chat、一次 commit）。
6. **MVU 归属**：从模型文本解析（`<UpdateVariable>`/inline `_.set`/`<JSONPatch>`），harness 校验+应用。模型完全不碰状态写入——与既有 MVU 标签解析机制一致（ADR-0006 的 `_projected_state_from_base` 本就从文本抽命令）。
7. **开场不是玩家回合**：缺少卡片 `first_mes` 时，harness 以 revision 0 snapshot 和当前 preset 编译上下文，使用 graph 中最终启用的 `narrative_director` 节点生成并解析开场。结果作为 AI-only opening 保存；经 MVU 校验得到的 opening 变量成为 revision 0 权威状态，同时记录 usage 与 durable event，但不调用 `submit`、不创建 task/commit，也不推进 revision。

## 验收（原则达成证明）

`test_flow_completes_when_model_only_emits_text_and_never_calls_any_tool`：director 只产最终文本、**零**工具调用、**零** commit 信号 → harness 仍 commit 恰好一次，revision+1，chat/state/projection 一致，MVU 正确应用。此测试确定性通过，是本 ADR 的验收门槛。

真实 DeepSeek E2E（ADR-0010）相应改为：prompt 只要求叙事、**不**指示模型调 commit，harness 从文本 commit。

## Consequences

- 引擎基本流转不再依赖模型行为：换模型/换 prompt/模型抽风，最坏是内容质量差（质量门禁可能拒→重产或 `quality_exhausted`），**绝不**因「模型没调 commit」而 failed_terminal 卡死。
- 单写边界、optimistic revision、reroll/rollback 分支语义（ADR-0008）、投影幂等（ADR-0006）全部保留——commit 只换了归属（harness 内部 vs 模型工具），不变量不变。
- `commit_turn_draft`/`validate_state_proposal` 不再是模型可见工具；模型工具面收窄为 3 个 read-only。
- 减小了 ADR-0008 记录的并发/lease 窗口的一部分：commit 不再依赖模型主动调用，director 返回后 harness 同步 commit，中断/并发窗口更窄。
- 生成式开场不会再把内部生成指令暴露成玩家消息；玩家第一轮固定从 revision 0 前进到 revision 1，重roll、回退和重启恢复共享同一条 lineage 语义。
- 取代 ADR-0005/0006/0010 中「commit 作为模型工具 / director 等模型调 commit」的描述（见各 ADR 的 supersession 注记）。

## 仍未解决（非本票范围）

- generation lease / crash recovery（ADR-0008 gap，Ticket 07）。
- browser golden path、真实 DeepSeek 长会话/错误路径的更广覆盖。
- 模型仍可能产出非法 MVU 或低质内容——这些由质量门禁 + 有界修订处理，属「写作质量」范畴，符合原则。
