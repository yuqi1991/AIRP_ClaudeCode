#!/usr/bin/env python3
"""start_runtime.py — 启动 AIRP Graph Runtime 的浏览器黄金路径。

并行于 legacy start_server.py，二者择一运行。新 runtime 独占 :8765（启动时
清理 legacy 残留）。流程：
  1. 环境检查（真实模式需要 Provider Profile 中已配置的 Key）
  2. 清理 :8765 残留进程（legacy server.py / 旧 runtime / mvu_server）
  3. 导入卡片（import_prepare，若未导入）
  4. 构造 SessionTurnRuntime；Provider 与 Graph 由 Studio 负责绑定
  5. 交付 opening（卡片 first_mes 优先；无则 DeepSeek 生成）
  6. 启动统一服务器 :8765，打印 URL

用法:
  python skills/start_runtime.py <card_folder> <ROOT>
  python skills/start_runtime.py <card_folder> <ROOT>

``skills/`` 目前只作为过渡入口。生产包入口是 ``airp-runtime``；直接执行
旧脚本仍受支持，方便旧脚本和开发环境迁移。
"""
from __future__ import annotations

import json
import os
import signal
import shutil
import subprocess
import sys
import time
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = Path(
    os.environ.get("AIRP_REPOSITORY_ROOT", str(PACKAGE_ROOT.parents[1]))
).expanduser().resolve()
LEGACY_SKILLS_ROOT = REPOSITORY_ROOT / "skills"

PORT = 8765
MAX_WAIT = 15.0


def _resolve_styles_root(root: Path, workspace) -> Path:
    """Select mutable UI/runtime files without requiring ``skills/``."""
    configured = os.environ.get("AIRP_STATIC_ROOT")
    candidates = (
        Path(configured).expanduser() if configured else None,
        root / "skills" / "styles",
        root / "styles",
        PACKAGE_ROOT / "styles",
        workspace.runtime_root / "styles",
    )
    for candidate in candidates:
        if candidate is not None and candidate.is_dir():
            return candidate.resolve()
    target = workspace.runtime_root / "styles"
    target.mkdir(parents=True, exist_ok=True)
    return target.resolve()


def _ensure_web_assets(styles: Path) -> None:
    """Seed a writable projection with the immutable packaged web assets."""
    if (styles / "index.html").is_file():
        return
    from airp.resources import static_asset_root

    assets = static_asset_root()
    if assets.resolve() == styles.resolve() or not assets.is_dir():
        return
    shutil.copytree(assets, styles, dirs_exist_ok=True)


def _die(msg: str, code: int = 1) -> None:
    print(json.dumps({"ok": False, "error": msg}, ensure_ascii=False), file=sys.stderr)
    sys.exit(code)


def _kill_port(port: int) -> int:
    """Kill any process listening on :port. Returns count killed."""
    try:
        out = subprocess.check_output(["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"],
                                      text=True, timeout=5)
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return 0
    pids = set()
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) > 1:
            try:
                pids.add(int(parts[1]))
            except ValueError:
                pass
    me = os.getpid()
    killed = 0
    for pid in pids:
        if pid == me:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            killed += 1
        except (ProcessLookupError, PermissionError):
            pass
    if killed:
        time.sleep(0.5)
    return killed


def _kill_legacy_skills_processes() -> None:
    """Kill stale legacy server.py / runtime_server.py / mvu_server.js processes."""
    for pattern in (
        str(LEGACY_SKILLS_ROOT / "server.py"),
        str(LEGACY_SKILLS_ROOT / "runtime_server.py"),
        str(LEGACY_SKILLS_ROOT / "mvu_server.js"),
    ):
        try:
            out = subprocess.check_output(["pgrep", "-f", pattern], text=True, timeout=5)
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            continue
        for line in out.splitlines():
            try:
                pid = int(line.strip())
            except ValueError:
                continue
            if pid == os.getpid():
                continue
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass


def _load_first_mes(card_folder: Path) -> dict | None:
    """Return {content, summary, options} from the card's first opening, or None."""
    # openings.json (import_prepare output) first
    op = card_folder / "memory" / "openings.json"
    if not op.is_file():
        op = card_folder / "openings.json"
    if op.is_file():
        try:
            data = json.loads(op.read_text(encoding="utf-8"))
            if isinstance(data, list) and data:
                first = data[0]
                mes = first.get("mes") or first.get("content") or ""
                if mes.strip():
                    return {
                        "content": mes,
                        "summary": first.get("summary", ""),
                        "options": first.get("options", ""),
                    }
        except Exception:
            pass
    # .card_data.json first_mes fallback
    cd = card_folder / ".card_data.json"
    if cd.is_file():
        try:
            data = json.loads(cd.read_text(encoding="utf-8"))
            mes = data.get("first_mes") or (data.get("data", {}) or {}).get("first_mes") or ""
            if mes.strip():
                return {"content": mes, "summary": "", "options": ""}
        except Exception:
            pass
    return None


