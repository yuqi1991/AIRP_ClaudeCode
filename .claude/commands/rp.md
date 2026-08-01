在当前目录启动 AIRP runtime。

1. 查找当前卡片目录或 PNG/JSON/TXT 素材。
2. 执行 `airp-runtime <card_folder> <project_root>`（源码 checkout 可用 `PYTHONPATH=src python -m airp.launcher ...`）。
3. 确认 `http://localhost:8765/` 可访问，并提示用户到 `/studio` 配置 Provider、Agent、Graph、Project 和 Worldbook。
4. 游戏页只选择激活 Graph；节点输入、输出和模型调用在 Studio Agent Trace 中查看。

没有可识别素材时，提示用户先放入 PNG 角色卡、JSON 世界书或 TXT 文本。
