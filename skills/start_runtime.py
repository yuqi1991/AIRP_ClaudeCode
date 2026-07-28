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


def _deliver_opening(
    card_folder: Path,
    styles: Path,
    runtime,
    *,
    mock: bool,
    runtime_config=None,
    provider=None,
) -> str:
    """Write the opening turn (index 0, AI-only). Card first_mes preferred;
    otherwise generate via the real provider. Returns 'first_mes' | 'generated'."""
    import handler
    from engine.render import resolve_card_macros

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
        runtime.capture_opening_from_chat_log()
        return "placeholder"

    from engine.provider import (
        AbortSignal,
        ProviderDelta,
        ProviderError,
        ProviderRequest,
        ProviderResult,
        RealProviderAdapter,
        UsageRecord,
    )
    from engine.turn_parser import parse_turn_text

    config = runtime_config
    if not isinstance(config, dict):
        store = getattr(runtime, "runtime_config_store", None)
        if store is None:
            raise ValueError("generated opening requires runtime config")
        config = store.freeze().data
    graph = config.get("graph") or {}
    nodes = [node for node in graph.get("nodes", []) if node.get("enabled", True)]
    if not nodes or nodes[-1].get("role") != "narrative_director":
        raise ValueError("generated opening requires a final narrative_director node")
    node = nodes[-1]
    instruction = (
        "请根据角色设定生成一段自然的中文开场叙事。不要虚构玩家已经做出的行动。"
        "输出 <content>、<summary> 和 <options>；如需初始化变量，可输出 <UpdateVariable>。"
    )
    if node.get("instruction"):
        instruction += "\n本次写作节点指令：" + node["instruction"]
    compiled = runtime.compile_opening_context(instruction)
    adapter = provider or RealProviderAdapter(
        mock=False,
        model=node["model"],
        base_url="https://api.deepseek.com",
        provider=node["provider"],
    )
    request = ProviderRequest(
        messages=compiled.payload,
        tools=[],
        model=node["model"],
        metadata={
            "session_id": runtime.session_id,
            "phase": "opening",
            "payload_hash": compiled.payload_hash,
        },
    )
    usage = UsageRecord()
    raw_text = ""
    for attempt in range(node["max_retries"] + 1):
        chunks = []
        usage = UsageRecord()
        try:
            for item in adapter.stream(request, AbortSignal()):
                if isinstance(item, ProviderDelta):
                    if item.tool_call is not None:
                        raise RuntimeError("opening provider returned an unexpected tool call")
                    if item.text:
                        chunks.append(item.text)
                elif isinstance(item, ProviderResult):
                    usage = item.usage
            raw_text = "".join(chunks).strip()
            break
        except ProviderError as exc:
            if not exc.retryable or attempt >= node["max_retries"]:
                raise
    if not raw_text:
        raise RuntimeError("opening provider returned empty content")
    draft = parse_turn_text(raw_text)
    if not draft.content.strip():
        raise RuntimeError("opening provider returned no visible content")
    tokens = {
        "in": usage.prompt_tokens,
        "out": usage.completion_tokens,
        "total": usage.total_tokens,
    }
    handler.append_turn(
        str(card_folder),
        content=draft.content,
        summary=draft.summary,
        options=draft.options,
        is_opening=True,
        tokens=tokens,
        full_text=raw_text,
        projection_root=styles,
    )
    runtime.capture_opening_from_chat_log(
        event_type="session.opening_generated",
        event_payload={
            "graph_id": config.get("graph_id"),
            "model": node["model"],
            "preset_id": config.get("preset_id"),
        },
    )
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
    imported_card_data = card_folder / ".card_data.json"
    if not session_init.exists() and not imported_card_data.exists():
        print("[start_runtime] 导入卡片…", file=sys.stderr)
        r = subprocess.run([sys.executable, str(SKILLS / "import_prepare.py"),
                            str(card_folder), str(root)],
                           capture_output=True, text=True)
        if r.returncode != 0:
            _die(f"import_prepare 失败: {r.stderr[:500]}")

    # 4. Construct runtime. Each task resolves its executor from the graph frozen
    # in its source snapshot, so config edits affect only later tasks.
    from engine.context_compiler import ContextPolicy
    from engine.executor_factory import RuntimeExecutorFactory
    from engine.provider_profiles import ProviderProfileService
    from engine.runtime import MultiTurnFakeExecutor, SessionTurnRuntime
    from engine.runtime_config import RuntimeConfigStore
    from engine.session_manager import SessionManager
    from engine.secret_store import LocalSecretStore
    from engine.studio_library import ProviderProfileStore

    config_store = RuntimeConfigStore(styles)
    frozen_config = config_store.freeze().data
    graph = frozen_config["graph"]
    manifest_policy = ContextPolicy(
        version=f"runtime-v1:{frozen_config['preset_id']}",
        token_budget=frozen_config["preset"]["token_budget"],
    )
    provider_profile_service = ProviderProfileService(
        ProviderProfileStore(styles),
        LocalSecretStore(styles / "studio" / "secrets.json"),
    )
    executor_factory = RuntimeExecutorFactory(
        mock=mock,
        base_url="https://api.deepseek.com",
        provider_profile_service=provider_profile_service,
    )
    database_path = card_folder / ".runtime.sqlite3"

    def build_runtime(session_id, *, bootstrap_legacy_history=False):
        return SessionTurnRuntime(
            database_path=database_path,
            card_folder=str(card_folder),
            projection_root=styles,
            # The factory is authoritative for every task; retain this fallback for
            # callers and legacy test helpers that inspect or swap runtime.executor.
            executor=MultiTurnFakeExecutor(),
            session_id=session_id,
            session_settings=frozen_config["settings"],
            manifest_policy=manifest_policy,
            runtime_config_store=config_store,
            executor_factory=executor_factory,
            max_commit_validation_retries=graph["commit_validation_retries"],
            bootstrap_legacy_history=bootstrap_legacy_history,
        )

    active_session_id = SessionManager.load_active_session_id(database_path)
    runtime = build_runtime(
        active_session_id,
        bootstrap_legacy_history=active_session_id == "local",
    )

    # 5. Deliver opening (only if chat_log is empty — no turn 0 yet) OR rebuild
    # projection from an existing save so the browser reflects the save instead
    # of import_prepare's placeholder content.js.
    if runtime.opening_turn() is None and runtime.active_revision() == 0:
        origin = _deliver_opening(
            card_folder,
            styles,
            runtime,
            mock=mock,
            runtime_config=frozen_config,
        )
        runtime.resume_projection()
        print(f"[start_runtime] 开场已交付（来源: {origin}）", file=sys.stderr)
    else:
        runtime.resume_projection()
        print(
            f"[start_runtime] 已恢复存档 {runtime.session_id} "
            f"（revision {runtime.active_revision()}），projection 已重建",
            file=sys.stderr,
        )

    session_manager = SessionManager(
        runtime,
        build_runtime,
        default_opening=runtime.opening_turn(),
    )

    # 6. Start unified server on :8765
    from runtime_server import SessionRuntimeServer
    server = SessionRuntimeServer(
        runtime,
        host="0.0.0.0",
        port=PORT,
        static_root=styles,
        preset_root=config_store.preset_root,
        graph_root=config_store.graph_root,
        session_manager=session_manager,
    )
    server.start()
    url = f"http://localhost:{PORT}"
    if not _wait_server_ready(url):
        _die(f"服务器未在 {url} 就绪")
    print(json.dumps({"ok": True, "url": url, "mock": mock,
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
