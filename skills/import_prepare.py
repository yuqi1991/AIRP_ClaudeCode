#!/usr/bin/env python3
"""
import_prepare.py — 导入/启动预处理管线。

统一完成启动阶段所有机械操作:
  1. 清理残留 Python 进程
  2. 解析角色卡数据 (代理 import_card.run_import)
  3. 初始化 session 文件 (.card_path, state.js, content.js, chat_log.json)
  4. 预填 response.txt (卡片 first_mes)
  5. 写入 import_context.txt (启动阶段汇总上下文)
  6. 输出 JSON 摘要到 stdout

替代 CLAUDE.md「自动启动流程」中的步骤 0/2/4/4.5/5/6 机械操作。

用法:
  python import_prepare.py <卡片文件夹> <ROOT>
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# Both files live in skills/, so direct import works (same as server.py imports handler).
# import_card.run_import() has no stdout side effects after refactoring.
from import_card import run_import

# build_import_context (+ read_json / _walk_vars helpers) extracted to engine.context
from engine.context import build_import_context


# ─── Phase 0: Cleanup ────────────────────────────────────────

def _pgrep(pattern: str) -> set[int]:
    try:
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True, text=True, timeout=5
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return set()

    pids = set()
    for line in result.stdout.splitlines():
        try:
            pids.add(int(line.strip()))
        except ValueError:
            pass
    return pids


def _terminate_pids(pids: set[int], exclude: set[int]) -> int:
    targets = [pid for pid in sorted(pids) if pid not in exclude]
    killed = 0

    for pid in targets:
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except ProcessLookupError:
            pass
        except PermissionError:
            pass

    if targets:
        time.sleep(0.3)

    for pid in targets:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue
        except PermissionError:
            continue
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass

    return killed


def cleanup_residual(styles_dir: Path) -> dict:
    """Kill stale Python processes running this repo's skills scripts."""
    skills_dir = styles_dir.parent.resolve()
    current_pid = os.getpid()
    patterns = {
        str(skills_dir / "server.py"),
        str(skills_dir / "handler.py"),
        str(skills_dir / "round_prepare.py"),
        str(skills_dir / "round_deliver.py"),
    }
    pids = set()
    for pattern in patterns:
        pids.update(_pgrep(pattern))

    killed = _terminate_pids(pids, {current_pid, os.getppid()})

    pending = styles_dir / ".pending"
    pending_existed = pending.exists()
    if pending_existed:
        try:
            pending.unlink()
        except Exception:
            pass

    return {"killed_processes": killed, "stale_pending_cleared": pending_existed}


# ─── Phase 2: Session File Initialization ────────────────────

def write_card_path(card_folder: str, styles_dir: Path) -> str:
    """Write .card_path so server.py can find the active card folder."""
    abs_path = str(Path(card_folder).resolve())
    (styles_dir / ".card_path").write_text(abs_path, encoding="utf-8")
    return abs_path


def init_state_js(styles_dir: Path, card_name: str, world_name: str,
                  card_folder: str) -> None:
    """Write initial state.js with pre-filled world name.

    The AI can later edit state.js for more specific time/location/env/
    quest/npcs values after reading import_context.txt.

    Dual-writes to styles/ and card folder (matching handler.py pattern).
    """
    safe_world = world_name.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    js = (
        "window.STATE = {\n"
        f'  world: "{safe_world}",\n'
        '  stage: "开局",\n'
        '  time: "",\n'
        '  location: "",\n'
        '  env: "",\n'
        '  quest: "",\n'
        "  generatedCount: 0,\n"
        "  totalTokens: 0,\n"
        "  actions: [],\n"
        '  player: "", hp: 0, hpMax: 0, mp: 0, mpMax: 0, exp: 0, expMax: 0, ed: false,\n'
        "  npcs: []\n"
        "};\n"
    )

    (styles_dir / "state.js").write_text(js, encoding="utf-8")
    # Dual write to card folder
    card_state = Path(card_folder) / "state.js"
    card_state.write_text(js, encoding="utf-8")


def init_content_js(styles_dir: Path, card_folder: str) -> None:
    """Write placeholder content.js.

    handler.py's write_content_js() will rebuild this when the first
    turn is appended via handler.py --opening.
    """
    js = (
        "window.CONTENT_HTML = '<div style=\"padding:60px;text-align:center;color:#999;\">正在生成开场...</div>';\n"
        "window.BEAUTIFY_HTML = '';\n"
        "window.SUMMARY_TEXT = '';\n"
        "window.TURN_OPTIONS = [];\n"
        "window.TURN_TOKENS = {};\n"
        "window.MVU_VARIABLES = {};\n"
        "window.MVU_DELTA = {};\n"
        "window.TURN_VARIABLES = [];\n"
        "window.BEAUTIFY_DATA = {};\n"
        "window.REGEX_SCRIPTS = [];\n"
    )
    (styles_dir / "content.js").write_text(js, encoding="utf-8")
    card_content = Path(card_folder) / "content.js"
    card_content.write_text(js, encoding="utf-8")


