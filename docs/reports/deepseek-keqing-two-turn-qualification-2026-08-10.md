# DeepSeek 刻晴双回合最小闭环验收

- 日期：2026-08-10
- 代码版本：`f36d315e7a8609e3512de9673410de9a93371a40`
- Provider Profile：`legacy-deepseek`
- Model：`deepseek-v4-flash`
- 范围：默认协作套件、Pi Core sidecar、真实 OpenAI-compatible DeepSeek 流式调用、SillyTavern `刻晴.json` 游戏抽屉导入。

## 前置与自动化

在临时 Workspace 中通过真实游戏抽屉导入角色卡，创建名为“刻晴双回合验收”的新存档，并在 Monitor 的唯一 Graph 下拉中选择 `default-two-round-review`。

启动前只检查了 Provider API 的 `key_configured: true`；没有读取、打印或复制 API Key。默认对象的安全可见字段为：

- Writer / Reviewer：`legacy-deepseek`、`deepseek-v4-flash`、`max_output_tokens: 8000`；
- Graph：`default-two-round-review`，输出节点 `final-writer`；
- Regex：Writer 绑定 `default-content`，最终输出要求严格 `<content>...</content>` 包装。

Keyless 回归在真实调用前执行：

```text
env -u DEEPSEEK_API_KEY -u AIRP_RUN_REAL_QUALIFICATION python3 -m pytest -q
# 225 passed, 2 skipped, 1 Chrome worldbook-drawer timeout
env -u DEEPSEEK_API_KEY -u AIRP_RUN_REAL_QUALIFICATION \
  python3 -m pytest -q tests/test_worldbook_drawer.py::test_worldbook_drawer_delete_button_deletes_unbound_worldbook
# 1 passed
python3 -m compileall -q src tests
npm run test:browser
# 19 passed
git diff --check
```

`compileall`、浏览器套件和 diff 检查均通过。Python 唯一失败是独立重跑通过的 Chrome 超时，未用于判断真实 Provider 结果。

## 真实两回合结果

Project：`project-b276edc02ef1`。Session：`session-8bbde70fc2da`。没有 Graph retry、失败、取消或中断事件。

| Revision | Task | Graph Run | Commit | 模型调用 | 总 Token | 延迟总计 |
|---|---|---|---|---:|---:|---:|
| 1 | `c48a5cf0-68f4-4f73-8c2b-eb4c08d400b9` | `3cece212-d6d8-4b79-8d1a-5f657bae12e3` | `7b1a796f-2d6d-44ef-a7e4-1b5624e7b980` | 5 | 36,904 | 118,115 ms |
| 2 | `d07270c6-c7a3-417e-b659-5559bd11c17d` | `edbf5ae2-33ac-4d15-a176-6f83ee1b8b99` | `4b5bd9a2-ca9e-49e3-af95-ac572e04e670` | 5 | 61,582 | 209,496 ms |

两次 Graph Run 的节点顺序均为：

```text
writer.loop1 -> reviewer.loop1 -> writer.loop2 -> reviewer.loop2 -> final-writer
```

两轮全部 10 个 Node Run 状态为 `succeeded`。每轮均记录四次非空 handoff：

```text
writer.loop1 -> reviewer.loop1
reviewer.loop1 -> writer.loop2
writer.loop2 -> reviewer.loop2
reviewer.loop2 -> final-writer
```

最终正文均由 Writer 的 Regex 变换得到，未包含 `<content>` 或 `</content>`：

| Revision | 正文长度 | SHA-256 |
|---|---:|---|
| 1 | 778 | `3643f1710f11be1e765eb79befab9ac4da4c67c184fbefb4a7a8a9542402ded1` |
| 2 | 982 | `1733ce9eca4b7ac7514b6797504e219837b026446e7784c1497723a0b5f1be0c` |

第二回合的 `writer.loop1` 冻结 system prompt 中可观察到第一回合的唯一线索“卯三”，证明连续性来自正式已提交历史而不是临时浏览器文本。

## 人工正文核验

通过。只记录必要摘录，不保存完整卡片或完整回复：

- 第一回合将“卯三”青铜货签与东侧货栈账目问题建立因果联系，并由刻晴提出同行调查；
- 第二回合承接货签、东侧货栈和调查时序，指定可核查的下一位账房联系人与行动顺序；
- 两轮都直接回应玩家动作，没有输出格式标签；人物语气、地点、线索和因果关系未见明显矛盾或凭空知情。

本次仅判断正文是否正确、连续和可游玩，不对文笔风格评分。

## 保密与发布边界

本报告不包含 API Key、Authorization/Cookie、完整请求消息、完整 Agent prompt、角色卡正文、Worldbook 正文或完整模型输出。原始运行在验收后从临时 Workspace 清理。

这证明默认协作套件的最小真实两回合闭环可用；它**不**构成 ADR-0022 要求的真实 Provider 20 回合发布 qualification。DeepSeek 路由仍为 `Experimental / 未验证`，直到完成独立的 20 回合、SSE 重连和 Runtime 重启证据。
