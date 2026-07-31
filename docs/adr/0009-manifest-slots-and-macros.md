# ADR-0009：Context Manifest 的 Slot 化与编译期宏

- **状态**：Experimental
- **日期**：2026-07-25
- **关联**：ADR-0004、Ticket 02 Context Manifest、`docs/research/pi-rp-borrowing-notes.md` §1、`docs/specs/pi-agent-core-runtime.md` Decisions 12–16、28

## Context

Ticket 02 的 Context Manifest 以固定 11 个 section 硬编码在 `context_compiler.compile_context` 中：顺序、来源、stability、inclusion_reason 全部内联。这保证了确定性、可哈希、provenance 与可重放，但无法声明式表达 CLAUDE.md 已要求的「模块化指令」——文风 / NSFW / 防抢话 / 人称 / 背景 NPC 独立启停、同维度互斥。

`docs/research/pi-rp-borrowing-notes.md` §1 记录了 pi-rp 的 PromptPreset：内置 slot + 宏引擎 + `compileSystemPrompt`。AIRP 只借鉴**功能形态**（声明式 slot 组合 + 宏），不引入 pi-rp 的 `.pi/prompt-presets/*.json` 加载、autoActivate，或 `{{date}}`/`{{time}}`/`{{cwd}}` 等非确定性宏。

**红线**（borrowing notes §1）：slot/宏不得破坏确定性、可哈希、provenance。宏展开必须在**编译期**完成并计入 `content_hash`；运行期不可变。

## Decision

1. **`ManifestSlot` + `PromptPreset`**
   - `ManifestSlot`：`kind` / `stability` / `inclusion_reason` / `resolve(request) → (content, source_meta)` / `enabled` / 可选 `include_when` / 可选 `expand_macros`。
   - `PromptPreset`：有序 `slots` + `id` / `version`。
   - 内置 `DEFAULT_CONTEXT_LAYOUT` 精确复现既有 section 顺序、稳定性、来源和条件 worldbook entries。这是内部兼容基线；现行 Graph 回归集中在 `skills/tests/test_graph_execution.py`。

2. **`compile_context` 消费 preset**
   - 从 `ContextCompileRequest.preset`（对象）或 `preset_id`（仅识别 `default`）解析 preset；缺省 → `DEFAULT_PRESET`。
   - 按 enabled + include_when 迭代 slot，经既有 `_add` / budget / payload / hash 路径产出 `CompiledContext`。
   - Manifest 元数据可含 `preset_id` / `preset_version` / `macros_version`；**不改变** default preset 的 payload 字节与 `payload_hash` / `stable_payload_hash`。

3. **编译期宏引擎**
   - 语法：`{{name}}`，仅在 `expand_macros=True` 的 **string** content 上展开，且在 `_add` / hashing **之前**。
   - 内置宏（只读 snapshot/request，无 live FS / ambient）：
     - `{{style}}` / `{{nsfw}}` / `{{person}}` / `{{charName}}` ← `snapshot.settings`（及 card_facts.name 作 charName 回退）
     - `{{user}}` ← settings.user 或 snapshot.user，缺省空串
   - **明确排除**：`{{date}}` / `{{time}}` / `{{cwd}}` / random——非确定性，违反红线。
   - 宏版本 `MACROS_VERSION` 写入 section `source.version`（拼接）与 manifest `macros_version`，保证 preset+snapshot 恒等展开。

4. **声明式模块化**
   - `compose_preset(base, disable_kinds=..., slot_overrides=..., append_slots=...)` 在 preset 层启停/替换 slot，**不改** `compile_context`。
   - `static_slot(...)` 便于挂固定指令文本（可带宏）。

5. **Runtime 接线（2026-07-27 续接）**
   - `skills/styles/presets/*.json` 与 `settings.json.runtime.preset_id` 只作为旧 runtime manifest 的兼容读取路径；新运行时由 Studio Project、Agent instruction 和 Graph Definition 负责用户配置。
   - 旧 entry 仍使用稳定 `id`、`role`、`enabled`、`placement`、`depth`、`order`，正文可内联或引用用户工作区 Markdown；引擎不再随 wheel 提供具体写作 preset。
   - preset 在 task 创建时连同 source hash 与展开 provenance 冻结到 `source_snapshot`；前端编辑仅影响后续 task。
   - `skills/styles/graphs/*.json` 只为旧客户端提供兼容投影；新 Graph Definition 存放在 Workspace，执行图不绑定写作角色或内容格式。

## Consequences

- 用户可以把文风、人称、NSFW 或临时指令写进自己的 Agent instruction 与宏上下文，E2E prompt 调优不必再改 compiler；引擎不再提供这些写作开关。
- 既有 Ticket 02 契约（确定性、replay、tamper、worldbook catalog-only、budget 截断、settings freeze）保持绿；default manifest 只保留空策略 slot，不注入引擎写作提示。
- 旧 JSON preset/受限顺序 graph 仍可读取，但新 Studio 不创建 preset；不实现 autoActivate、任意条件 DSL 或非确定性宏。
- 真实 DeepSeek E2E / recall / generation lease 仍属后续票。
