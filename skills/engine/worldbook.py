"""engine.worldbook — 世界书 catalog 构建（纯 I/O）。

从 import_card.py 剪切的 build_worldbook_index，保持函数名与签名不变。
import_card.py 通过 `from engine.worldbook import build_worldbook_index` 引入同名符号，
调用点（run_import 内 build_worldbook_index(entries, memory_dir)）无需修改。

catalog 格式（skill 模式）：每条记录含 title/section/usage，供主 agent 按需 Grep
reference.md 取全文；重新导入时按 section 保留已生成的 usage。
"""
import json
import os


def build_worldbook_index(entries: list[dict], memory_dir: str) -> dict:
    """从世界书条目生成 .worldbook_index.json —— 世界书条目 catalog（skill 模式）。

    每条记录是 catalog 的一个条目（像 skill 的 description 入口）：
    - title: 条目标题（comment，= reference.md 的 ## 标题原文）
    - section: Grep 定位用的 Markdown 标题（"## {title}"）
    - usage: 一行简述（"讲什么+何时读"），由主 agent 在导入审阅时生成；初始空

    重新导入时按 section 保留已生成的 usage，避免清空主 agent 的工作。
    （keyword/one_liner 等为旧自动匹配机制服务的字段已移除——检索改为主 agent
    读 catalog 后按需 Grep reference.md 取全文。）
    """
    index_path = os.path.join(memory_dir, ".worldbook_index.json")

    # Preserve AI-generated usage across re-imports (keyed by section)
    prev_usage = {}
    if os.path.exists(index_path):
        try:
            with open(index_path, "r", encoding="utf-8") as f:
                for e in json.load(f):
                    sec = e.get("section", "")
                    u = e.get("usage", "")
                    if sec and isinstance(u, str) and u.strip():
                        prev_usage[sec] = u
        except Exception:
            pass

    index = []
    for e in entries:
        content = e.get("content", "")
        if not content.strip():
            continue
        comment = e.get("comment", "")
        section = f"## {comment}"
        index.append({
            "title": comment,
            "section": section,
            "usage": prev_usage.get(section, ""),
        })

    if index:
        with open(index_path, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)

    return {"index_entries": len(index)}
