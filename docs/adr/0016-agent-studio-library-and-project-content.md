# Agent Studio Library 与 Project 内容边界

- **状态**：Proposed
- **日期**：2026-07-28
- **关联**：ADR-0002、ADR-0012、`docs/architecture/agent-studio.md`

Agent Studio 将 Provider Profile、Agent Definition、Prompt Preset、Graph Definition 和 Worldbook Definition 作为跨 Project 复用的全局 Library；AIRP Project 对应一张角色卡所代表的角色与故事框架，拥有规范化角色卡数据、Openings、变量、素材、Graph 选择、Worldbook Bindings 和多个 Session。导入格式只属于边界，导入完成后 AIRP 内部结构成为编辑与运行事实源；角色和 NPC 继续是故事实体，不成为 Agent。

Worldbook Definition 是无版本历史的简单 skill-mode 文件对象，一个 Project 可以绑定多本，且所有 Session 共享 Project 的绑定。共享世界书保存后影响所有绑定 Project 的下一次 Graph Run；需要定制时复制文件并重新绑定。世界书条目标题在全局 Library 唯一，冲突时自动追加 `-copy` 后缀；角色卡内嵌、SillyTavern World Info 和 AIRP JSON 均可导入，第一阶段只承诺 AIRP JSON 导出。

导入带有 `character_book` 的 SillyTavern 角色卡时，Project 导入边界同时创建对应的 Worldbook Definition，并立即写入该 Project 的 Worldbook Bindings；若 Project 创建失败，刚创建的世界书随事务回滚。删除游戏会删除 Project Definition；若删除的是当前游戏且仍有其他游戏，Runtime 自动切换到剩余游戏。

全局复用会让一次世界书编辑影响多个 Project，因此 Studio 必须显示引用关系并在删除被引用对象时阻止操作。该取舍避免 Session 覆盖、绑定层条目覆盖和世界书版本系统，同时保持通用资料与文风设定的跨角色复用。