def _deliver_opening(
    card_folder: Path,
    styles: Path,
    runtime,
) -> str:
    """Write the opening turn (index 0, AI-only). Card first_mes preferred;
    otherwise generate via the real provider. Returns 'first_mes' | 'generated'."""
    from airp import handler
    from airp.engine.render import resolve_card_macros

    card_facts = runtime.card_facts()
    settings = runtime.session_settings if isinstance(runtime.session_settings, dict) else {}
    user_name = settings.get("charName") or settings.get("user") or "旅行者"
    character_name = card_facts.get("name") or ""
    first = _load_first_mes(card_folder)
    if first:
        content = resolve_card_macros(
            first["content"], user_name=user_name, character_name=character_name
        )
        handler.append_turn(
            str(card_folder),
            content=content,
            summary=first.get("summary", ""),
            options=first.get("options", ""),
            is_opening=True,
            full_text=content,
            projection_root=styles,
        )
        runtime.capture_opening_from_chat_log()
        return "first_mes"
    draft = runtime.generate_opening_draft()
    if not draft.content.strip():
        raise RuntimeError("opening provider returned no visible content")
    handler.append_turn(
        str(card_folder),
        content=draft.content,
        summary=draft.summary,
        options=draft.options,
        is_opening=True,
        full_text=draft.content,
        projection_root=styles,
    )
    runtime.capture_opening_from_chat_log(
        event_type="session.opening_generated",
        event_payload={
            "graph_id": runtime.execution_graph_id,
        },
    )
    return "generated"


def _wait_server_ready(url: str, timeout: float = MAX_WAIT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            subprocess.run(["curl", "-sf", "--max-time", "2", f"{url}/v1/session/snapshot"],
                           check=True, capture_output=True, timeout=3)
            return True
        except Exception:
            time.sleep(0.3)
    return False


def main() -> None:
    if len(sys.argv) != 3:
        _die("Usage: python skills/start_runtime.py <card_folder> <ROOT>")
    card_folder = Path(sys.argv[1]).resolve()
    root = Path(sys.argv[2]).resolve()
    from airp.workspace import Workspace

    workspace = Workspace.default().ensure()
    styles = _resolve_styles_root(root, workspace)
    _ensure_web_assets(styles)
    os.environ.setdefault("AIRP_STATIC_ROOT", str(styles))

    # 1. Environment checks. Provider credentials belong to Studio's local
    # SecretStore; startup only needs a selected Graph in real mode.
    if not card_folder.is_dir():
        _die(f"卡片文件夹不存在: {card_folder}")

    # 2. Clean :8765 + stale processes
    killed = _kill_port(PORT)
    _kill_legacy_skills_processes()
    if killed:
        print(f"[start_runtime] 已清理 :{PORT} 上的 {killed} 个残留进程", file=sys.stderr)

    # 3. Import card if not initialized
    session_init = card_folder / ".session_init"
    imported_card_data = card_folder / ".card_data.json"
    if not session_init.exists() and not imported_card_data.exists():
        print("[start_runtime] 导入卡片…", file=sys.stderr)
        from airp.import_prepare import prepare_card

        try:
            prepare_card(card_folder, root, styles_dir=styles)
        except Exception as exc:
            _die(f"import_prepare 失败: {exc}")

    # 4. Construct the runtime. Studio owns all executable configuration.
    from airp.engine.context_compiler import ContextPolicy
    from airp.host.rp.session_runtime import SessionTurnRuntime
    from airp.host.rp.session_manager import SessionManager

    manifest_policy = ContextPolicy(version="runtime-v1", token_budget=8000)
    database_path = card_folder / ".runtime.sqlite3"

    def build_runtime(session_id, *, bootstrap_legacy_history=False):
        return SessionTurnRuntime(
            database_path=database_path,
            card_folder=str(card_folder),
            projection_root=styles,
            session_id=session_id,
            session_settings={},
            manifest_policy=manifest_policy,
            bootstrap_legacy_history=bootstrap_legacy_history,
        )

    active_session_id = SessionManager.load_active_session_id(database_path)
    runtime = build_runtime(
        active_session_id,
        bootstrap_legacy_history=active_session_id == "local",
    )

    session_manager = SessionManager(
        runtime,
        build_runtime,
        default_opening=runtime.opening_turn(),
    )

    # Construct the server before generated opening delivery so its active
    # Studio Graph can configure the execution plan.
    from airp.server import SessionRuntimeServer
    server = SessionRuntimeServer(
        runtime,
        host="0.0.0.0",
        port=PORT,
        static_root=styles,
        session_manager=session_manager,
        workspace=workspace,
    )

    # 5. Deliver opening (only if chat_log is empty — no turn 0 yet) OR rebuild
    # projection from an existing save so the browser reflects the save instead
    # of import_prepare's placeholder content.js.
    if runtime.opening_turn() is None and runtime.active_revision() == 0 and runtime.execution_graph_id:
        origin = _deliver_opening(
            card_folder,
            styles,
            runtime,
        )
        runtime.resume_projection()
        print(f"[start_runtime] 开场已交付（来源: {origin}）", file=sys.stderr)
    elif runtime.opening_turn() is not None or runtime.active_revision() != 0:
        runtime.resume_projection()
        print(
            f"[start_runtime] 已恢复存档 {runtime.session_id} "
            f"（revision {runtime.active_revision()}），projection 已重建",
            file=sys.stderr,
        )
    else:
        print("[start_runtime] 请先在 Studio 中创建并激活 Graph，然后在游戏中开始故事", file=sys.stderr)

    # 6. Start unified server on :8765
    server.start()
    url = f"http://localhost:{PORT}"
    if not _wait_server_ready(url):
        _die(f"服务器未在 {url} 就绪")
    print(json.dumps({"ok": True, "url": url,
                      "card": str(card_folder),
                      "session_id": session_manager.active_session_id}, ensure_ascii=False))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n[start_runtime] 关闭…", file=sys.stderr)
        server.stop()


if __name__ == "__main__":
    main()
