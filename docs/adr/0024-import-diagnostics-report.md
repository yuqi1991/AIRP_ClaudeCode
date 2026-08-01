# ADR-0024：角色卡与世界书导入诊断报告契约

- **状态**：Accepted（报告格式、状态语义与生产 emitter/UI 均已实现；兼容矩阵继续扩充）
- **日期**：2026-08-02
- **关联**：Wayfinder #14、ADR-0016、ADR-0017、ADR-0023、`CONTEXT.md`

## Context

AIRP 导入 PNG、JSON、TXT 和 SillyTavern 角色卡。角色卡可能包含开场、alternate
greetings、变量初始化、正则脚本、前端资源和内嵌 `character_book`。导入边界会把这些
内容规范化为 Project、Openings、Worldbook Definition 和兼容资源，但旧的
`run_import()`/`prepare_card()` 返回的是面向内部流程的松散 summary，无法让玩家知道：

- 哪个源字段被导入、规范化、默认、跳过或失败；
- 内嵌世界书实际导入了多少条目，是否绑定到了当前 Project；
- 部分导入是否仍可游玩，应该检查哪个源路径；
- 某个兼容字段是 AIRP 运行时能力，还是只被记录而未被执行。

缺少这些信息会把“可用但有损”误认为“成功”，也会让世界书导入失败看起来像卡片导入
卡住。报告必须是可测试、可保存和可被 UI/CLI 共用的数据契约，而不是拼接给玩家看的
一次性字符串。

## Decision

### 1. 使用 versioned `airp.import-diagnostics` 报告

每次导入产生一个结构化报告，报告本身不拥有 Project 或 Worldbook 事实；它只记录导入
尝试对事实执行了什么转换。v1 的顶层结构固定为：

```json
{
  "schema": {"id": "airp.import-diagnostics", "version": 1},
  "overall": {"status": "success|degraded|failed", "counts": {}},
  "source": {"file": "...", "format": "..."},
  "project": {"name": "..."},
  "worldbook": {
    "embedded": true,
    "source_format": "sillytavern-character-book",
    "entries_seen": 0,
    "entries_imported": 0,
    "entries_skipped": 0,
    "bound_to_project": false
  },
  "findings": []
}
```

`findings` 是有序数组。每项至少包含：

| 字段 | 约束 |
|---|---|
| `code` | 稳定机器码，例如 `card.name.imported`、`worldbook.entry.invalid`；UI 不依赖中文文案判断状态 |
| `level` | `info`、`warning` 或 `error` |
| `status` | `imported`、`normalized`、`defaulted`、`skipped`、`not_present`、`degraded` 或 `failed` |
| `source_path` | 原始输入中的 JSON/path 表达式；不存在时为 `null` |
| `destination_path` | AIRP 事实或兼容投影的目标路径；不存在时为 `null` |
| `message` | 面向用户的简短说明，不包含完整卡片正文、API key 或其它秘密 |
| `repair` | 可选的安全建议对象；只描述人工检查或确定性默认值，不执行任意脚本 |

报告允许以后增加顶层字段和 finding code，但不能改变 v1 字段含义。源路径和目标路径
让 UI 可以定位问题，机器码让测试和未来本地化不必解析文案。

### 2. 固定 overall 状态语义

- `success`：没有 warning/error，所有必需事实成功落地；可有 `info/imported` finding。
- `degraded`：Project 可以创建和游玩，但存在默认值、跳过项、兼容降级或部分世界书
  导入。报告必须显示被跳过的数量和源路径。
- `failed`：没有可用的角色卡事实，或 Project 与内嵌 Worldbook 的原子创建/绑定边界
  失败。失败必须回滚本次导入新建的私有对象，不得留下半绑定 Project。

缺少可选字段本身不是失败；只有当它改变可游玩性或触发安全/原子性错误时才升级为
`warning`/`error`。结构错误不能被静默当成空数组。

### 3. 内嵌 Worldbook 必须显式报告

角色卡中的 `character_book` 仍按 ADR-0016 转换为独立 Worldbook Definition，并自动绑定
到当前 Project。报告必须同时给出 `entries_seen/imported/skipped` 和
`bound_to_project`：

- 有效条目导入且绑定成功时，产生 `worldbook.embedded.imported`；
- 单条目坏损时跳过该条目并产生 `worldbook.entry.*`，整体为 `degraded`；
- `character_book` 结构无法解析，或绑定/回滚失败时产生 `level=error,status=failed`，整体为 `failed`。

SillyTavern 的 regex scripts、beautify 或其它 extensions 只在确有 AIRP 运行时映射时
转换。没有映射的内容只能产生兼容性 finding，不能伪装成 Regex Collection 或静默丢弃。

### 4. 报告面向 UI、CLI 和测试复用

游戏抽屉在导入完成后先显示 overall 摘要，再按角色卡、开场、变量、世界书和兼容资源
分组展示 findings；允许复制/下载 JSON。UI 不负责重新解释导入规则，也不执行
`repair`。测试 fixture 至少覆盖完整 ST v2 卡、缺失字段/坏损条目的降级卡，以及无法解析
内嵌 worldbook 的失败卡。

原型验证记录在独立分支
`prototype/import-diagnostics-format` 的提交
`51babc153206f51d75bd115132533bf6ea26cc02`；一次命令输出了上述三类报告，作为本 ADR
的设计证据。原型代码不进入产品分支。

## Current implementation status

格式、状态语义和测试样例已经确定。`src/airp/import_card.py`、
`src/airp/import_prepare.py` 与 Project import API 在保留旧 summary 字段的同时发出
`airp.import-diagnostics`；游戏抽屉显示整体状态、世界书条目计数和 finding。真实 PNG/JSON/TXT
fixture 与更多 SillyTavern extensions 仍可继续扩充，但不再改变 v1 契约。

## Consequences

- 导入成功不再等同于“所有源字段无损保留”，玩家可以看到明确的降级边界。
- API、CLI、浏览器和回归测试共享同一个可版本化数据结构。
- 报告会增加导入实现的诊断工作量，但避免为每种格式维护互不一致的错误文案。
- 本 ADR 不承诺 SillyTavern extensions 的无损回导，也不决定任意 DAG 或多 Agent 世界
  模拟语义。
