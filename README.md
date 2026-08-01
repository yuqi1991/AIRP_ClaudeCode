# AIRP

AIRP 是一个内容中立的 multi-agent graph runtime，并在其上提供可调试的 RP host：角色卡、故事会话、世界书、宏和正则集合都属于 host/用户数据，不由引擎硬编码写作格式。

## 当前能力

- `airp.engine`：GraphRuntime、ProviderNodeRunner、OpenAI-compatible `chat_completions`/`responses`、Agent instruction 宏、Regex Collection、显式 capability registry。
- `airp.host.rp`：卡片投影、Session/Revision、世界书按需读取、存档管理和 RP 工具。
- 集成式 AIRP 工作区：游戏、Worldbook、Agents 与编排、Regex Collections、模型都在游戏页顶部互斥抽屉中编辑；右侧 Monitor 持续显示存档和 Graph/Agent Trace。
- 浏览器前端：`narrative.preview.delta` SSE 流式预览、节点输入输出详情、失败节点定位和整图重跑。
- 角色卡导入：PNG/JSON/TXT、SillyTavern worldbook、卡片脚本提取；脚本资源随 `airp` 包分发。

## 快速开始

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e .

# card_folder 是一张角色卡目录；ROOT 可以传仓库根目录或项目根目录
airp-runtime /path/to/card_folder /path/to/project_root
```

服务默认监听 `0.0.0.0:8765`。浏览器打开 `http://localhost:8765/` 进入 AIRP 游戏工作区；顶部抽屉提供全部 Studio 配置入口，旧的 `/studio` 页面仅保留为兼容入口。先配置 Provider/API key，创建或选择 Agent、Graph、Project，再在游戏抽屉中选择当前 Graph。

源码 checkout 未安装 console script 时可使用：

```bash
PYTHONPATH=src python -m airp.launcher /path/to/card_folder /path/to/project_root
```

## 配置归属

| 内容 | 编辑位置 | 运行时归属 |
| --- | --- | --- |
| Provider URL、API format、模型、key | Studio → Providers | Workspace library + Secret Store |
| Agent 名称、instruction、模型参数、advanced JSON、工具和 Regex Collection | Studio → Agents | Workspace library |
| Graph 节点顺序与 Agent 绑定 | Studio → Graphs | Workspace library |
| 角色卡字段、开场、世界书绑定 | 游戏 → 游戏抽屉 | Workspace projects |
| 世界书条目 | 游戏 → 世界书抽屉 | Workspace worldbooks |
| 会话、revision、事件、投影 | 游戏运行时 / Monitor | 卡片目录 + Workspace sessions/runtime |

Agent instruction 是唯一的写作软约束入口。引擎不会替用户规定文风、人称、NSFW、字数、标签或正文格式；需要这些要求时，直接在 Agent instruction 或宏中编辑。

## 目录结构

```text
src/airp/
  engine/       内容中立的 graph/provider/context/regex 逻辑
  host/rp/      RP session、card projection、worldbook tools
  web/          wheel 内置只读网页资源
  resources/    随包分发的卡片脚本运行资源
tests/          Python 回归与黄金路径测试
docs/           架构、ADR、状态与迁移说明
```

用户可变 Studio 数据默认在 `~/.local/share/airp`，也可通过 `AIRP_DATA_DIR`/Workspace API 指定。仓库不再依赖 `skills/` 作为生产代码、测试入口或网页资源；旧的被忽略运行态文件不会被 canonical runtime 读取。

## 验证

```bash
pytest -q
python3 -m compileall -q src tests
pip wheel --no-deps --no-build-isolation .
npm install --ignore-scripts   # 仅用于角色卡脚本 smoke test
```

真实 Provider 测试是 opt-in：设置对应的 secret 后运行 `pytest -q tests/test_real_deepseek_e2e.py`。

## 设计原则

1. Engine 与 RP host 分层，Graph node 与 runtime lifecycle 独立。
2. 配置由 Studio/Workspace 持有，游戏页只选择激活 Graph。
3. 一节点失败即整图失败；用户点击重跑时重新执行整图。
4. 所有节点输入、输出、模型调用和工具调用都可在 Trace 中审计。
5. 兼容逻辑只留在明确的 `airp.compat`、导入器和投影边界，不再保留第二套旧 runtime。
