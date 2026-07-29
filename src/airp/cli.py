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


def _ensure_runtime_defaults(styles: Path) -> None:
    """Create the small compatibility config needed by a fresh wheel install."""
    settings = styles / "settings.json"
    presets = styles / "presets"
    graphs = styles / "graphs"
    presets.mkdir(parents=True, exist_ok=True)
    graphs.mkdir(parents=True, exist_ok=True)
    if not settings.exists():
        settings.write_text(
            json.dumps({"runtime": {"preset_id": "default", "graph_id": "default"}}, indent=2),
            encoding="utf-8",
        )
    preset = presets / "default.json"
    if not preset.exists():
        preset.write_text(
            json.dumps(
                {
                    "id": "default",
                    "version": "1",
                    "entries": [
                        {"id": "card-facts", "role": "user", "content": "{{card_facts}}"},
                        {"id": "current-state", "role": "user", "content": "{{current_state}}"},
                        {"id": "player-input", "role": "user", "content": "{{player_input}}"},
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    graph = graphs / "default.json"
    if not graph.exists():
        graph.write_text(
            json.dumps(
                {
                    "id": "default",
                    "version": "1",
                    "mode": "sequential",
                    "nodes": [
                        {
                            "id": "director",
                            "role": "default",
                            "enabled": True,
                            "order": 0,
                            "provider": "deepseek",
                            "model": "",
                        }
                    ],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )


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
    *,
    mock: bool,
    runtime_config=None,
    provider=None,
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

    from airp.engine.provider import (
        AbortSignal,
        ProviderDelta,
        ProviderError,
        ProviderRequest,
        ProviderResult,
        RealProviderAdapter,
        UsageRecord,
    )
    from airp.engine.agent_framework import AgentArtifact

    config = runtime_config
    if not isinstance(config, dict):
        store = getattr(runtime, "runtime_config_store", None)
        if store is None:
            raise ValueError("generated opening requires runtime config")
        config = store.freeze().data
    graph = config.get("graph") or {}
    nodes = [node for node in graph.get("nodes", []) if node.get("enabled", True)]
    if not nodes:
        raise ValueError("generated opening requires at least one enabled graph node")
    node = nodes[-1]
    turn_adapter = getattr(runtime, "turn_adapter", None)
    validate_opening_plan = getattr(turn_adapter, "validate_opening_plan", None)
    if callable(validate_opening_plan):
        validate_opening_plan(config)
    opening_instruction = getattr(turn_adapter, "opening_instruction", None)
    if not callable(opening_instruction):
        raise TypeError("selected TurnAdapter does not support generated openings")
    instruction = opening_instruction(node.get("instruction") or "")
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
        parameters={
            **(node.get("generation") or {}),
            **(node.get("advanced") or {}),
        },
    )
    usage = UsageRecord()
    raw_text = ""
    # A provider error fails the opening phase; retries are explicit graph/run
    # commands and must never be hidden inside a model call.
    for _attempt in range(1):
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
        except ProviderError:
            raise
    if not raw_text:
        raise RuntimeError("opening provider returned empty content")
    if turn_adapter is None or not callable(getattr(turn_adapter, "interpret", None)):
        raise TypeError("generated opening requires a TurnAdapter")
    draft = turn_adapter.interpret(
        AgentArtifact.text(raw_text),
        context={"phase": "opening", "graph_id": config.get("graph_id")},
    )
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
            subprocess.run(["curl", "-sf", "--max-time", "2", f"{url}/v1/session/snapshot"],
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
    from airp.workspace import Workspace

    workspace = Workspace.default().ensure()
    styles = _resolve_styles_root(root, workspace)
    _ensure_web_assets(styles)
    os.environ.setdefault("AIRP_STATIC_ROOT", str(styles))
    _ensure_runtime_defaults(styles)

    # 1. Environment checks
    if not mock and not os.environ.get("DEEPSEEK_API_KEY"):
        _die("DEEPSEEK_API_KEY 未设置（请 source ~/.zshrc 或导出环境变量），或用 --mock 走 FakeProvider")
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

    # 4. Construct the runtime. Normal turns resolve their graph from the
    # Workspace-backed Studio Project; legacy config is opening compatibility.
    from airp.engine.context_compiler import ContextPolicy
    from airp.engine.executor_factory import RuntimeExecutorFactory
    from airp.engine.provider_profiles import ProviderProfileService
    from airp.engine.runtime import MultiTurnFakeExecutor, SessionTurnRuntime
    from airp.engine.runtime_config import RuntimeConfigStore
    from airp.engine.session_manager import SessionManager
    from airp.engine.secret_store import LocalSecretStore
    from airp.engine.studio_library import ProviderProfileStore

    config_store = RuntimeConfigStore(styles)
    frozen_config = config_store.freeze().data
    # Workspace-backed Studio definitions are authoritative for normal turns.
    # Keep the legacy config only for the one-time opening compatibility path;
    # turn snapshots must not freeze presets/settings from the shipped tree.
    manifest_policy = ContextPolicy(version="runtime-v1", token_budget=8000)
    provider_profile_service = ProviderProfileService(
        ProviderProfileStore(styles, workspace=workspace),
        LocalSecretStore(workspace.secrets_path),
    )
    executor_factory = RuntimeExecutorFactory(
        mock=mock,
        base_url="https://api.deepseek.com",
        provider_profile_service=provider_profile_service,
        cwd=root,
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
            session_settings={},
            manifest_policy=manifest_policy,
            runtime_config_store=None,
            executor_factory=executor_factory,
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

    # Construct the server before generated opening delivery so its Studio
    # project selection can configure the Turn Adapter and execution graph.
    from airp.server import SessionRuntimeServer
    server = SessionRuntimeServer(
        runtime,
        host="0.0.0.0",
        port=PORT,
        static_root=styles,
        preset_root=config_store.preset_root,
        graph_root=config_store.graph_root,
        session_manager=session_manager,
        workspace=workspace,
    )

    opening_config = frozen_config
    try:
        selected_graph_id = server._runtime_selection().get("graph_id")
        if selected_graph_id and server.graph_definitions is not None:
            selected_graph = server.graph_definitions.get_graph(selected_graph_id)
            opening_config = {
                **frozen_config,
                "graph_id": selected_graph_id,
                "graph": server._legacy_graph_from_studio(selected_graph),
            }
    except Exception:
        # Legacy config remains a valid opening fallback when no Studio Graph
        # is selected or an old project references a removed Graph.
        opening_config = frozen_config

    opening_provider = None
    if not mock:
        opening_nodes = [
            node for node in (opening_config.get("graph") or {}).get("nodes", [])
            if node.get("enabled", True)
        ]
        opening_node = opening_nodes[-1] if opening_nodes else {}
        profile_id = opening_node.get("provider_profile_id")
        if profile_id:
            opening_provider = provider_profile_service.execution_adapter(
                profile_id,
                opening_node.get("model", ""),
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
            runtime_config=opening_config,
            provider=opening_provider,
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

    # 6. Start unified server on :8765
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
