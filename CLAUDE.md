# AIRP 开发与运行约定

AIRP 的生产入口是可安装的 `airp-runtime`，生产 Python 代码只在 `src/airp`，回归测试在 `tests`。`skills/` 不再是运行时、测试或资源入口；不要重新添加旧桥接脚本、文件轮询回合管线或第二套 Provider/runtime。

## 启动

```bash
airp-runtime <card_folder> <project_root>
# 源码 checkout：
PYTHONPATH=src python -m airp.launcher <card_folder> <project_root>
```

服务器监听 `0.0.0.0:8765`。Studio 是 Provider、Agent、Graph、Project、Worldbook 和 Regex Collection 的唯一编辑入口；游戏页面只选择激活 Graph。Provider key 由 Workspace Secret Store 保存，不写入 Graph、事件或 prompt manifest。

## 架构边界

- `airp.engine`：内容中立的 Graph/Provider/Context/Regex/Macro 逻辑。
- `airp.host.rp`：故事会话、卡片投影、世界书和显式 RP capabilities。
- `airp.server`：HTTP/SSE transport 与 Studio API。
- `airp.import_card` / `airp.import_prepare`：卡片导入和启动初始化。
- `airp.web` / `airp.resources`：只读网页和卡片脚本资源。
- `airp.compat`：仅用于旧卡片回放/导入的兼容解析。

引擎不定义文风、人称、NSFW、字数、标签或正文格式。此类软约束由用户编辑 Agent instruction；宏只负责可审计地组装上下文。Regex Collection 由 Agent 绑定，按配置顺序对 input/output/both 应用。

## 数据与调试

- Studio 数据默认位于 `~/.local/share/airp`：`library/`、`projects/`、`secrets.json`、`sessions/`、`runtime/`。
- 卡片目录保存 `.card_data.json`、`memory/`、`.runtime.sqlite3` 和兼容投影。
- Agent Trace 必须能看到节点输入、输出、错误、模型调用和工具调用；不要用空的“成功”占位覆盖真实运行证据。
- 一个节点失败就终止整图；重试由用户点击整图重跑完成。

## 开发检查

```bash
pytest -q
python3 -m compileall -q src tests
git diff --check
```

真实 Provider 测试默认跳过，明确设置 secret 后再运行 `tests/test_real_deepseek_e2e.py`。修改配置模型或导入格式时，优先增加失败用例，再修实现。
