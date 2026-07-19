"""engine — 纯逻辑库（deep modules）。

每个模块是一个窄接口 + 大实现的 deep module，供 cli/ 脚本 import。
- tokens: token 记账（transcript delta + checkpoint）
- mvu: 变量系统（JSONPatch 执行 + schema + checklist）
- card: 卡片数据访问 + turn 编辑
- render: content.js / state.js 渲染
- worldbook: 世界书 catalog
- context: round_context / import_context 构建
"""
