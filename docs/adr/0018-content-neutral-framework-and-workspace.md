# ADR-0018：内容中立的 Agent Framework 与 Workspace

- **状态**：Accepted
- **日期**：2026-07-29
- **关联**：ADR-0003、ADR-0009、ADR-0011、ADR-0014、ADR-0016、ADR-0017

## Context

AIRP 当前将 Agent 编排、RP 回合提交、MVU、输出标签解析、静态资源和用户配置放在同一组运行路径中。`NarrativeDirector`、`narrative_director` 角色名、`narrative_draft` 产物和 `<content>`/`<summary>`/`<options>` 等规则会让 Agent instruction 看起来可编辑，但实际行为仍由引擎隐藏决定。

生产代码也曾依赖 `skills/` 的目录布局。该目录同时承载生产实现、Claude Code 工具、静态 Web 文件、Studio 可变对象和兼容投影，导致运行时配置存在两个来源，HTTP 也重复维护 `/api` 和 `/v1` 路径。

## Decision

1. 建立内容中立的 **Agent Framework**。它只负责用户定义的 Agent、Execution Plan、Graph Run、节点状态、Provider/Tool 调用、Artifact 交接、Trace 和失败语义。它不解析故事内容，不认识 RP 输出标签，不根据 Agent 名称或角色名改变执行。
2. 保留现有 RP Turn Adapter 作为第一方适配器。它拥有 RP 回合解析、正则规则、MVU、质量检查、开场和故事投影；Project 选择使用哪个 Adapter。当前 RP 游戏默认使用它，但 Framework 不依赖它。
3. Studio 定义是正常创建、编辑和执行的唯一事实源。旧 `settings.json`、`presets/` 和 `graphs/` 只保留首次迁移、导入和历史回放的兼容读取；不再作为正常 CRUD 或执行来源，也不再双写旧 Graph。
4. 生产 Python 代码迁移到 `src/airp`，网页资源迁移到 `src/airp/web`，卡片脚本等只读资源迁移到 `src/airp/resources`，用户可变对象统一归属 Workspace。`airp.cli`、`airp.server`、`airp.handler` 和 `airp.import_*` 都属于可安装生产包；旧 `skills/` 生产实现、转发层、网页副本和测试入口全部删除。
5. Workspace 默认位于操作系统用户数据目录，可由 `AIRP_DATA_DIR` 覆盖。Provider、Agent、Graph、Worldbook、Project、Session 和本地 Secret Store 不再写入源码或静态资源目录。
6. Application Facade 负责应用装配和命名动作；HTTP transport 只维护 `/v1/*`，不把 URL 或 JSON 路由细节泄漏给运行时和 Studio Library。
7. 测试先覆盖 Framework、RP Turn Adapter、Compatibility Adapter 和 Application Facade 的公开 seam，再删除被新契约覆盖的旧路径测试。事务、世界书 catalog/on-demand、回放和浏览器黄金路径测试继续保留。

## Consequences

- Agent instruction 成为用户能看到和编辑的写作约束来源；RP 格式协议仍可用，但其规则集中在可替换的 RP Turn Adapter。
- 旧项目和旧运行记录可以迁移/回放，但新运行不会再被 legacy preset 或 `narrative_director` 隐式改写。
- 旧 `skills/` 目录不再是生产实现、测试入口或 wheel 运行时依赖；`airp-runtime` 安装后可直接使用包内 CLI、网页资源和卡片脚本资源。
- Framework 可以被其他写作团队、审稿、资料整理或非 RP Graph 复用。
- Workspace 是用户数据边界；删除、复制和引用规则由 Studio Library 保持现有定义。