def init_chat_log(card_folder: str) -> bool:
    """Initialize chat_log.json as empty array if not exists.

    Returns True if created, False if it already existed (re-import).
    """
    path = Path(card_folder) / "chat_log.json"
    if not path.exists():
        path.write_text("[]", encoding="utf-8")
        return True
    return False


# ─── Main Pipeline ──────────────────────────────────────────

def main():
    if len(sys.argv) < 3:
        print(json.dumps({
            "ok": False, "action": "error",
            "error": "Usage: python import_prepare.py <card_folder> <ROOT>"
        }, ensure_ascii=False))
        sys.exit(1)

    card_folder = sys.argv[1]
    root = sys.argv[2]
    styles_dir = Path(root) / "skills" / "styles"
    os.makedirs(styles_dir, exist_ok=True)

    # ══ Phase 0: Cleanup ══
    cleanup_info = cleanup_residual(styles_dir)

    # ══ Phase 1: Card Import ══
    import_result = run_import(card_folder, root)

    card_name = import_result.get("card_name", "")
    world_name = import_result.get("world_name", "")

    # ══ Phase 2: Session Initialization ══
    card_path_abs = write_card_path(card_folder, styles_dir)
    init_state_js(styles_dir, card_name, world_name, card_folder)
    init_content_js(styles_dir, card_folder)
    chat_log_created = init_chat_log(card_folder)

    # Pass chat_log status through to context builder
    import_result["chat_log_created"] = chat_log_created

    # ══ Phase 3: Import Context File ══
    context_path, context_size = build_import_context(
        card_folder, styles_dir, import_result
    )

    # ══ Phase 3.5: Token Checkpoint Init ══
    # Write initial checkpoint so round_deliver can compute deltas.
    # load_checkpoint handles cross-session transcript switching automatically.
    try:
        from engine import tokens as token_stats
        token_stats.save_checkpoint(card_folder)  # no delta — just record baseline offset
    except Exception:
        pass

    # ══ Phase 4: JSON Summary ══
    summary = {
        "ok": True,
        "action": "imported" if import_result.get("status") == "ok" else "partial",
        "card_dir": card_folder,
        "card_name": card_name,
        "world_name": world_name,
        "source_type": import_result.get("source_type", ""),
        "files_written": {
            "card_path": str(styles_dir / ".card_path"),
            "state_js": str(styles_dir / "state.js"),
            "content_js": str(styles_dir / "content.js"),
            "chat_log_created": chat_log_created,
            "import_context": str(context_path),
            "import_context_size": context_size,
            "response_txt_prefilled": import_result.get("response_txt_written", False),
            "openings_json": import_result.get("openings_count", 0) > 0,
            "session_init": import_result.get("session_init", False),
        },
        "openings_count": import_result.get("openings_count", 0),
        "worldbook_entries": import_result.get("worldbook_entries_total", 0),
        "memory": import_result.get("memory", {}),
        "initvar_keys": import_result.get("initvar_keys", []),
        "initvar_source": import_result.get("initvar_source", ""),
        "card_structure": {
            "has_stages": import_result.get("card_structure", {}).get("has_stages", False),
            "has_events": import_result.get("card_structure", {}).get("has_events", False),
            "character_count": len(import_result.get("card_structure", {}).get("characters", {})),
        },
        "cleanup": cleanup_info,
    }

    # Carry forward optional detail keys from import_card
    for key in ["regex_scripts", "beautify_keys", "injection_rules",
                 "schema_fields", "merged_worldbooks"]:
        if key in import_result:
            summary[key] = import_result[key]

    # Cards with no detectable data
    if import_result.get("status") == "no_card_found":
        summary.update({
            "ok": True,
            "action": "no_card_data",
            "warning": "No recognizable card data found (no PNG/JSON/TXT with card format)",
            "files_scanned": import_result.get("files_scanned", {}),
        })

    # Output (with Windows encoding safety)
    try:
        output_str = json.dumps(summary, ensure_ascii=False, indent=2)
        sys.stdout.reconfigure(encoding='utf-8')
        print(output_str)
    except (UnicodeEncodeError, AttributeError):
        print(json.dumps(summary, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
