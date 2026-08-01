---
name: rp
description: 启动 canonical AIRP runtime 并打开浏览器游戏/Studio。
---

扫描当前目录中的 PNG 角色卡、JSON 世界书和 TXT 素材，然后运行：

```bash
airp-runtime <card_folder> <project_root>
```

服务监听 `0.0.0.0:8765`。启动器自动导入或恢复卡片、活动 session 和 opening；不再启动旧的 `skills/server.py` 或文件轮询 bridge。Provider、Agent、Graph、Project、Worldbook 和 Regex Collection 都在 Studio 编辑，游戏页只选择激活 Graph。
