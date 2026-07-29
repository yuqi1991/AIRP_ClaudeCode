"""engine.context — 启动/回合上下文构建（纯逻辑）。

从 import_prepare.py / round_prepare.py 剪切的上下文构建函数，保持函数名与签名不变。
原文件通过 `from engine.context import ...` 引入同名符号，调用点无需修改。

包含：
- build_import_context: 生成 import_context.txt（启动汇总），连同其专用 helper
  read_json / _walk_vars 一并迁入（这三个 helper 仅被 build_import_context 使用，
  迁入后 engine.context 自包含，避免对 import_prepare 的循环 import）。
- list_initvar_paths: 递归列出 initvar 路径（round_prepare 静态前缀用）。
"""
import json
from pathlib import Path


def read_json(path):
    """Safe JSON file read, returns None on failure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _walk_vars(obj, prefix=""):
    """Recursively list all leaf paths with values for context display."""
    lines = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            lines.extend(_walk_vars(v, f"{prefix}/{k}"))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            lines.extend(_walk_vars(v, f"{prefix}/{i}"))
    else:
        val_str = json.dumps(obj, ensure_ascii=False)
        if len(val_str) > 80:
            val_str = val_str[:80] + "..."
        lines.append(f"  {prefix} = {val_str}")
    return lines


def build_import_context(card_folder: str, styles_dir: Path,
                         import_result: dict) -> tuple:
    """Write import_context.txt — consolidated startup context for the AI.

    Analogous to round_context.txt in the per-round pipeline.  Groups all
    the information the AI currently reads from 8+ separate files into
    one structured document.

    Returns (Path, size_in_bytes).
    """
    card_path = Path(card_folder)
    parts = []

    # ── CARD_INFO ──
    card_name = import_result.get("card_name", "") or "(unknown)"
    world_name = import_result.get("world_name", "") or "(unknown)"
    source_type = import_result.get("source_type", "?")
    source_file = import_result.get("source_file", "")

    parts.append("=== CARD_INFO ===")
    parts.append(f"  Name: {card_name}")
    parts.append(f"  World: {world_name}")
    parts.append(f"  Source: {source_type}")
    if source_file:
        parts.append(f"  File: {source_file}")
    if import_result.get("merged_worldbooks"):
        mw = import_result["merged_worldbooks"]
        parts.append(f"  Merged worldbooks: {mw['files']} files, {mw['entries']} extra entries")
    parts.append("")

    # ── MEMORY_FILES ──
    memory_dir = card_path / "memory"
    parts.append("=== MEMORY_FILES ===")
    if memory_dir.exists():
        for fname in sorted(memory_dir.iterdir()):
            if fname.suffix == ".md" and fname.name != "MEMORY.md":
                desc = ""
                try:
                    content = fname.read_text(encoding="utf-8")
                    for line in content.split("\n"):
                        if line.startswith("description:"):
                            desc = " — " + line.split(":", 1)[1].strip()
                            break
                except Exception:
                    pass
                parts.append(f"  {fname.name}{desc}")
            elif fname.suffix == ".json" and fname.name.startswith("."):
                size = fname.stat().st_size
                parts.append(f"  {fname.name} ({size} bytes)")
    else:
        parts.append("  (no memory directory)")
    parts.append("")

    # ── WORLDBOOK_CATALOG (skill-mode entry list) + USAGE_TASK ──
    wb_index = read_json(card_path / "memory" / ".worldbook_index.json") or []
    parts.append(f"=== WORLDBOOK_CATALOG ({len(wb_index)} entries) ===")
    if wb_index:
        for entry in wb_index:
            title = entry.get("title", "?")
            usage = (entry.get("usage", "") or "").strip()
            parts.append(f"  {title}" if not usage else f"  {title} — {usage}")
    else:
        parts.append("  (none)")
    parts.append("")

    # USAGE_TASK: list entries whose usage is still empty, instruct the AI to fill them
    missing_usage = [e for e in wb_index if not (e.get("usage", "") or "").strip()]
    if missing_usage:
        parts.append(f"=== USAGE_TASK ({len(missing_usage)} entries need usage) ===")
        for entry in missing_usage:
            parts.append(f"  {entry.get('section', '')}  (title: {entry.get('title', '?')})")
        parts.append("")
        parts.append("为以上每条生成一句 usage（≤60字，「讲什么+何时读」，面向检索决策非剧情摘要）。")
        parts.append(f"操作: 读一次 {card_folder}/memory/reference.md，按 ## 标题定位每条正文，")
        parts.append(f"用 Write 把 usage 字段写进 {card_folder}/memory/.worldbook_index.json（保留每条的 title/section）。")
        parts.append("示例: 太阳圣约 → 「能量机制与圣约仪式设定；共鸣/能量恢复/仪式/性场景后果时加载。」")
        parts.append("完成后此段下次导入自动消失（usage 非空即跳过）。")
        parts.append("")
    else:
        parts.append("=== USAGE_READY (all entries have usage) ===")
        parts.append("")

    # ── CARD_STRUCTURE ──
    structure = import_result.get("card_structure", {})
    if structure:
        parts.append("=== CARD_STRUCTURE ===")
        parts.append(f"  has_stages: {structure.get('has_stages', False)}")
        parts.append(f"  has_events: {structure.get('has_events', False)}")
        chars = structure.get("characters", {})
        if chars:
            parts.append(f"  characters ({len(chars)}): {', '.join(chars.keys())}")
    else:
        parts.append("=== CARD_STRUCTURE ===\n  (none)")
    parts.append("")

    # ── INITIAL_VARIABLES ──
    initvar = read_json(card_path / ".initvar.json")
    if initvar:
        parts.append("=== INITIAL_VARIABLES ===")
        var_lines = _walk_vars(initvar)
        if var_lines:
            for line in var_lines:
                parts.append(line)
        else:
            parts.append("  (empty)")
    else:
        parts.append("=== INITIAL_VARIABLES ===\n  (none)")
    parts.append("")

    # (INJECTION_RULES section removed — injection mechanism disabled in skill-mode refactor)

    # ── OPENINGS ──
    openings_count = import_result.get("openings_count", 0)
    parts.append(f"=== OPENINGS ({openings_count} available) ===")
    if openings_count > 0:
        openings = read_json(styles_dir / "openings.json")
        if openings:
            for i, o in enumerate(openings):
                label = o.get("label", "")[:60]
                content_preview = o.get("content", "")[:150]
                parts.append(f"  [{o.get('id', i)}] {label}")
                if i == 0:
                    parts.append(f"       Preview: {content_preview}...")
                    parts.append(f"       (this is the active opening — pre-filled in response.txt)")
    parts.append("")

    # ── SESSION_STATE ──
    parts.append("=== SESSION_STATE ===")
    parts.append(f"  .card_path: written")
    parts.append(f"  state.js: world=\"{world_name}\"")
    parts.append(f"  content.js: placeholder")
    parts.append(f"  chat_log.json: {'created (new)' if import_result.get('chat_log_created') else 'already exists (preserved)'}")
    parts.append(f"  response.txt: {'pre-filled from first_mes' if import_result.get('response_txt_written') else '(empty — AI must generate opening)'}")
    parts.append(f"  .session_init: created")
    parts.append(f"  .initvar.json: {'present' if import_result.get('initvar_keys') else '(none)'}")
    parts.append(f"  .beautify.json: {'present' if import_result.get('beautify_keys') else '(none)'}")
    parts.append(f"  .regex_scripts.json: {'present' if import_result.get('regex_scripts') else '(none)'}")
    parts.append(f"  .injection_rules.json: {'present' if import_result.get('injection_rules') else '(none)'}")
    parts.append("")

    parts.append("=== NEXT_STEPS ===")
    parts.append("  1. Read this file for full startup context")
    parts.append("  2. 若 USAGE_TASK 段存在: 读 reference.md 为缺 usage 的条目各生成一句 usage，写回 .worldbook_index.json（一次性，之后常驻）")
    parts.append("  3. (Optional) Edit state.js: set time/location/env/quest/npcs")
    parts.append("  4. If response.txt is empty: generate opening narrative → write response.txt")
    parts.append("  5. Deliver opening: python handler.py <card_folder> --opening")
    parts.append("  6. ScheduleWakeup to start input monitoring loop")

    # Write
    output_path = styles_dir / "import_context.txt"
    output_text = "\n".join(parts)
    output_path.write_text(output_text, encoding="utf-8")
    return output_path, len(output_text.encode("utf-8"))


def list_initvar_paths(initvar):
    """Recursively list all paths in initvar with current values."""
    lines = []

    def walk(obj, prefix=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                walk(v, f"{prefix}/{k}")
        elif isinstance(obj, list):
            for i, v in enumerate(obj):
                walk(v, f"{prefix}/{i}")
        else:
            lines.append(f"  {prefix} = {json.dumps(obj, ensure_ascii=False)}")

    walk(initvar)
    return "\n".join(lines)
