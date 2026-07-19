#!/usr/bin/env python3
"""
round_prepare.py — 回合预处理管线。

收集 AI 生成叙事所需的全部上下文，输出到单一的 round_context.txt。
替代 CLAUDE.md「每轮处理」步骤 1-5.1 中所有机械性操作。

缓存策略：静态内容放文件开头（前缀缓存命中），动态内容放文件末尾。

用法:
  python round_prepare.py <card_folder> <ROOT>
"""

import json
import os
import re
import sys
from pathlib import Path

# In-process imports replace subprocess calls (was: subprocess.run to these scripts).
from engine import mvu as mvu_check

# list_initvar_paths extracted to engine.context (deep module)
from engine.context import list_initvar_paths


def read_file(path):
    """Safely read a text file, return None on failure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def read_json(path):
    """Safely read a JSON file, return None on failure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


# (grep_reference_md / _keyword_score / _input_matches removed — skill-mode:
#  the AI reads WORLDBOOK_CATALOG and Greps reference.md on demand via shell.)


def main():
    if len(sys.argv) < 3:
        print("Usage: python round_prepare.py <card_folder> <ROOT>", file=sys.stderr)
        sys.exit(1)

    card_folder = sys.argv[1]
    root = sys.argv[2]
    styles_dir = Path(root) / "skills" / "styles"

    # ── Token delta capture (retroactively fixes previous turn) ──
    pending_tokens = {}
    try:
        from engine import tokens as token_stats
        ts_path = token_stats.locate_transcript()
        cp = token_stats.load_checkpoint(card_folder) if ts_path else {}
        t_offset = cp.get("last_byte_offset", 0)

        if cp.get("previous_checkpoint"):
            token_stats.compute_startup_cost(card_folder)
            cp = token_stats.load_checkpoint(card_folder)
            t_offset = cp.get("last_byte_offset", 0)

        usage = token_stats.read_usage_since(ts_path, t_offset) if ts_path else []
        pending_delta = token_stats.compute_delta(usage)

        if pending_delta.get("request_count", 0) > 0:
            pd_in = pending_delta["input_tokens"]
            pd_out = pending_delta["output_tokens"]
            pending_tokens = {
                "round_in": pd_in,
                "round_out": pd_out,
                "round_total": pd_in + pd_out,
                "cache_read": pending_delta["cache_read"],
                "cache_hit": pending_delta["cache_hit_pct"],
            }

            # Retroactively fix the previous AI turn's token data in chat_log
            cl_path = Path(card_folder) / "chat_log.json"
            cl = read_json(cl_path) or []
            if cl:
                prev_turn = cl[-1]
                cum = cp.get("cumulative", {})
                prev_turn["tokens"] = {
                    "in": pd_in,
                    "out": pd_out,
                    "total": pd_in + pd_out,
                    "cache_read": pending_delta["cache_read"],
                    "cache_hit": pending_delta["cache_hit_pct"],
                    "cumulative_in": cum.get("input_tokens", 0) + pd_in,
                    "cumulative_out": cum.get("output_tokens", 0) + pd_out,
                    "cumulative_total": cum.get("input_tokens", 0) + cum.get("output_tokens", 0) + pd_in + pd_out,
                }
                with open(cl_path, "w", encoding="utf-8") as f:
                    json.dump(cl, f, ensure_ascii=False, indent=2)

            # Advance checkpoint to current transcript position
            token_stats.save_checkpoint(card_folder, delta=pending_delta, label="round")
    except Exception:
        pass

    # ── Gather data first ──
    input_path = styles_dir / "input.txt"
    user_input = read_file(input_path) or "(无输入)"
    user_text = user_input.strip()

    settings_path = styles_dir / "settings.json"
    settings = read_json(settings_path) or {}

    project_md = Path(card_folder) / "memory" / "project.md"
    recent_memory = ""
    if project_md.exists():
        raw = read_file(project_md)
        if raw:
            entries = re.split(r"\n(?=## \d{4}-\d{2}-\d{2})", raw)
            recent = entries[-3:] if len(entries) > 3 else entries
            recent_memory = "".join(recent).strip()[:3000]

    wb_index_path = Path(card_folder) / "memory" / ".worldbook_index.json"
    wb_index = read_json(wb_index_path) or []

    card_structure_path = Path(card_folder) / "memory" / ".card_structure.json"
    card_structure = read_json(card_structure_path)

    # (Worldbook matching + injections removed — skill-mode: AI reads WORLDBOOK_CATALOG
    #  in the static prefix and Greps reference.md on demand. No automatic matching.)

    # Variable paths
    mvu_data = None
    try:
        mvu_data = mvu_check.generate_checklist(card_folder)
    except Exception:
        pass

    initvar_path = Path(card_folder) / ".initvar.json"
    initvar = read_json(initvar_path)

    chat_log_path = Path(card_folder) / "chat_log.json"
    chat_log = read_json(chat_log_path) or []

    # ═══════════════════════════════════════════════
    # BUILD OUTPUT — static prefix first (cached),
    # dynamic suffix last (uncached per round).
    # ═══════════════════════════════════════════════

    static_parts = []
    dynamic_parts = []

    # ── STATIC PREFIX (rarely changes, good for prompt cache) ──

    static_parts.append(f"=== WORLDBOOK_CATALOG ({len(wb_index)} entries, skill-mode) ===")
    static_parts.append("  (每条 = 可按需加载的世界书条目。读 USER_INPUT 后挑本轮需要的,用 Grep 取全文:")
    static_parts.append(f"   grep -n -A 200 \"^## {{完整标题}}$\" {card_folder}/memory/reference.md)")
    if wb_index:
        for entry in wb_index:
            title = entry.get("title", "?")
            usage = (entry.get("usage", "") or "").strip()
            static_parts.append(f"  {title}" if not usage else f"  {title} — {usage}")

    if card_structure:
        static_parts.append(f"\n=== CARD_STRUCTURE ===")
        static_parts.append(f"  has_stages: {card_structure.get('has_stages', False)}")
        static_parts.append(f"  has_events: {card_structure.get('has_events', False)}")
        chars = card_structure.get("characters", {})
        if chars:
            static_parts.append(f"  characters: {', '.join(chars.keys())}")
    else:
        static_parts.append("\n=== CARD_STRUCTURE ===\n  (none)")

    static_parts.append("\n=== SETTINGS ===")
    for key in ["style", "nsfw", "person", "wordCount", "antiImpersonation", "bgNpc", "charName"]:
        val = settings.get(key, "未设置")
        static_parts.append(f"  {key}: {val}")

    # Initvar paths are static (never change after card import)
    if initvar:
        static_parts.append("\n=== INITVAR_PATHS (baseline structure) ===")
        static_parts.append(list_initvar_paths(initvar))

    # ── DYNAMIC SUFFIX (changes every round) ──

    dynamic_parts.append("=== USER_INPUT ===")
    dynamic_parts.append(user_text)

    # Pending token delta from previous round's generation
    if pending_tokens:
        dynamic_parts.append("\n=== PENDING_TOKENS ===")
        for k, v in pending_tokens.items():
            dynamic_parts.append(f"  {k}: {v}")

    # (WORLD_MATCHES / INPUT_MATCHES / INJECTIONS sections removed — skill-mode.
    #  The AI now reads WORLDBOOK_CATALOG (static prefix) and Greps reference.md
    #  on demand for the 2-3 entries this round actually needs.)

    # Variable paths — only emit details for sections touched last turn.
    # Full path tree is already in INITVAR_PATHS (static prefix); repeating all
    # 149 paths here was ~5kB of redundant non-cache content per round.
    dynamic_parts.append("\n=== VARIABLE_PATHS ===")
    if mvu_data:
        dynamic_parts.append(f"  Sections: {', '.join(mvu_data.get('sections', []))}")
        dynamic_parts.append(f"  Total paths: {mvu_data.get('total_paths', '?')} (full tree in INITVAR_PATHS above)")
        touched = mvu_data.get("touched_last_turn", [])
        dynamic_parts.append(f"  Touched last turn: {', '.join(touched) if touched else '(none — details omitted, see INITVAR_PATHS)'}")
        checklist = mvu_data.get("checklist", "")
        if checklist and touched:
            touched_set = set(touched)
            emitted_any = False
            for line in checklist.split("\n"):
                if any(sec in line for sec in touched_set):
                    dynamic_parts.append(f"  {line}")
                    emitted_any = True
            if not emitted_any:
                dynamic_parts.append("  (touched sections had no checklist rows)")
        dynamic_parts.append(f"\n  Reminder: {mvu_data.get('reminder', '')}")
    else:
        dynamic_parts.append("  (mvu_check unavailable)")

    # Recent memory
    if recent_memory:
        dynamic_parts.append("\n=== RECENT_MEMORY ===")
        dynamic_parts.append(recent_memory)

    # Recent chat
    if chat_log:
        dynamic_parts.append("\n=== RECENT_CHAT (last 3 turns) ===")
        for entry in chat_log[-3:]:
            idx = entry.get("index", "?")
            user_txt = entry.get("user", "")[:200]
            summary = entry.get("summary", "")[:200]
            ai_txt = re.sub(r"<[^>]+>", "", entry.get("ai", ""))[:300]
            dynamic_parts.append(f"\n  Turn {idx}:")
            dynamic_parts.append(f"    User: {user_txt}")
            dynamic_parts.append(f"    AI: {ai_txt}")
            if summary:
                dynamic_parts.append(f"    Summary: {summary}")
    else:
        dynamic_parts.append("\n=== RECENT_CHAT ===\n  (no history — first turn)")

    # ── Write Output ──
    output_path = styles_dir / "round_context.txt"
    output_text = "\n".join(static_parts + dynamic_parts)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(output_text)

    print(json.dumps({
        "ok": True,
        "output": str(output_path),
        "size": len(output_text),
        "catalog_entries": len(wb_index),
        "usage_filled": sum(1 for e in wb_index if (e.get("usage", "") or "").strip()),
        "is_first_turn": len(chat_log) <= 1
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
