# Integrate Studio Into The Game Workspace

AIRP 将 Studio 从独立页面改为游戏界面内的顶部工作抽屉，并保留右侧 Monitor 持续展示存档与 Agent Graph 运行状态。这个结构牺牲了独立配置页的无限画布，换取游玩上下文不中断、配置修改与运行反馈相邻，以及桌面和移动端共享同一套导航模型；各 Studio 模块共用单一互斥抽屉宿主，节点详情使用主对话区上的单实例浮窗。

## Delivered Scope

- 游戏页成为唯一主工作区，顶部入口统一打开向下展开的互斥抽屉；旧 `/studio` 路径直接映射到同一工作区，不再存在独立 Studio HTML 文件。
- 游戏抽屉支持 Project 导入、搜索、切换、删除、角色卡/开场/Worldbook 绑定编辑，并恢复目标 Project 最近一次 Session。
- 导入带有 `character_book` 的 SillyTavern 角色卡时，Project 与 Worldbook 在同一导入边界完成创建和绑定；创建失败会回滚已创建的 Worldbook。
- Worldbook 绑定页区分加载中、空列表和错误重试状态；Project 列表刷新使用请求序号避免旧响应覆盖删除或切换后的状态。
- 默认 Provider、Agent、Graph 与 Regex Collection 首次打开即加载为完整可编辑表单；游戏 Monitor 是唯一可操作的活动 Graph 选择入口，Studio 不再提供重复的 Runtime Config。
- 最新正式回复在对话区直接提供“重新生成”和“回退到此”两个回合操作；reroll 不依赖侧栏选项是否存在。Regex Collection 抽屉只编辑和测试规则，Agent 到 Collection 的绑定只在 Agent 编辑器维护。
- Studio 启动诊断只显示服务端提供的安全消息与动作，不回显 key；Provider、Agent、Graph 与 Regex 保存均携带 `expected_revision`，运行中保存从下一 Task 生效。
- 当前 Graph Runtime 仍是线性顺序执行；Monitor 的垂直节点视图表达现有运行语义，不承诺任意分支/并行 DAG。

## Verification

`pytest -q`、Studio/API 回归测试、游戏 Project 抽屉测试、Node.js 前端语法检查，以及 Playwright 的 1440/1280 桌面和 390/360 移动布局、溢出与交叠检查覆盖本次交付。可控 blocking runner 集成测试证明运行中的 Task 使用启动时冻结的配置 revision，下一 Task 才使用保存后的 revision。真实 Provider qualification 和任意 DAG Runtime 仍属于后续工作。
