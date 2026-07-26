#!/usr/bin/env python3
"""start_runtime.py — 启动新 Pi-runtime 的浏览器黄金路径（基本可用）。

并行于 legacy start_server.py，二者择一运行。新 runtime 独占 :8765（启动时
清理 legacy 残留）。流程：
  1. 环境检查（DEEPSEEK_API_KEY / node_modules）
  2. 清理 :8765 残留进程（legacy server.py / 旧 runtime / mvu_server）
  3. 导入卡片（import_prepare，若未导入）
  4. 构造 RealProviderAdapter + ProviderDrivenDirector + SessionTurnRuntime
     (projection_root = skills/styles)
  5. 交付 opening（卡片 first_mes 优先；无则 DeepSeek 生成）
  6. 启动统一服务器 :8765，打印 URL

用法:
  python skills/start_runtime.py <card_folder> <ROOT>
  python skills/start_runtime.py <card_folder> <ROOT> --mock   # FakeProvider，不调真实模型
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

SKILLS = Path(__file__).resolve().parent
sys.path.insert(0, str(SKILLS))

PORT = 8765
MAX_WAIT = 15.0


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
    for pattern in (str(SKILLS / "server.py"), str(SKILLS / "runtime_server.py"),
                    str(SKILLS / "mvu_server.js")):
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


def _deliver_opening(card_folder: Path, styles: Path, runtime, *, mock: bool) -> str:
    """Write the opening turn (index 0, AI-only). Card first_mes preferred;
    otherwise generate via the real provider. Returns 'first_mes' | 'generated'."""
    import handler
    first = _load_first_mes(card_folder)
    if first:
        handler.append_turn(
            str(card_folder),
            content=first["content"],
            summary=first.get("summary", ""),
            options=first.get("options", ""),
            is_opening=True,
            full_text=first["content"],
            projection_root=styles,
        )
        return "first_mes"
    if mock:
        # FakeProvider path has no real model; write a placeholder opening.
        handler.append_turn(
            str(card_folder),
            content="<p>（无 first_mes 且 mock 模式——占位开场。）</p>",
            summary="占位开场",
            options="",
            is_opening=True,
            full_text="<p>占位开场</p>",
            projection_root=styles,
        )
        return "placeholder"
    # Generate an opening via real DeepSeek.
    from engine.director import ProviderDrivenDirector
    director = runtime.executor
    text = "请生成一段开场叙事（两三句中文），用 <content>/<summary>/<options> 标签。"
    result = runtime.submit(text=text, idempotency_key="opening-gen")
    return "generated"


def _wait_server_ready(url: str, timeout: float = MAX_WAIT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            subprocess.run(["curl", "-sf", "--max-time", "2", f"{url}/api/pending"],
                           check=True, capture_output=True, timeout=3)
            return True
        except Exception:
            time.sleep(0.3)
    return False


def main() -> None:
    if len(sys.argv) < 3:
        _die("Usage: python skills/start_runtime.py <card_folder> <ROOT> [--mock]")
    card_folder = Path(sys.argv[1]).resolve()
    root = Path(sys.argv[2]).resolve()
    mock = "--mock" in sys.argv
    styles = SKILLS / "styles"

    # 1. Environment checks
    if not mock and not os.environ.get("DEEPSEEK_API_KEY"):
        _die("DEEPSEEK_API_KEY 未设置（请 source ~/.zshrc 或导出环境变量），或用 --mock 走 FakeProvider")
    if not mock and not (root / "node_modules" / "@earendil-works").is_dir():
        _die("node_modules 未安装（请在仓库根目录运行 npm install）")
    if not card_folder.is_dir():
        _die(f"卡片文件夹不存在: {card_folder}")

    # 2. Clean :8765 + stale processes
    killed = _kill_port(PORT)
    _kill_legacy_skills_processes()
    if killed:
        print(f"[start_runtime] 已清理 :{PORT} 上的 {killed} 个残留进程", file=sys.stderr)

    # 3. Import card if not initialized
    session_init = card_folder / ".session_init"
    if not session_init.exists():
        print("[start_runtime] 导入卡片…", file=sys.stderr)
        r = subprocess.run([sys.executable, str(SKILLS / "import_prepare.py"),
                            str(card_folder), str(root)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            _die(f"import_prepare 失败: {r.stderr[:500]}")

    # 4. Construct runtime with a real (or mock) DeepSeek director
    from engine.context_compiler import ContextPolicy
    # Real cards exceed the 8k default (large card_facts/worldbook/variables);
    # DeepSeek-v4-flash has ample context. Budget fits recent_memory first.
    manifest_policy = ContextPolicy(version="runtime-v1", token_budget=32000)
    if mock:
        from engine.runtime import MultiTurnFakeExecutor, SessionTurnRuntime
        executor = MultiTurnFakeExecutor()
        runtime = SessionTurnRuntime(
            database_path=card_folder / ".runtime.sqlite3",
            card_folder=str(card_folder), projection_root=styles,
            executor=executor, manifest_policy=manifest_policy,
        )
    else:
        from engine.director import ProviderDrivenDirector
        from engine.provider import RealProviderAdapter
        from engine.runtime import SessionTurnRuntime
        adapter = RealProviderAdapter(mock=False, model="deepseek-v4-flash",
                                      base_url="https://api.deepseek.com")
        executor = ProviderDrivenDirector(adapter, max_tool_rounds=8, max_retries=2)
        settings = {}
        sfile = styles / "settings.json"
        if sfile.is_file():
            try:
                settings = json.loads(sfile.read_text(encoding="utf-8"))
            except Exception:
                pass
        runtime = SessionTurnRuntime(
            database_path=card_folder / ".runtime.sqlite3",
            card_folder=str(card_folder), projection_root=styles,
            executor=executor, session_settings=settings,
            manifest_policy=manifest_policy,
        )

    # 5. Deliver opening (only if chat_log is empty — no turn 0 yet) OR rebuild
    # projection from an existing save so the browser reflects the save instead
    # of import_prepare's placeholder content.js.
    log_path = card_folder / "chat_log.json"
    turns = []
    if log_path.is_file():
        try:
            turns = json.loads(log_path.read_text(encoding="utf-8"))
        except Exception:
            turns = []
    if not turns:
        origin = _deliver_opening(card_folder, styles, runtime, mock=mock)
        runtime.capture_opening_from_chat_log()
        runtime.resume_projection()
        print(f"[start_runtime] 开场已交付（来源: {origin}）", file=sys.stderr)
    else:
        runtime.resume_projection()
        print(f"[start_runtime] 已有存档（{len(turns)} 回合），projection 已重建", file=sys.stderr)

    # 6. Start unified server on :8765
    from runtime_server import SessionRuntimeServer
    server = SessionRuntimeServer(runtime, host="127.0.0.1", port=PORT, static_root=styles)
    server.start()
    url = f"http://localhost:{PORT}"
    if not _wait_server_ready(url):
        _die(f"服务器未在 {url} 就绪")
    print(json.dumps({"ok": True, "url": url, "mock": mock,
                      "card": str(card_folder)}, ensure_ascii=False))
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("\n[start_runtime] 关闭…", file=sys.stderr)
        server.stop()


if __name__ == "__main__":
    main()
