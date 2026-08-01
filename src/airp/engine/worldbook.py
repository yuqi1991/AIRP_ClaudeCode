"""世界书 catalog 构建（纯 I/O）。

导入器将原始条目写成可审计的 catalog 与正文文件，Host 再按精确标题加载正文。

catalog 格式（skill 模式）：每条记录含 title/section/usage，供主 agent 按需 Grep
reference.md 取全文；重新导入时按 section 保留已生成的 usage。
"""
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class WorldbookEntry:
    title: str
    content: str
    catalog_hash: str
    reference_hash: str
    content_hash: str


def load_worldbook_entry(card_folder, title):
    memory_dir = Path(card_folder) / "memory"
    catalog_text = (memory_dir / ".worldbook_index.json").read_text(encoding="utf-8")
    reference_text = _read_optional(memory_dir / "reference.md")
    user_text = _read_optional(memory_dir / "user.md")
    return load_worldbook_entry_from_texts(catalog_text, reference_text, user_text, title)


def load_worldbook_entry_from_texts(catalog_text, reference_text, user_text, title):
    if not isinstance(title, str) or not title:
        raise ValueError("worldbook title is required")
    catalog = json.loads(catalog_text)
    matches = [entry for entry in catalog if entry.get("title") == title and entry.get("section") == f"## {title}"]
    if len(matches) != 1:
        raise ValueError("worldbook title is not an exact catalog entry")
    source_text = user_text if "{{user}}" in title else reference_text
    content = extract_exact_title_section(source_text, title)
    return WorldbookEntry(
        title=title,
        content=content,
        catalog_hash=_sha256(catalog_text),
        reference_hash=_sha256(source_text),
        content_hash=_sha256(content),
    )


def _read_optional(path):
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def extract_exact_title_section(markdown, title):
    heading = f"## {title}"
    lines = markdown.splitlines(keepends=True)
    positions = [index for index, line in enumerate(lines) if line.rstrip("\r\n") == heading]
    if len(positions) != 1:
        raise ValueError("worldbook section is missing or duplicated")
    start = positions[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        if lines[index].startswith("## "):
            end = index
            break
    return "".join(lines[start:end]).strip()


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
