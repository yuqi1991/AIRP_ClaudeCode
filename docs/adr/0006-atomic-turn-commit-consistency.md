# ADR-0006：原子回合提交一致性（严格 MVU 校验 + 幂等投影）

- **状态**：Experimental
- **日期**：2026-07-25
- **关联**：ADR-0004、ADR-0005、Ticket 05（`.scratch/pi-runtime/issues/05-atomic-turn-commit.md`）、`docs/specs/pi-agent-core-runtime.md`

## Context

Ticket 03 让 `commit_turn_draft` 成为唯一写入权威状态的入口，并加入 optimistic `expected_revision`、同 task 幂等与 projection 失败回滚。但提交前仍缺两件事：

1. **提交门禁**：`commit_turn_draft` 此前只校验 draft 形状（`validate_draft_dict`）与 revision，不校验内容质量、也不校验 MVU/schema 合法性。模型产出的畸形 draft 可能直接落盘。
2. **重入与重试一致性**：同 commit id 的投影重入、同 task 内的草稿修订是否真正「不在提交历史里留下痕迹」，需要显式契约而非隐式假设。

Ticket 05 的目标：无论质量重试、MVU/schema 拒绝、provider 错误、stale revision 还是重复 commit，玩家只看到一个完整一致的已提交回合，或一个明确的未提交失败；聊天、变量/审计、记忆与前端投影来自同一个 committed revision。

## Decision

> **⚠️ Partially superseded by ADR-0011 and issue #26**：commit 不再由模型工具发起，而由 harness 自动执行。自 issue #26 起，引擎内置的正文质量/长度门禁也已取消；空字符串和纯空白都是可正常提交的 opaque text。严格 MVU/schema 校验、optimistic revision、唯一 commit 和幂等投影仍保留。

在 Ticket 03 的单一写入边界之上，补齐提交门禁与一致性保证，全部在提交事务内、写盘前完成：

- **正文不设内置质量门禁**：引擎把 Graph 最终输出作为 opaque text 提交，不校验空值、长度、文风或标签。这些要求由用户可编辑的 Agent Prompt 和 Regex Collection 表达，引擎不兜底。
- **严格 MVU/schema 校验**（提交时，写盘前）：对 `draft.mvu_commands or content` 抽命令，用 `generate_schema(base_state, strict_template=True)`（非可扩展）逐条 `validate_command_strict`，再 dry-run `execute_commands`。任一不过返回稳定错误码，**不写 commit/revision/snapshot/投影**。基线读自 `_state_at_revision(task["base_revision"])`，不碰可变的 `state.js`/`chat_log.json`。
- 历史数据中的 `quality_gate_failed` / `quality_exhausted` 状态仍可读、可展示，但新运行不再产生这两种状态。
- **失败表面不冒充成功**：MVU/stale/provider-terminal/abort/投影失败一律落非提交终态（`commit_id is None`、revision 不变、`chat_log.json` 不变、无 `turn.committed` 事件）。
- **幂等且可重试的投影**：`projection_checkpoints` 增加 `applied_marker`；`_project` 在 `state=='applied'` 或 `applied_marker==commit_id` 时短路，重入安全。首次投影失败保留 backup/restore（Ticket 03）使任务停在 `projection_pending`，二次 `_project` 可恢复，最终只产生一条 chat 行、不重复 MVU、输出结构等价。
- schema 迁移：`tasks.validation_failures`、`tasks.validation_exhausted`、`projection_checkpoints.applied_marker` 三列带 `ALTER TABLE` 升级路径。

## Consequences

- 玩家可见面只剩「完整一致的已提交回合」或「明确未提交失败」；所有下游表面绑定同一 commit/revision。
- 真实 reroll/rollback 分支语义仍属 Ticket 06；崩溃恢复分类属 Ticket 07；真实浏览器/SSE 与真实 DeepSeek E2E 属最终选型门槛——本票均为黑盒契约 + FakeProvider。
- 复用了引擎既有 MVU 原语（`extract_commands`/`generate_schema`/`execute_commands`），未另立第二套语义；严格校验是 MVU 之上的**增量**，见下方关键约束。

### 关键约束（review 中纠正的 wide-blast-radius 风险）

实现期间一度把严格校验直接写进了**共享**的 `engine.mvu.validate_command`（并改了 `generate_schema`/schema walker），这会改变 live 回合路径（`handler.py` 在 mvu_server 不可用时回退到 `validate_command`）对**新变量路径**的宽容：卡作者常在运行时引入新 NPC/计数器等新路径，原本被宽松接受，改后会以 `Unknown path` 被拒。

纠正：严格校验拆为**独立** `engine.mvu.validate_command_strict`，仅与严格（非可扩展）schema 配对用于提交门禁；`validate_command`/`generate_schema`/`_get_schema_for_path` 保持 live 宽松语义不变。回归测试 `test_live_mvu_validation_stays_lenient_about_new_paths` 锁定此边界。

`mvu.py` 的改动因此是**纯增量 + 一处无害抽取**（`_get_schema_for_path` 委托给新 `_get_schema_for_parts`），无行为变化；新增的 `validate_command_strict` 是提交门禁专用。
