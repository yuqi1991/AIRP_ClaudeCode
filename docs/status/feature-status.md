# 功能成熟度矩阵

> 状态定义见 [Wiki 首页](../README.md)。本表区分“已经有代码”“已经验证有效”和“未来目标”。

## Proven

| 功能 | 状态 | 用户价值 | 证据/入口 | 限制 |
|---|---|---|---|---|
| 卡片导入与存档 | Proven | 导入 PNG/JSON/TXT 素材，生成卡片目录、开场、世界书、变量和记忆文件 | `skills/import_card.py`、`skills/import_prepare.py`、`/rp` | 兼容范围未自动测试，复杂卡片仍需诊断 |
| MVU 变量与记忆 | Proven | 回合中执行变量更新，保存 chat log、变量差分和剧情摘要 | `skills/engine/mvu.py`、`engine/card.py`、`write_memory.py` | 未知路径和 server 不可用时校验较宽松 |
| 前端回合体验 | Proven | 浏览器输入、开场切换、重roll、回退、token 展示和内容渲染 | `skills/server.py`、`skills/styles/index.html` | 单卡全局运行时，无法多实例 |
| 世界书 skill 模式 | Proven | catalog + usage + 按需加载条目，显著降低动态上下文体积 | `engine/worldbook.py`、`round_prepare.py`、ADR-0002 | 长会话中 Grep 结果仍进入追加式 transcript |
| engine 深模块 | Proven | token、MVU、卡片存储、渲染、世界书和 context 已拆为独立模块 | `skills/engine/`、ADR-0003 | 数据目录仍未重组 |

## Working

| 功能 | 状态 | 当前情况 | 主要缺口 |
|---|---|---|---|
| 卡片/世界书兼容 | Working | 可解析 PNG/JSON/TXT、世界书、正则和美化相关资产 | 缺少统一兼容报告、自动测试和可视化调试 |
| 后台 NPC 与剧情规划 | Working | 规则和回合流程已定义，存在 story plan 与记忆文件 | 长期演化质量、可解释性和独立验证不足 |
| 文风与防抢话 | Working | settings/profile/硬性门禁已存在 | 仍依赖单 agent 自觉执行，缺少独立审校闭环 |

## Broken / 关键缺口

| 功能/问题 | 状态 | 为什么重要 | 当前证据 |
|---|---|---|---|
| Claude Code runtime | Broken | `wait_pending`/session loop 不适合作为独立产品 runtime | 当前流程依赖 ScheduleWakeup 与 Claude session |
| 上下文精确控制 | Broken | 追加式 transcript 会累积历史和按需 Grep tool result，无法完全由引擎决定 prompt 形状 | ADR-0001 |
| 世界模拟 | Broken | 后台 NPC、事件和伏笔尚不够自主、稳定或可解释 | 主要依赖单 agent 每轮遵守规则 |
| 卡片兼容诊断 | Broken | 导入失败、变量规则、正则、美化问题难以定位和向玩家解释 | 无完整诊断报告/测试矩阵 |
| 自动角色资料补全 | Planned | agent 尚未形成可追溯的 Web Search→资料摘要→设定采纳流程 | 产品原则已确认，未实现 |
| 玩家实时编辑 | Planned | 玩家尚不能有结构化、即时、可追溯地编辑设定 | 产品方向已确认，未实现 |

## Planned

- 独立 harness/runtime，替换 Claude Code 直驱 loop；
- 多 agent 编排：叙事导演、世界模拟、角色演化/文风润色；
- 玩家实时设定编辑；
- 卡片/世界书兼容诊断与修复建议；
- 外部角色资料的自主检索、来源记录和玩家采纳机制；
- 自动化测试与端到端验收。
