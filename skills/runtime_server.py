"""Thin stdlib HTTP + SSE server for the new Pi-runtime path (Ticket 04).

Parallel to the legacy Claude Code bridge in ``skills/server.py``. This module
MUST NOT import or patch that bridge. It never writes ``input.txt`` / ``.pending``
and never calls ``/api/wait_pending``.

Endpoints
---------
``POST /v1/session/commands/submit``
    Body ``{text, idempotency_key}``. Accepts the command on a **background
    thread** so SSE can stream ``narrative.preview.delta`` while the director
    runs. Returns ``202`` with the task identity as soon as the durable task row
    exists (or the terminal result if the run already finished).

``POST /v1/session/commands/cancel``
    Body ``{task_id?}`` and/or ``{idempotency_key?}``. Propagates abort via the
    command service.

``POST /v1/session/commands/reroll``
    Body ``{revision, idempotency_key}``. Creates a new branch tip reusing the
    original player input; streams via the same SSE channel.

``POST /v1/session/commands/rollback``
    Body ``{revision, idempotency_key}``. Moves the active head without deleting
    history; subsequent submits descend from the new head.

``GET /v1/session/snapshot``
    Active revision, current task, last event sequence.

``GET /v1/session/events?after=<sequence>``
    JSON poll fallback of durable events with ``sequence > after``.

``GET /v1/session/events/stream?after=<sequence>``
    Server-Sent Events. ``id`` = monotonic sequence, ``event`` = runtime type,
    ``data`` = JSON payload including ``sequence`` and ``type``. On connect,
    replays every event after ``after``, then live-tails until the client
    disconnects. Heartbeat comment lines (``: ping``) every
    :data:`SSE_HEARTBEAT_SECONDS` so proxies do not kill the stream.

SSE quiet / lifetime policy
---------------------------
The stream stays open until the client disconnects (or the server is shut
down). There is no automatic close after a terminal task — browsers reconnect
via ``Last-Event-ID`` / ``?after=`` and the server is at-least-once for the gap
(clients dedupe by sequence).

Thread model
------------
* One :class:`~engine.runtime.SessionTurnRuntime` per server (single-session
  tracer bullet, ADR-0004).
* ``submit`` work runs on a daemon worker thread so the request thread and any
  concurrent SSE handlers remain responsive.
* Event reads (``events_after``) do not take the runtime generation lock; the
  director can emit preview events while SSE polls them.
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from engine.agent_definitions import AgentDefinitionError, AgentDefinitionService, AgentDefinitionStore
from engine.commands import SessionCommandService
from engine.graph_definitions import GraphDefinitionError, GraphDefinitionService, GraphDefinitionStore
from engine.graph_runtime import ExecutionPlanCompiler, GraphRuntime
from engine.node_runner import ProviderNodeRunner
from engine.provider import runtime_provider_api_key, set_runtime_provider_override
from engine.provider_profiles import ProviderConnectionError, ProviderProfileService
from engine.project_library import ProjectLibrary, ProjectLibraryError
from engine.runtime import RuntimeEvent, SessionTurnRuntime
from engine.runtime_config import CONFIG_ID_RE, RuntimeConfigError, RuntimeConfigStore
from engine.session_manager import SessionManager, SessionManagerError
from engine.secret_store import LocalSecretStore
from engine.studio_library import ProviderProfileError, ProviderProfileStore
from engine.studio_migration import bootstrap_legacy_runtime_library
from engine.worldbook_library import WorldbookLibrary, WorldbookLibraryError

SSE_HEARTBEAT_SECONDS = 15.0
SSE_POLL_INTERVAL_SECONDS = 0.05
SUBMIT_ACCEPT_WAIT_SECONDS = 2.0
RUNNING_TASK_STATUSES = frozenset({"queued", "leased", "running", "projection_pending"})
DEFAULT_PROVIDER_BASE_URL = "https://api.deepseek.com"
MODEL_DISCOVERY_TIMEOUT_SECONDS = 10
STUDIO_PROVIDER_PATHS = ("/v1/studio/providers", "/api/studio/providers")
STUDIO_AGENT_PATHS = (
    "/v1/studio/agents",
    "/api/studio/agents",
    "/v1/studio/agent-definitions",
    "/api/studio/agent-definitions",
)
STUDIO_PROMPT_PRESET_PATHS = ("/v1/studio/prompt-presets", "/api/studio/prompt-presets")
STUDIO_GRAPH_PATHS = ("/v1/studio/graphs", "/api/studio/graphs")
STUDIO_WORLDBOOK_PATHS = ("/v1/studio/worldbooks", "/api/studio/worldbooks")
STUDIO_PROJECT_PATHS = ("/v1/studio/projects", "/api/studio/projects")
STUDIO_PROJECT_CONTEXT_PATHS = ("/v1/studio/project-context", "/api/studio/project-context")
STUDIO_GRAPH_RUN_PATHS = ("/v1/studio/graph-runs", "/api/studio/graph-runs")
STUDIO_NODE_RUN_PATHS = ("/v1/studio/node-runs", "/api/studio/node-runs")
STUDIO_DEBUG_REPLAY_PATHS = ("/v1/studio/debug-replays", "/api/studio/debug-replays")
AGENT_TRACE_DETAIL_PATH = "/v1/session/agent-traces"


def _event_to_dict(event: RuntimeEvent) -> dict:
    data = dict(event.payload) if isinstance(event.payload, dict) else {"value": event.payload}
    data.setdefault("sequence", event.sequence)
    data.setdefault("type", event.type)
    return data


def _format_sse(event: RuntimeEvent) -> bytes:
    data = json.dumps(_event_to_dict(event), ensure_ascii=False, separators=(",", ":"))
    # Multi-line data is rare; keep a single data line for the tracer bullet.
    return (
        f"id: {event.sequence}\n"
        f"event: {event.type}\n"
        f"data: {data}\n"
        f"\n"
    ).encode("utf-8")


class SessionRuntimeServer:
    """Single-session stdlib HTTP server wrapping :class:`SessionCommandService`."""

    def __init__(
        self,
        runtime: SessionTurnRuntime,
        host: str = "127.0.0.1",
        port: int = 0,
        *,
        command_service: SessionCommandService | None = None,
        heartbeat_seconds: float = SSE_HEARTBEAT_SECONDS,
        poll_interval_seconds: float = SSE_POLL_INTERVAL_SECONDS,
        static_root: str | None = None,
        preset_root: str | None = None,
        graph_root: str | None = None,
        session_manager: SessionManager | None = None,
    ):
        self.runtime = runtime
        self.service = command_service or SessionCommandService(runtime)
        self.session_manager = session_manager
        self.host = host
        self.port = port
        self.heartbeat_seconds = heartbeat_seconds
        self.poll_interval_seconds = poll_interval_seconds
        # Frontend compat: serve static files (index.html/content.js/state.js/...)
        # from styles dir, and adapt the legacy /api/* calls onto the runtime.
        self.static_root = Path(static_root).resolve() if static_root else None
        repo_root = Path(__file__).resolve().parents[1]
        self.preset_root = Path(preset_root).resolve() if preset_root else ((self.static_root / "presets").resolve() if self.static_root else None)
        self.graph_root = Path(graph_root).resolve() if graph_root else ((self.static_root / "graphs").resolve() if self.static_root else (repo_root / "graphs").resolve())
        self.provider_profile_store = ProviderProfileStore(self.static_root) if self.static_root else None
        self.provider_secret_store = LocalSecretStore(self.static_root / "studio" / "secrets.json") if self.static_root else None
        self.provider_profiles = (
            ProviderProfileService(
                self.provider_profile_store,
                self.provider_secret_store,
                discovery_timeout=MODEL_DISCOVERY_TIMEOUT_SECONDS,
            )
            if self.provider_profile_store is not None and self.provider_secret_store is not None
            else None
        )
        self.agent_store = (
            AgentDefinitionStore(
                self.static_root,
                graph_root=self.graph_root,
                preset_root=self.preset_root,
            )
            if self.static_root
            else None
        )
        self.agent_definitions = AgentDefinitionService(self.agent_store) if self.agent_store else None
        # Short alias for callers that use the Studio object name directly.
        self.agents = self.agent_definitions
        self.graph_store = GraphDefinitionStore(self.static_root, agent_store=self.agent_store) if self.static_root else None
        self.graph_definitions = GraphDefinitionService(self.graph_store) if self.graph_store else None
        self.graphs = self.graph_definitions
        self.worldbooks = WorldbookLibrary(self.static_root) if self.static_root else None
        self.projects = (
            ProjectLibrary(self.static_root, worldbooks=self.worldbooks)
            if self.static_root and self.worldbooks is not None
            else None
        )
        self.config_store = (
            RuntimeConfigStore(
                self.static_root,
                preset_root=self.preset_root,
                graph_root=self.graph_root,
            )
            if self.static_root
            else None
        )
        self._bootstrap_legacy_studio_library()
        self._studio_graph_configured = False
        self._configure_runtime_worldbooks()
        self._configure_runtime_studio_graph()
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._submit_threads: list[threading.Thread] = []
        self._submit_results: dict[str, Any] = {}
        self._submit_lock = threading.Lock()
        self._closed = threading.Event()

    # ── lifecycle ──────────────────────────────────────────────────────

    def start(self) -> "SessionRuntimeServer":
        if self._httpd is not None:
            return self
        handler = self._make_handler()
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self._httpd.daemon_threads = True
        # Reflect the actual bound port when port=0.
        self.host, self.port = self._httpd.server_address[:2]
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="session-runtime-http",
            daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._closed.set()
        httpd = self._httpd
        self._httpd = None
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None
        for thread in list(self._submit_threads):
            thread.join(timeout=5)
        self._submit_threads.clear()

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def __enter__(self) -> "SessionRuntimeServer":
        return self.start()

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()

    # ── submit backgrounding ───────────────────────────────────────────

    def submit_async(self, text: str, idempotency_key: str) -> dict:
        """Start ``service.submit`` on a worker thread; return task identity ASAP.

        This is the load-bearing non-blocking path for SSE: the HTTP request
        handler must not wait for the full director run.
        """
        return self._command_async(
            idempotency_key,
            worker_fn=lambda: self._run_and_touch(
                lambda: self.service.submit(text, idempotency_key)
            ),
            thread_name=f"submit-{idempotency_key[:24]}",
        )

    def reroll_async(self, revision: int, idempotency_key: str) -> dict:
        """Background ``service.reroll`` so SSE can stream the new branch tip."""
        return self._command_async(
            idempotency_key,
            worker_fn=lambda: self._run_and_touch(
                lambda: self.service.reroll(
                    revision=revision, idempotency_key=idempotency_key
                )
            ),
            thread_name=f"reroll-{idempotency_key[:24]}",
        )

    def retry_graph_run_async(self, graph_run_id: str, idempotency_key: str) -> dict:
        """Background complete Graph retry using current saved definitions."""
        return self._command_async(
            idempotency_key,
            worker_fn=lambda: self._run_and_touch(
                lambda: self.service.retry_graph_run(
                    graph_run_id=graph_run_id,
                    idempotency_key=idempotency_key,
                )
            ),
            thread_name=f"graph-retry-{idempotency_key[:24]}",
        )

    def debug_replay_command(self, node_run_id: str, idempotency_key: str):
        """Run one isolated replay through the command-service write seam."""
        result = self.service.debug_replay(
            node_run_id=node_run_id,
            idempotency_key=idempotency_key,
        )
        detail = (
            self.runtime.debug_replay_detail(result.debug_replay_id)
            if result.debug_replay_id
            else None
        )
        return result, detail

    def _run_and_touch(self, command):
        result = command()
        if self.session_manager is not None:
            self.session_manager.touch_active()
        return result

    def _command_async(self, idempotency_key: str, worker_fn, thread_name: str) -> dict:
        with self._submit_lock:
            existing = self._submit_results.get(idempotency_key)
            if existing is not None and (existing.get("task_id") or existing.get("finished")):
                # In-flight or finished prior accept for this key.
                finished = bool(existing.get("finished", False))
                ok = existing.get("ok")
                if ok is None:
                    ok = True
                return {
                    "ok": ok,
                    "accepted": not finished,
                    "finished": finished,
                    "task_id": existing.get("task_id"),
                    "commit_id": existing.get("commit_id"),
                    "revision": existing.get("revision"),
                    "status": existing.get("status") or "queued",
                    "error": existing.get("error"),
                    "retryable": bool(existing.get("retryable", False)),
                    "message": existing.get("message"),
                    "graph_run_id": existing.get("graph_run_id"),
                    "debug_replay_id": existing.get("debug_replay_id"),
                }

            slot: dict[str, Any] = {"finished": False, "task_id": None}
            self._submit_results[idempotency_key] = slot

        def worker():
            result = worker_fn()
            with self._submit_lock:
                slot.update(result.to_dict())
                slot["finished"] = True
                if result.task_id:
                    slot["task_id"] = result.task_id

        thread = threading.Thread(
            target=worker,
            name=thread_name,
            daemon=True,
        )
        with self._submit_lock:
            self._submit_threads.append(thread)
        thread.start()

        # Wait briefly for the durable task row (created at the start of submit)
        # so the response can carry a real task_id without blocking on commit.
        deadline = time.monotonic() + SUBMIT_ACCEPT_WAIT_SECONDS
        task_id = None
        while time.monotonic() < deadline:
            task_id = self.runtime.task_id_for_key(idempotency_key)
            if task_id:
                break
            with self._submit_lock:
                if slot.get("finished"):
                    break
            time.sleep(0.01)

        with self._submit_lock:
            if task_id:
                slot["task_id"] = task_id
            finished = bool(slot.get("finished"))
            ok_val = slot.get("ok")
            if ok_val is None:
                ok_val = True if task_id or not finished else False
            payload = {
                "ok": ok_val if finished else (True if task_id or finished else slot.get("ok", True)),
                "accepted": not finished,
                "finished": finished,
                "task_id": slot.get("task_id") or task_id,
                "commit_id": slot.get("commit_id"),
                "revision": slot.get("revision"),
                "status": slot.get("status") or ("queued" if task_id else None),
                "error": slot.get("error"),
                "retryable": bool(slot.get("retryable", False)),
                "message": slot.get("message"),
                "graph_run_id": slot.get("graph_run_id"),
                "debug_replay_id": slot.get("debug_replay_id"),
            }
            if finished and slot.get("ok") is False:
                payload["ok"] = False
            return payload

    def _snapshot_payload(self) -> dict[str, Any]:
        snap = self.service.snapshot()
        data = snap.to_dict()
        current_task = data.get("current_task")
        status = current_task.get("status") if isinstance(current_task, dict) else "idle"
        pending = status in RUNNING_TASK_STATUSES
        data["status"] = status
        data["pending"] = pending
        return data

    def _session_status_payload(self) -> dict[str, Any]:
        snapshot = self._snapshot_payload()
        # Keep the legacy polling response stable; Studio graph traces use the
        # versioned /v1/session/snapshot contract below.
        snapshot.pop("graph_runs", None)
        return {
            "initialized": True,
            "pending": snapshot["pending"],
            "status": snapshot["status"],
            "task_id": (snapshot.get("current_task") or {}).get("task_id"),
            "snapshot": snapshot,
        }

    def _sessions_payload(self) -> dict[str, Any]:
        if self.session_manager is None:
            return {
                "ok": True,
                "active_session_id": self.runtime.session_id,
                "sessions": [
                    {
                        "id": self.runtime.session_id,
                        "title": "主存档",
                        "active": True,
                        "active_revision": self.runtime.active_revision(),
                    }
                ],
            }
        return {
            "ok": True,
            "active_session_id": self.session_manager.active_session_id,
            "sessions": self.session_manager.list_sessions(),
        }

    def _sync_managed_runtime(self) -> None:
        if self.session_manager is None:
            return
        self.runtime = self.session_manager.runtime
        self._studio_graph_configured = False
        self._configure_runtime_worldbooks()
        self._configure_runtime_studio_graph()
        self.service = SessionCommandService(self.runtime)
        with self._submit_lock:
            self._submit_results.clear()

    def _runtime_selection(self) -> dict[str, str | None]:
        if self.config_store is None:
            return {"preset_id": None, "graph_id": None}
        try:
            return self.config_store.selection()
        except RuntimeConfigError:
            return {"preset_id": None, "graph_id": None}

    def _config_collection_payload(self, kind: str) -> dict[str, Any]:
        selection = self._runtime_selection()
        selected_key = "preset_id" if kind == "preset" else "graph_id"
        return {
            "ok": True,
            "kind": kind,
            "selected_id": selection.get(selected_key),
            "items": self._list_json_configs(kind),
        }

    def _runtime_config_payload(self) -> dict[str, Any]:
        return {
            "ok": True,
            "selected": self._runtime_selection(),
            "presets": self._list_json_configs("preset"),
            "graphs": self._studio_graph_options(),
        }

    def _studio_graph_options(self) -> list[dict[str, Any]]:
        """Expose Studio graphs to the small game-page activation selector."""
        if self.graph_definitions is not None:
            try:
                options = [
                    {
                        "id": graph["id"],
                        "name": graph.get("name") or graph["id"],
                        "updated_at": graph.get("updated_at", 0),
                    }
                    for graph in self.graph_definitions.list_graphs()
                ]
                if options:
                    return options
            except GraphDefinitionError:
                pass
        return self._list_json_configs("graph")

    def _bootstrap_legacy_studio_library(self) -> None:
        """Make the active card's legacy files visible in Studio on first boot."""
        if not all(
            (
                self.static_root,
                self.provider_profile_store,
                self.provider_secret_store,
                self.agent_store,
                self.graph_store,
                self.projects,
            )
        ):
            return
        try:
            bootstrap_legacy_runtime_library(
                static_root=self.static_root,
                provider_store=self.provider_profile_store,
                secret_store=self.provider_secret_store,
                agent_store=self.agent_store,
                graph_store=self.graph_store,
                project_store=self.projects,
                project_id=self.runtime.project_id,
                card_facts=self.runtime.card_facts(),
            )
        except Exception:
            # A malformed optional legacy file must not prevent the game from
            # starting; Studio will still expose any valid existing objects.
            return

    # ── Provider configuration (keys are memory-only) ────────────────

    def _active_narrative_node(self) -> tuple[dict | None, str | None]:
        """Return the selected graph final narrative node and graph id."""
        if self.config_store is None:
            return None, None
        try:
            graph_id = self._runtime_selection().get("graph_id")
            if not graph_id:
                return None, None
            graph = self.config_store.read_config("graph", graph_id)
            graph = self.config_store.validate_config("graph", graph_id, graph)
        except (RuntimeConfigError, OSError):
            return None, None
        nodes = [node for node in graph.get("nodes", []) if node.get("enabled", True)]
        if not nodes or nodes[-1].get("role") != "narrative_director":
            return None, graph_id
        return nodes[-1], graph_id

    def _provider_config_payload(self) -> dict[str, Any]:
        node, _ = self._active_narrative_node()
        provider = (node or {}).get("provider", "deepseek")
        settings = self._read_settings()
        provider_settings = settings.get("provider") if isinstance(settings, dict) else {}
        if not isinstance(provider_settings, dict):
            provider_settings = {}
        return {
            "ok": True,
            "provider": provider,
            "base_url": provider_settings.get("base_url") or DEFAULT_PROVIDER_BASE_URL,
            "model": (node or {}).get("model") or "deepseek-v4-flash",
            "key_configured": bool(runtime_provider_api_key(provider) or os.environ.get("DEEPSEEK_API_KEY")),
        }

    def _write_provider_config(self, body: dict) -> tuple[dict[str, Any], int]:
        if not isinstance(body, dict):
            return {"ok": False, "error": "invalid_payload"}, 400
        node, graph_id = self._active_narrative_node()
        if node is None or graph_id is None or self.config_store is None:
            return {"ok": False, "error": "active_narrative_node_unavailable"}, 400
        base_url = body.get("base_url")
        if base_url is not None and (not isinstance(base_url, str) or not base_url.strip()):
            return {"ok": False, "error": "invalid_base_url"}, 400
        model = body.get("model")
        if model is not None and (not isinstance(model, str) or not model.strip()):
            return {"ok": False, "error": "invalid_model"}, 400
        api_key = body.get("api_key")
        if api_key is not None and (not isinstance(api_key, str) or not api_key.strip()):
            return {"ok": False, "error": "invalid_api_key"}, 400
        if base_url is not None:
            settings = self._read_settings()
            provider_settings = settings.get("provider") if isinstance(settings, dict) else {}
            provider_settings = dict(provider_settings) if isinstance(provider_settings, dict) else {}
            provider_settings["base_url"] = base_url.strip().rstrip("/")
            try:
                self.config_store.update_settings({"provider": provider_settings})
            except RuntimeConfigError as exc:
                return {"ok": False, **exc.to_dict()}, 400
        if model is not None:
            try:
                graph = self.config_store.read_config("graph", graph_id)
                final_node_id = node["id"]
                final_node = next((item for item in graph.get("nodes", []) if item.get("id") == final_node_id), None)
                if final_node is None:
                    return {"ok": False, "error": "active_narrative_node_unavailable"}, 400
                final_node["model"] = model.strip()
                self.config_store.write_config("graph", graph_id, graph)
            except (RuntimeConfigError, OSError) as exc:
                return {"ok": False, "error": "invalid_runtime_config", "message": str(exc)}, 400
        if api_key is not None:
            # This key is intentionally process-only: never settings, task data, or events.
            set_runtime_provider_override(node.get("provider", "deepseek"), api_key=api_key.strip())
        return self._provider_config_payload(), 200

    def _discover_provider_models(self, overrides: dict | None = None) -> dict[str, Any]:
        overrides = overrides if isinstance(overrides, dict) else {}
        current = self._provider_config_payload()
        base_url = overrides.get("base_url", current["base_url"])
        api_key = overrides.get("api_key") or runtime_provider_api_key(current["provider"]) or os.environ.get("DEEPSEEK_API_KEY")
        if not isinstance(base_url, str) or not base_url.strip():
            return {"ok": False, "models": [], "error": "invalid_base_url"}
        if not isinstance(api_key, str) or not api_key:
            return {"ok": False, "models": [], "error": "api_key_not_configured"}
        try:
            request = Request(
                base_url.strip().rstrip("/") + "/models",
                headers={"Accept": "application/json", "Authorization": f"Bearer {api_key}"},
                method="GET",
            )
            with urlopen(request, timeout=MODEL_DISCOVERY_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8"))
            raw_models = payload.get("data", []) if isinstance(payload, dict) else []
            models = sorted({item.get("id") for item in raw_models if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]})
            return {"ok": True, "models": models}
        except Exception:
            # Provider exceptions can include request details; do not expose them.
            return {"ok": False, "models": [], "error": "model_discovery_failed"}

    # ── Studio Provider Profiles ──────────────────────────────────────

    def _studio_provider_error(self, exc: ProviderProfileError) -> tuple[dict[str, Any], int]:
        return exc.to_dict(), exc.status

    def _studio_provider_list(self) -> tuple[dict[str, Any], int]:
        if self.provider_profiles is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            return {"ok": True, "profiles": self.provider_profiles.list_profiles()}, 200
        except ProviderProfileError as exc:
            return self._studio_provider_error(exc)

    def _studio_provider_get(self, profile_id: str) -> tuple[dict[str, Any], int]:
        if self.provider_profiles is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            return {"ok": True, "profile": self.provider_profiles.get_profile(profile_id)}, 200
        except ProviderProfileError as exc:
            return self._studio_provider_error(exc)

    def _studio_provider_create(self, body: dict) -> tuple[dict[str, Any], int]:
        if self.provider_profiles is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            return {"ok": True, **self.provider_profiles.create_profile(body)}, 201
        except ProviderProfileError as exc:
            return self._studio_provider_error(exc)
        except ValueError as exc:
            return {"ok": False, "error": "invalid_api_key", "message": str(exc)}, 400

    def _studio_provider_update(self, profile_id: str, body: dict) -> tuple[dict[str, Any], int]:
        if self.provider_profiles is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            return {"ok": True, **self.provider_profiles.update_profile(profile_id, body)}, 200
        except ProviderProfileError as exc:
            return self._studio_provider_error(exc)
        except ValueError as exc:
            return {"ok": False, "error": "invalid_api_key", "message": str(exc)}, 400

    def _studio_provider_delete(self, profile_id: str) -> tuple[dict[str, Any], int]:
        if self.provider_profiles is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            self.provider_profiles.delete_profile(profile_id)
            return {"ok": True, "deleted_id": profile_id}, 200
        except ProviderProfileError as exc:
            return self._studio_provider_error(exc)

    # ── Studio Agent Definitions and prompt previews ──────────────────

    @staticmethod
    def _studio_agent_error(exc: AgentDefinitionError) -> tuple[dict[str, Any], int]:
        return exc.to_dict(), exc.status

    def _studio_agent_list(self) -> tuple[dict[str, Any], int]:
        if self.agent_definitions is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            return {"ok": True, "agents": self.agent_definitions.list_agents()}, 200
        except AgentDefinitionError as exc:
            return self._studio_agent_error(exc)

    def _studio_agent_get(self, agent_id: str) -> tuple[dict[str, Any], int]:
        if self.agent_definitions is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            return {"ok": True, "agent": self.agent_definitions.get_agent(agent_id)}, 200
        except AgentDefinitionError as exc:
            return self._studio_agent_error(exc)

    def _studio_agent_create(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if self.agent_definitions is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            result = self.agent_definitions.create_agent(body)
            self._sync_studio_graphs_to_legacy()
            return {"ok": True, **result}, 201
        except AgentDefinitionError as exc:
            return self._studio_agent_error(exc)

    def _studio_agent_update(self, agent_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if self.agent_definitions is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            result = self.agent_definitions.update_agent(agent_id, body)
            self._sync_studio_graphs_to_legacy()
            return {"ok": True, **result}, 200
        except AgentDefinitionError as exc:
            return self._studio_agent_error(exc)

    def _studio_agent_copy(self, agent_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if self.agent_definitions is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            result = self.agent_definitions.copy_agent(agent_id, body)
            self._sync_studio_graphs_to_legacy()
            return {"ok": True, **result}, 201
        except AgentDefinitionError as exc:
            return self._studio_agent_error(exc)

    def _studio_agent_delete(self, agent_id: str) -> tuple[dict[str, Any], int]:
        if self.agent_definitions is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            self.agent_definitions.delete_agent(agent_id)
            return {"ok": True, "deleted_id": agent_id}, 200
        except AgentDefinitionError as exc:
            return self._studio_agent_error(exc)

    def _studio_agent_preview(self, agent_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if self.agent_definitions is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            return {"ok": True, **self.agent_definitions.preview_agent(agent_id, body)}, 200
        except AgentDefinitionError as exc:
            return self._studio_agent_error(exc)

    def _studio_prompt_preset_list(self) -> tuple[dict[str, Any], int]:
        if self.agent_definitions is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            return {"ok": True, "presets": self.agent_definitions.list_prompt_presets()}, 200
        except AgentDefinitionError as exc:
            return self._studio_agent_error(exc)

    def _studio_prompt_preset_get(self, preset_id: str) -> tuple[dict[str, Any], int]:
        if self.agent_definitions is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            return {"ok": True, "preset": self.agent_definitions.get_prompt_preset(preset_id)}, 200
        except AgentDefinitionError as exc:
            return self._studio_agent_error(exc)

    # ── Studio Graph Definitions ─────────────────────────────────────

    @staticmethod
    def _studio_graph_error(exc: GraphDefinitionError) -> tuple[dict[str, Any], int]:
        return exc.to_dict(), exc.status

    def _studio_graph_list(self) -> tuple[dict[str, Any], int]:
        if self.graph_definitions is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            return {"ok": True, "graphs": self.graph_definitions.list_graphs()}, 200
        except GraphDefinitionError as exc:
            return self._studio_graph_error(exc)

    def _studio_graph_get(self, graph_id: str) -> tuple[dict[str, Any], int]:
        if self.graph_definitions is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            return {"ok": True, "graph": self.graph_definitions.get_graph(graph_id)}, 200
        except GraphDefinitionError as exc:
            return self._studio_graph_error(exc)

    def _studio_graph_create(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if self.graph_definitions is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            result = self.graph_definitions.create_graph(body)
            self._sync_studio_graphs_to_legacy()
            return {"ok": True, **result}, 201
        except GraphDefinitionError as exc:
            return self._studio_graph_error(exc)

    def _studio_graph_update(self, graph_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if self.graph_definitions is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            result = self.graph_definitions.update_graph(graph_id, body)
            self._sync_studio_graphs_to_legacy()
            return {"ok": True, **result}, 200
        except GraphDefinitionError as exc:
            return self._studio_graph_error(exc)

    def _studio_graph_copy(self, graph_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if self.graph_definitions is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            result = self.graph_definitions.copy_graph(graph_id, body)
            self._sync_studio_graphs_to_legacy()
            return {"ok": True, **result}, 201
        except GraphDefinitionError as exc:
            return self._studio_graph_error(exc)

    def _studio_graph_delete(self, graph_id: str) -> tuple[dict[str, Any], int]:
        if self.graph_definitions is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            self.graph_definitions.delete_graph(graph_id)
            return {"ok": True, "deleted_id": graph_id}, 200
        except GraphDefinitionError as exc:
            return self._studio_graph_error(exc)

    def _sync_studio_graphs_to_legacy(self) -> None:
        """Project Studio graph definitions for the legacy startup reader.

        Studio remains authoritative for an attached runtime.  The projection
        is only a compatibility bridge for ``start_runtime.py`` which freezes
        one legacy graph before constructing the HTTP server.
        """
        if self.config_store is None or self.graph_definitions is None or self.agent_store is None:
            return
        try:
            graphs = self.graph_definitions.list_graphs()
        except GraphDefinitionError:
            return
        for graph in graphs:
            try:
                self.config_store.write_config("graph", graph["id"], self._legacy_graph_from_studio(graph))
            except (GraphDefinitionError, AgentDefinitionError, RuntimeConfigError, OSError, ValueError):
                continue

    def _legacy_graph_from_studio(self, graph: dict[str, Any]) -> dict[str, Any]:
        nodes = []
        enabled_nodes = [node for node in graph.get("nodes", []) if node.get("enabled", True)]
        final_node_id = graph.get("output_node_id") or (enabled_nodes[-1]["node_id"] if enabled_nodes else None)
        for index, node in enumerate(graph.get("nodes", [])):
            agent = self.agent_store.get_agent(node["agent_id"])
            provider_profile_id = node.get("provider_profile_id") or agent.get("provider_profile_id")
            model = node.get("model_id") or agent.get("model_id") or "deepseek-v4-flash"
            role = "narrative_director" if node["node_id"] == final_node_id else f"studio_{node['agent_id']}"
            nodes.append(
                {
                    "id": node["node_id"],
                    "role": role,
                    "enabled": node.get("enabled", True),
                    "order": node.get("order", index),
                    "provider": "deepseek",
                    "provider_profile_id": provider_profile_id,
                    "model": model,
                    "max_tool_rounds": 8,
                    "max_retries": 2,
                    "instruction": agent.get("instruction", ""),
                }
            )
        return {
            "id": graph["id"],
            "version": str(graph.get("version") or "1"),
            "mode": "sequential",
            "commit_validation_retries": 3,
            "nodes": nodes,
        }

    # ── Studio Worldbooks and Project bindings ────────────────────────

    def _configure_runtime_worldbooks(self) -> None:
        if self.worldbooks is not None:
            self.runtime.configure_worldbook_library(self.worldbooks.snapshot_for_project)

    def _configure_runtime_studio_graph(self) -> None:
        """Attach saved Studio definitions only when this runtime has a Graph Project.

        A Runtime without a saved Project keeps its existing executor.  This
        preserves the legacy/browser compatibility path while making a saved
        Project's next submission compile from the Studio Library.
        """
        if not all((self.agent_store, self.graphs, self.projects, self.worldbooks, self.provider_profiles)):
            return
        try:
            project = self.projects.get_project(self.runtime.project_id)
        except ProjectLibraryError:
            if self._studio_graph_configured:
                self.runtime.configure_execution_graph(None, None)
                self._studio_graph_configured = False
            return
        if not project.get("graph_id"):
            if self._studio_graph_configured:
                self.runtime.configure_execution_graph(None, None)
                self._studio_graph_configured = False
            return
        compiler = ExecutionPlanCompiler(
            agent_store=self.agent_store,
            graph_store=self.graphs,
            project_store=self.projects,
            worldbook_store=self.worldbooks,
        )
        runner = ProviderNodeRunner(self._provider_for_studio_graph_node)
        self.runtime.configure_execution_graph(
            compiler,
            GraphRuntime(runner),
            project_id=project["id"],
        )
        self._studio_graph_configured = True

    def _provider_for_studio_graph_node(self, node):
        if self.provider_profiles is None:
            raise ValueError("Studio Provider Profiles are unavailable")
        profile_id = node.agent.provider_profile_id
        if not profile_id:
            raise ValueError("Agent Definition requires a Provider Profile")
        model_id = node.model_id or node.agent.model_id
        return self.provider_profiles.execution_adapter(profile_id, model_id or "")

    @staticmethod
    def _studio_worldbook_error(exc: WorldbookLibraryError) -> tuple[dict[str, Any], int]:
        return exc.to_dict(), exc.status

    def _studio_worldbook_action(self, action, *, status=200, key="worldbook"):
        if self.worldbooks is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            value = action()
            if isinstance(value, tuple):
                worldbook, renamed = value
                return {"ok": True, key: worldbook, "renamed_entries": renamed}, status
            return {"ok": True, key: value}, status
        except WorldbookLibraryError as exc:
            return self._studio_worldbook_error(exc)

    def _studio_worldbook_import(self, body):
        if self.worldbooks is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            worldbook, source_format, renamed = self.worldbooks.import_worldbook(body)
            project_id = body.get("project_id")
            if project_id:
                current = self.worldbooks.get_project_bindings(project_id)
                self.worldbooks.set_project_bindings(
                    project_id,
                    {"name": current["name"], "worldbook_ids": [*current["worldbook_ids"], worldbook["id"]]},
                )
            return {
                "ok": True,
                "worldbook": worldbook,
                "source_format": source_format,
                "renamed_entries": renamed,
            }, 201
        except WorldbookLibraryError as exc:
            return self._studio_worldbook_error(exc)

    def _studio_project_bindings(self, project_id, body=None):
        if self.worldbooks is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            if body is not None and self.projects is not None:
                try:
                    project = self.projects.get_project(project_id)
                except ProjectLibraryError as exc:
                    if exc.code != "project_not_found":
                        raise
                    project = None
                if project is not None:
                    project = self.projects.update_project(project_id, body)
                else:
                    project = self.worldbooks.set_project_bindings(project_id, body)
            else:
                project = self.worldbooks.get_project_bindings(project_id)
            self._configure_runtime_studio_graph()
            return {
                "ok": True,
                "project": project,
                "effective_worldbooks": self.worldbooks.effective_worldbooks(project_id),
            }, 200
        except WorldbookLibraryError as exc:
            return self._studio_worldbook_error(exc)
        except ProjectLibraryError as exc:
            return self._studio_project_error(exc)

    @staticmethod
    def _studio_project_error(exc: ProjectLibraryError) -> tuple[dict[str, Any], int]:
        return exc.to_dict(), exc.status

    def _studio_project_action(self, action, *, status=200, key="project"):
        if self.projects is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            project = action()
            self._configure_runtime_studio_graph()
            return {"ok": True, key: project}, status
        except ProjectLibraryError as exc:
            return self._studio_project_error(exc)

    def _studio_project_import(self, body):
        if self.projects is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            return {"ok": True, "project": self.projects.import_card(body)}, 201
        except ProjectLibraryError as exc:
            return self._studio_project_error(exc)

    def _studio_provider_test(self, profile_id: str) -> tuple[dict[str, Any], int]:
        try:
            models = self.provider_profiles.test_connection(profile_id)
            return {"ok": True, "model_ids": models}, 200
        except ProviderProfileError as exc:
            return self._studio_provider_error(exc)
        except ProviderConnectionError as exc:
            return exc.to_dict(), 502

    def _studio_provider_refresh(self, profile_id: str) -> tuple[dict[str, Any], int]:
        try:
            return {"ok": True, "profile": self.provider_profiles.refresh_models(profile_id)}, 200
        except ProviderProfileError as exc:
            return self._studio_provider_error(exc)
        except ProviderConnectionError as exc:
            return exc.to_dict(), 502

    def _studio_provider_delete_secret(self, profile_id: str) -> tuple[dict[str, Any], int]:
        try:
            return {"ok": True, "profile": self.provider_profiles.delete_secret(profile_id)}, 200
        except ProviderProfileError as exc:
            return self._studio_provider_error(exc)

    # ── HTTP handler factory ───────────────────────────────────────────

    def _make_handler(self):
        server_ref = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # noqa: A003 — silence default stderr logs
                return

            def _send_cors_headers(self) -> None:
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, PATCH, DELETE, OPTIONS")
                self.send_header("Access-Control-Allow-Headers", "Content-Type")

            def _read_json(self) -> dict:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    return {}
                raw = self.rfile.read(length)
                if not raw:
                    return {}
                try:
                    data = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return {}
                return data if isinstance(data, dict) else {}

            def _send_json(self, status: int, body: dict) -> None:
                payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self._send_cors_headers()
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)

            def do_OPTIONS(self):  # noqa: N802
                self.send_response(200)
                self._send_cors_headers()
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self):  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                query = parse_qs(parsed.query)

                for prefix in STUDIO_DEBUG_REPLAY_PATHS:
                    if path.startswith(prefix + "/"):
                        replay_id = path[len(prefix) + 1:]
                        if "/" not in replay_id:
                            detail = server_ref.runtime.debug_replay_detail(replay_id)
                            if detail is None:
                                self._send_json(404, {"ok": False, "error": "debug_replay_not_found"})
                            else:
                                self._send_json(200, {"ok": True, "debug_replay": detail})
                            return

                for prefix in STUDIO_NODE_RUN_PATHS:
                    if path.startswith(prefix + "/") and "/replay" in path:
                        suffix = path[len(prefix) + 1:]
                        parts = suffix.split("/")
                        if len(parts) == 2 and parts[1] == "replay":
                            self._send_json(400, {"ok": False, "error": "debug_replay_requires_post"})
                            return

                for prefix in STUDIO_NODE_RUN_PATHS:
                    if path == prefix:
                        self._send_json(200, {"ok": True, "node_runs": []})
                        return
                    if path.startswith(prefix + "/"):
                        node_run_id = path[len(prefix) + 1:]
                        if "/" not in node_run_id:
                            detail = server_ref.runtime.node_run_detail(node_run_id)
                            if detail is None:
                                self._send_json(404, {"ok": False, "error": "node_run_not_found"})
                            else:
                                self._send_json(200, {"ok": True, "node_run": detail})
                            return

                for prefix in STUDIO_GRAPH_RUN_PATHS:
                    if path == prefix:
                        snapshot = server_ref.runtime.graph_runs_snapshot()
                        self._send_json(200, {"ok": True, **snapshot})
                        return
                    if path.startswith(prefix + "/"):
                        graph_run_id = path[len(prefix) + 1:]
                        if "/" not in graph_run_id:
                            snapshot = server_ref.runtime.graph_runs_snapshot() if graph_run_id == "current" else None
                            detail = None
                            if snapshot is not None:
                                detail = snapshot.get("current") or snapshot.get("most_recent")
                            else:
                                detail = server_ref.runtime.graph_run_detail(graph_run_id)
                            if detail is None:
                                self._send_json(404, {"ok": False, "error": "graph_run_not_found"})
                            else:
                                self._send_json(200, {"ok": True, "graph_run": detail})
                            return

                if path == "/v1/session/snapshot":
                    snap = server_ref.service.snapshot()
                    self._send_json(200, snap.to_dict())
                    return

                if path in ("/v1/session/events/stream", "/v1/studio/graph-runs/events/stream"):
                    after = _parse_after(query, self.headers)
                    self._stream_sse(after)
                    return

                if path == AGENT_TRACE_DETAIL_PATH:
                    task_id = (query.get("task_id") or [""])[0]
                    node_id = (query.get("node_id") or [""])[0]
                    detail = server_ref.runtime.agent_trace_detail(task_id, node_id)
                    if detail is None:
                        self._send_json(404, {"ok": False, "error": "agent_trace_not_found"})
                    else:
                        self._send_json(200, {"ok": True, "agent_trace": detail})
                    return

                if path == "/v1/session/events":
                    after = _parse_after(query, self.headers)
                    events = server_ref.service.events_after(after)
                    self._send_json(
                        200,
                        {
                            "after": after,
                            "events": [_event_to_dict(e) for e in events],
                        },
                    )
                    return

                # ── Runtime Studio Provider Profiles ────────────────────
                if path in STUDIO_PROVIDER_PATHS:
                    payload, status = server_ref._studio_provider_list()
                    self._send_json(status, payload)
                    return
                for prefix in STUDIO_PROVIDER_PATHS:
                    if path.startswith(prefix + "/"):
                        profile_id = path[len(prefix) + 1:]
                        if "/" not in profile_id:
                            payload, status = server_ref._studio_provider_get(profile_id)
                            self._send_json(status, payload)
                            return
                        break

                # ── Runtime Studio Agent Definitions ───────────────────
                if path in STUDIO_AGENT_PATHS:
                    payload, status = server_ref._studio_agent_list()
                    self._send_json(status, payload)
                    return
                for prefix in STUDIO_AGENT_PATHS:
                    if path.startswith(prefix + "/"):
                        suffix = path[len(prefix) + 1:]
                        parts = suffix.split("/")
                        if len(parts) == 1:
                            payload, status = server_ref._studio_agent_get(parts[0])
                            self._send_json(status, payload)
                            return
                        break

                # Prompt presets are read-only library inputs for Agent editing.
                if path in STUDIO_PROMPT_PRESET_PATHS:
                    payload, status = server_ref._studio_prompt_preset_list()
                    self._send_json(status, payload)
                    return
                for prefix in STUDIO_PROMPT_PRESET_PATHS:
                    if path.startswith(prefix + "/"):
                        preset_id = path[len(prefix) + 1:]
                        if "/" not in preset_id:
                            payload, status = server_ref._studio_prompt_preset_get(preset_id)
                            self._send_json(status, payload)
                            return
                        break

                # ── Runtime Studio Graph Definitions ──────────────────
                if path in STUDIO_GRAPH_PATHS:
                    payload, status = server_ref._studio_graph_list()
                    self._send_json(status, payload)
                    return
                for prefix in STUDIO_GRAPH_PATHS:
                    if path.startswith(prefix + "/"):
                        suffix = path[len(prefix) + 1:]
                        parts = suffix.split("/")
                        if len(parts) == 1:
                            payload, status = server_ref._studio_graph_get(parts[0])
                            self._send_json(status, payload)
                            return
                        break

                # ── Runtime Studio Worldbooks and Project bindings ─────
                if path in STUDIO_PROJECT_CONTEXT_PATHS:
                    self._send_json(
                        200,
                        {"ok": True, "project_id": server_ref.runtime.project_id},
                    )
                    return
                if path in STUDIO_WORLDBOOK_PATHS:
                    payload, status = server_ref._studio_worldbook_action(
                        server_ref.worldbooks.list_worldbooks if server_ref.worldbooks else lambda: [],
                        key="worldbooks",
                    )
                    self._send_json(status, payload)
                    return
                for prefix in STUDIO_WORLDBOOK_PATHS:
                    if path.startswith(prefix + "/"):
                        suffix = path[len(prefix) + 1:]
                        parts = suffix.split("/")
                        if len(parts) == 2 and parts[1] == "export":
                            payload, status = server_ref._studio_worldbook_action(
                                lambda: server_ref.worldbooks.export_worldbook(parts[0]),
                                key="export",
                            )
                            if status == 200:
                                payload = payload["export"]
                            self._send_json(status, payload)
                            return
                        if len(parts) == 1 and parts[0] != "import":
                            payload, status = server_ref._studio_worldbook_action(
                                lambda: server_ref.worldbooks.get_worldbook(parts[0])
                            )
                            self._send_json(status, payload)
                            return
                        break
                if path in STUDIO_PROJECT_PATHS:
                    payload, status = server_ref._studio_project_action(
                        server_ref.projects.list_projects if server_ref.projects else lambda: [],
                        key="projects",
                    )
                    self._send_json(status, payload)
                    return
                for prefix in STUDIO_PROJECT_PATHS:
                    if path.startswith(prefix + "/"):
                        parts = path[len(prefix) + 1:].split("/")
                        if len(parts) == 1:
                            payload, status = server_ref._studio_project_action(
                                lambda: server_ref.projects.get_project(parts[0]),
                            )
                            self._send_json(status, payload)
                            return
                        if len(parts) == 2 and parts[1] in {"worldbooks", "bindings"}:
                            payload, status = server_ref._studio_project_bindings(parts[0])
                            self._send_json(status, payload)
                            return
                        break

                # ── Frontend compat: file-backed /api/* reads ──────────────
                if path == "/api/pending":
                    payload = server_ref._session_status_payload()
                    payload["ok"] = True
                    self._send_json(200, payload)
                    return
                if path == "/api/openings":
                    self._send_json(200, server_ref._read_openings())
                    return
                if path == "/api/settings":
                    self._send_json(200, server_ref._read_settings())
                    return
                if path == "/api/style-profiles":
                    self._send_json(200, server_ref._read_style_profiles())
                    return
                if path == "/api/session_status":
                    self._send_json(200, server_ref._session_status_payload())
                    return
                if path == "/api/session_snapshot":
                    self._send_json(200, server_ref._snapshot_payload())
                    return
                if path == "/api/sessions":
                    self._send_json(200, server_ref._sessions_payload())
                    return
                if path == "/api/provider/config":
                    self._send_json(200, server_ref._provider_config_payload())
                    return
                if path == "/api/provider/models":
                    self._send_json(200, server_ref._discover_provider_models())
                    return
                if path == "/api/runtime/config":
                    self._send_json(200, server_ref._runtime_config_payload())
                    return
                if path == "/api/runtime/presets":
                    self._send_json(200, server_ref._config_collection_payload("preset"))
                    return
                if path == "/api/runtime/graphs":
                    self._send_json(200, server_ref._config_collection_payload("graph"))
                    return
                if path.startswith("/api/runtime/presets/"):
                    config_id = path[len("/api/runtime/presets/"):]
                    payload, code = server_ref._read_json_config("preset", config_id)
                    self._send_json(code, payload)
                    return
                if path.startswith("/api/runtime/graphs/"):
                    config_id = path[len("/api/runtime/graphs/"):]
                    payload, code = server_ref._read_json_config("graph", config_id)
                    self._send_json(code, payload)
                    return

                # ── Static files from styles dir (index.html/content.js/...) ──
                if server_ref.static_root is not None and self._maybe_serve_static(path):
                    return

                self._send_json(404, {"ok": False, "error": "not_found"})

            def do_POST(self):  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                body = self._read_json()

                for prefix in STUDIO_GRAPH_RUN_PATHS:
                    if path.startswith(prefix + "/"):
                        suffix = path[len(prefix) + 1:]
                        parts = suffix.split("/")
                        if len(parts) == 2 and parts[1] in {"retry", "retry-graph", "retry_graph"}:
                            graph_run_id = parts[0]
                            key = body.get("idempotency_key")
                            if not isinstance(key, str) or not key.strip():
                                self._send_json(
                                    400,
                                    {
                                        "ok": False,
                                        "error": "invalid_command",
                                        "message": "missing idempotency_key",
                                    },
                                )
                                return
                            result = server_ref.retry_graph_run_async(graph_run_id, key)
                            status = 200 if result.get("finished") else 202
                            if result.get("error") in {
                                "unknown_graph_run",
                                "graph_not_retryable",
                                "invalid_graph_input",
                            }:
                                status = 400
                            self._send_json(status, result)
                            return

                for prefix in STUDIO_NODE_RUN_PATHS:
                    if path.startswith(prefix + "/"):
                        suffix = path[len(prefix) + 1:]
                        parts = suffix.split("/")
                        if len(parts) == 2 and parts[1] in {"replay", "debug-replay", "debug_replay"}:
                            key = body.get("idempotency_key")
                            result, detail = server_ref.debug_replay_command(parts[0], key)
                            payload = result.to_dict()
                            if detail is not None:
                                payload["debug_replay"] = detail
                            status = 200 if result.ok else 422
                            if result.error == "invalid_command":
                                status = 400
                            elif result.error == "node_run_not_found":
                                status = 404
                            self._send_json(status, payload)
                            return

                for prefix in STUDIO_DEBUG_REPLAY_PATHS:
                    if path == prefix:
                        node_run_id = body.get("node_run_id") or body.get("source_node_run_id")
                        result, detail = server_ref.debug_replay_command(
                            node_run_id,
                            body.get("idempotency_key"),
                        )
                        payload = result.to_dict()
                        if detail is not None:
                            payload["debug_replay"] = detail
                        status = 200 if result.ok else 422
                        if result.error == "invalid_command":
                            status = 400
                        elif result.error == "node_run_not_found":
                            status = 404
                        self._send_json(status, payload)
                        return

                # ── Runtime Studio Provider Profiles ────────────────────
                if path in STUDIO_PROVIDER_PATHS:
                    payload, status = server_ref._studio_provider_create(body)
                    self._send_json(status, payload)
                    return
                for prefix in STUDIO_PROVIDER_PATHS:
                    if path.startswith(prefix + "/"):
                        suffix = path[len(prefix) + 1:]
                        parts = suffix.split("/")
                        if len(parts) == 2 and parts[1] in {"enable", "disable"}:
                            payload, status = server_ref._studio_provider_update(
                                parts[0], {"enabled": parts[1] == "enable"}
                            )
                            self._send_json(status, payload)
                            return
                        if len(parts) == 2 and parts[1] == "test":
                            payload, status = server_ref._studio_provider_test(parts[0])
                            self._send_json(status, payload)
                            return
                        if len(parts) == 3 and parts[1:] == ["models", "refresh"]:
                            payload, status = server_ref._studio_provider_refresh(parts[0])
                            self._send_json(status, payload)
                            return
                        break

                # ── Runtime Studio Agent Definitions ───────────────────
                if path in STUDIO_AGENT_PATHS:
                    payload, status = server_ref._studio_agent_create(body)
                    self._send_json(status, payload)
                    return
                for prefix in STUDIO_AGENT_PATHS:
                    if path.startswith(prefix + "/"):
                        suffix = path[len(prefix) + 1:]
                        parts = suffix.split("/")
                        if len(parts) == 2 and parts[1] in {"copy", "duplicate"}:
                            payload, status = server_ref._studio_agent_copy(parts[0], body)
                            self._send_json(status, payload)
                            return
                        if len(parts) == 2 and parts[1] in {
                            "prompt-preview",
                            "prompt_preview",
                            "preview",
                        }:
                            payload, status = server_ref._studio_agent_preview(parts[0], body)
                            self._send_json(status, payload)
                            return
                        break

                # ── Runtime Studio Graph Definitions ──────────────────
                if path in STUDIO_GRAPH_PATHS:
                    payload, status = server_ref._studio_graph_create(body)
                    self._send_json(status, payload)
                    return
                for prefix in STUDIO_GRAPH_PATHS:
                    if path.startswith(prefix + "/"):
                        suffix = path[len(prefix) + 1:]
                        parts = suffix.split("/")
                        if len(parts) == 2 and parts[1] in {"copy", "duplicate"}:
                            payload, status = server_ref._studio_graph_copy(parts[0], body)
                            self._send_json(status, payload)
                            return
                        break

                if path in tuple(prefix + "/import" for prefix in STUDIO_WORLDBOOK_PATHS):
                    payload, status = server_ref._studio_worldbook_import(body)
                    self._send_json(status, payload)
                    return
                if path in tuple(prefix + "/import" for prefix in STUDIO_PROJECT_PATHS):
                    payload, status = server_ref._studio_project_import(body)
                    self._send_json(status, payload)
                    return
                if path in STUDIO_PROJECT_PATHS:
                    payload, status = server_ref._studio_project_action(
                        lambda: server_ref.projects.create_project(body), status=201
                    )
                    self._send_json(status, payload)
                    return
                if path in STUDIO_WORLDBOOK_PATHS:
                    payload, status = server_ref._studio_worldbook_action(
                        lambda: server_ref.worldbooks.create_worldbook(body), status=201
                    )
                    self._send_json(status, payload)
                    return
                for prefix in STUDIO_WORLDBOOK_PATHS:
                    if path.startswith(prefix + "/"):
                        parts = path[len(prefix) + 1:].split("/")
                        if len(parts) == 2 and parts[1] == "copy":
                            payload, status = server_ref._studio_worldbook_action(
                                lambda: server_ref.worldbooks.copy_worldbook(parts[0]), status=201
                            )
                            self._send_json(status, payload)
                            return
                        break
                for prefix in STUDIO_PROJECT_PATHS:
                    if path.startswith(prefix + "/"):
                        parts = path[len(prefix) + 1:].split("/")
                        if len(parts) == 2 and parts[1] in {"copy", "duplicate"}:
                            payload, status = server_ref._studio_project_action(
                                lambda: server_ref.projects.copy_project(parts[0], body), status=201
                            )
                            self._send_json(status, payload)
                            return
                        break

                if path == "/api/sessions":
                    if server_ref.session_manager is None:
                        self._send_json(501, {"ok": False, "error": "session_management_unavailable"})
                        return
                    try:
                        session = server_ref.session_manager.create_session(
                            body.get("title") or "新存档"
                        )
                        server_ref._sync_managed_runtime()
                    except SessionManagerError as exc:
                        self._send_session_error(exc)
                        return
                    self._send_json(
                        201,
                        {
                            **server_ref._sessions_payload(),
                            "session": session,
                            "snapshot": server_ref._snapshot_payload(),
                        },
                    )
                    return

                if path == "/api/sessions/switch":
                    if server_ref.session_manager is None:
                        self._send_json(501, {"ok": False, "error": "session_management_unavailable"})
                        return
                    try:
                        session = server_ref.session_manager.switch_session(
                            body.get("session_id")
                        )
                        server_ref._sync_managed_runtime()
                    except SessionManagerError as exc:
                        self._send_session_error(exc)
                        return
                    self._send_json(
                        200,
                        {
                            **server_ref._sessions_payload(),
                            "session": session,
                            "snapshot": server_ref._snapshot_payload(),
                        },
                    )
                    return

                if path == "/api/sessions/rename":
                    if server_ref.session_manager is None:
                        self._send_json(501, {"ok": False, "error": "session_management_unavailable"})
                        return
                    try:
                        session = server_ref.session_manager.rename_session(
                            body.get("session_id"), body.get("title")
                        )
                    except SessionManagerError as exc:
                        self._send_session_error(exc)
                        return
                    self._send_json(
                        200,
                        {**server_ref._sessions_payload(), "session": session},
                    )
                    return

                if path == "/api/provider/models":
                    self._send_json(200, server_ref._discover_provider_models(body))
                    return

                if path == "/v1/session/commands/submit":
                    text = body.get("text", "")
                    key = body.get("idempotency_key", "")
                    if not isinstance(text, str) or not text.strip():
                        self._send_json(
                            400,
                            {
                                "ok": False,
                                "error": "invalid_command",
                                "message": "empty input",
                                "retryable": False,
                            },
                        )
                        return
                    if not isinstance(key, str) or not key.strip():
                        self._send_json(
                            400,
                            {
                                "ok": False,
                                "error": "invalid_command",
                                "message": "missing idempotency_key",
                                "retryable": False,
                            },
                        )
                        return
                    result = server_ref.submit_async(text, key)
                    status = 200 if result.get("finished") else 202
                    if result.get("ok") is False and result.get("finished"):
                        status = 200
                    self._send_json(status, result)
                    return

                if path == "/v1/session/commands/cancel":
                    result = server_ref.service.cancel(
                        task_id=body.get("task_id"),
                        idempotency_key=body.get("idempotency_key"),
                        cancel_idempotency_key=body.get("cancel_idempotency_key"),
                    )
                    code = 200 if result.ok or result.error == "unknown_task" else 400
                    if result.error == "unknown_task":
                        code = 404
                    self._send_json(code, result.to_dict())
                    return

                if path in {
                    "/v1/session/commands/retry-graph",
                    "/v1/session/commands/retry_graph",
                }:
                    graph_run_id = body.get("graph_run_id") or body.get("run_id")
                    key = body.get("idempotency_key")
                    if not isinstance(graph_run_id, str) or not graph_run_id.strip():
                        self._send_json(400, {"ok": False, "error": "invalid_command", "message": "missing graph_run_id"})
                        return
                    if not isinstance(key, str) or not key.strip():
                        self._send_json(400, {"ok": False, "error": "invalid_command", "message": "missing idempotency_key"})
                        return
                    result = server_ref.retry_graph_run_async(graph_run_id, key)
                    status = 200 if result.get("finished") else 202
                    if result.get("error") in {
                        "unknown_graph_run",
                        "graph_not_retryable",
                        "invalid_graph_input",
                    }:
                        status = 400
                    self._send_json(status, result)
                    return

                if path == "/v1/session/commands/reroll":
                    # Reroll may run a full director generation; accept on a
                    # background thread the same way submit does so SSE can
                    # stream preview while the new branch tip is produced.
                    key = body.get("idempotency_key", "")
                    revision = body.get("revision")
                    if not isinstance(key, str) or not key.strip():
                        self._send_json(
                            400,
                            {
                                "ok": False,
                                "error": "invalid_command",
                                "message": "missing idempotency_key",
                                "retryable": False,
                            },
                        )
                        return
                    if revision is None or not isinstance(revision, int):
                        self._send_json(
                            400,
                            {
                                "ok": False,
                                "error": "invalid_command",
                                "message": "revision required",
                                "retryable": False,
                            },
                        )
                        return
                    result = server_ref.reroll_async(revision, key)
                    status = 200 if result.get("finished") else 202
                    if result.get("ok") is False and result.get("finished"):
                        status = 200
                    if result.get("error") in ("unknown_revision", "cannot_reroll_opening"):
                        status = 400
                    self._send_json(status, result)
                    return

                if path == "/v1/session/commands/rollback":
                    result = server_ref.service.rollback(
                        revision=body.get("revision"),
                        idempotency_key=body.get("idempotency_key"),
                    )
                    if server_ref.session_manager is not None:
                        server_ref.session_manager.touch_active()
                    code = 200 if result.ok else 400
                    if result.error == "unknown_revision":
                        code = 404
                    self._send_json(code, result.to_dict())
                    return

                # ── Frontend compat: legacy /api/* → runtime ───────────────
                if path == "/api/submit":
                    text = (body.get("text") or "").strip()
                    if not text:
                        self._send_json(400, {"ok": False, "error": "empty input"})
                        return
                    # Player identity is part of frozen settings/context. Keep
                    # the durable player message byte-for-byte as the user typed
                    # it so the chat bubble does not repeat an identity prefix.
                    submitted_text = text
                    key = body.get("idempotency_key") or f"api-submit-{time.time_ns()}"
                    result = server_ref.submit_async(submitted_text, key)
                    payload = {
                        **result,
                        "submitted_text": submitted_text,
                        "snapshot": server_ref._snapshot_payload(),
                    }
                    # Acknowledge immediately; frontend polls content.js for the result.
                    self._send_json(200, payload)
                    return

                if path == "/api/reroll":
                    head = server_ref.runtime.active_revision()
                    if head <= 0:
                        self._send_json(400, {"ok": False, "error": "no turns to reroll"})
                        return
                    key = body.get("idempotency_key") or f"api-reroll-{time.time_ns()}"
                    result = server_ref.reroll_async(head, key)
                    self._send_json(200, {**result, "snapshot": server_ref._snapshot_payload()})
                    return

                if path == "/api/delete_turns":
                    from_index = body.get("from_index")
                    if not isinstance(from_index, int) or from_index < 0:
                        self._send_json(400, {"ok": False, "error": "cannot resolve rollback target"})
                        return
                    visible = server_ref.runtime.visible_turns()
                    target = 0
                    for turn in visible[:min(from_index, len(visible))]:
                        if turn["revision"] > 0:
                            target = turn["revision"]
                    result = server_ref.service.rollback(revision=target, idempotency_key=f"api-delete-{time.time_ns()}")
                    if server_ref.session_manager is not None:
                        server_ref.session_manager.touch_active()
                    self._send_json(200 if result.ok else 400, {**result.to_dict(), "snapshot": server_ref._snapshot_payload()})
                    return

                if path == "/api/settings":
                    try:
                        settings = server_ref._write_settings(body)
                    except RuntimeConfigError as exc:
                        self._send_json(400, {"ok": False, **exc.to_dict()})
                        return
                    self._send_json(200, {"ok": True, "settings": settings})
                    return

                if path == "/api/switch_opening":
                    ok = server_ref._switch_opening(body.get("opening_id"))
                    self._send_json(
                        200 if ok else 400,
                        {"ok": ok, "snapshot": server_ref._snapshot_payload()},
                    )
                    return

                if path == "/api/style-profiles/delete":
                    name = (body.get("name") or "").strip()
                    ok = server_ref._delete_style_profile(name)
                    self._send_json(200 if ok else 404, {"ok": ok})
                    return

                self._send_json(404, {"ok": False, "error": "not_found"})

            def do_DELETE(self):  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                for prefix in STUDIO_AGENT_PATHS:
                    if path.startswith(prefix + "/"):
                        agent_id = path[len(prefix) + 1:]
                        if "/" not in agent_id:
                            payload, status = server_ref._studio_agent_delete(agent_id)
                            self._send_json(status, payload)
                            return
                        break
                for prefix in STUDIO_PROVIDER_PATHS:
                    if path.startswith(prefix + "/"):
                        suffix = path[len(prefix) + 1:]
                        parts = suffix.split("/")
                        if len(parts) == 2 and parts[1] == "secret":
                            payload, status = server_ref._studio_provider_delete_secret(parts[0])
                            self._send_json(status, payload)
                            return
                        profile_id = suffix
                        if len(parts) == 1:
                            payload, status = server_ref._studio_provider_delete(profile_id)
                            self._send_json(status, payload)
                            return
                        break
                for prefix in STUDIO_GRAPH_PATHS:
                    if path.startswith(prefix + "/"):
                        graph_id = path[len(prefix) + 1:]
                        if "/" not in graph_id:
                            payload, status = server_ref._studio_graph_delete(graph_id)
                            self._send_json(status, payload)
                            return
                        break
                for prefix in STUDIO_WORLDBOOK_PATHS:
                    if path.startswith(prefix + "/"):
                        worldbook_id = path[len(prefix) + 1:]
                        if "/" not in worldbook_id:
                            if server_ref.worldbooks is None:
                                self._send_json(501, {"ok": False, "error": "studio_library_unavailable"})
                                return
                            try:
                                server_ref.worldbooks.delete_worldbook(worldbook_id)
                                self._send_json(200, {"ok": True, "deleted_id": worldbook_id})
                            except WorldbookLibraryError as exc:
                                payload, status = server_ref._studio_worldbook_error(exc)
                                self._send_json(status, payload)
                            return
                        break
                prefix = "/api/sessions/"
                if not path.startswith(prefix):
                    self._send_json(404, {"ok": False, "error": "not_found"})
                    return
                if server_ref.session_manager is None:
                    self._send_json(501, {"ok": False, "error": "session_management_unavailable"})
                    return
                session_id = path[len(prefix):]
                try:
                    result = server_ref.session_manager.delete_session(session_id)
                    server_ref._sync_managed_runtime()
                except SessionManagerError as exc:
                    self._send_session_error(exc)
                    return
                self._send_json(
                    200,
                    {**server_ref._sessions_payload(), **result, "snapshot": server_ref._snapshot_payload()},
                )

            def _send_session_error(self, exc: SessionManagerError):
                if exc.code == "unknown_session":
                    status = 404
                elif exc.code in {"generation_active", "cannot_delete_only_session"}:
                    status = 409
                else:
                    status = 400
                self._send_json(status, exc.to_dict())

            def do_PUT(self):  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                body = self._read_json()

                for prefix in STUDIO_AGENT_PATHS:
                    if path.startswith(prefix + "/"):
                        agent_id = path[len(prefix) + 1:]
                        if "/" not in agent_id:
                            payload, status = server_ref._studio_agent_update(agent_id, body)
                            self._send_json(status, payload)
                            return
                        break

                for prefix in STUDIO_PROVIDER_PATHS:
                    if path.startswith(prefix + "/"):
                        profile_id = path[len(prefix) + 1:]
                        if "/" not in profile_id:
                            payload, status = server_ref._studio_provider_update(profile_id, body)
                            self._send_json(status, payload)
                            return
                        break

                for prefix in STUDIO_GRAPH_PATHS:
                    if path.startswith(prefix + "/"):
                        graph_id = path[len(prefix) + 1:]
                        if "/" not in graph_id:
                            payload, status = server_ref._studio_graph_update(graph_id, body)
                            self._send_json(status, payload)
                            return
                        break

                for prefix in STUDIO_WORLDBOOK_PATHS:
                    if path.startswith(prefix + "/"):
                        worldbook_id = path[len(prefix) + 1:]
                        if "/" not in worldbook_id:
                            payload, status = server_ref._studio_worldbook_action(
                                lambda: server_ref.worldbooks.update_worldbook(worldbook_id, body)
                            )
                            self._send_json(status, payload)
                            return
                        break
                for prefix in STUDIO_PROJECT_PATHS:
                    if path.startswith(prefix + "/"):
                        parts = path[len(prefix) + 1:].split("/")
                        if len(parts) == 1:
                            payload, status = server_ref._studio_project_action(
                                lambda: server_ref.projects.update_project(parts[0], body),
                            )
                            self._send_json(status, payload)
                            return
                        if len(parts) == 2 and parts[1] in {"worldbooks", "bindings"}:
                            payload, status = server_ref._studio_project_bindings(parts[0], body)
                            self._send_json(status, payload)
                            return

                if path == "/api/provider/config":
                    payload, code = server_ref._write_provider_config(body)
                    self._send_json(code, payload)
                    return
                if path == "/api/runtime/config":
                    payload, code = server_ref._write_runtime_selection(body)
                    self._send_json(code, payload)
                    return
                if path.startswith("/api/runtime/presets/"):
                    config_id = path[len("/api/runtime/presets/"):]
                    payload, code = server_ref._write_json_config("preset", config_id, body)
                    self._send_json(code, payload)
                    return
                if path.startswith("/api/runtime/graphs/"):
                    config_id = path[len("/api/runtime/graphs/"):]
                    payload, code = server_ref._write_json_config("graph", config_id, body)
                    self._send_json(code, payload)
                    return

                self._send_json(404, {"ok": False, "error": "not_found"})

            def do_PATCH(self):  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                body = self._read_json()
                for prefix in STUDIO_AGENT_PATHS:
                    if path.startswith(prefix + "/"):
                        agent_id = path[len(prefix) + 1:]
                        if "/" not in agent_id:
                            payload, status = server_ref._studio_agent_update(agent_id, body)
                            self._send_json(status, payload)
                            return
                        break
                for prefix in STUDIO_PROVIDER_PATHS:
                    if path.startswith(prefix + "/"):
                        profile_id = path[len(prefix) + 1:]
                        if "/" not in profile_id:
                            payload, status = server_ref._studio_provider_update(profile_id, body)
                            self._send_json(status, payload)
                            return
                        break
                for prefix in STUDIO_GRAPH_PATHS:
                    if path.startswith(prefix + "/"):
                        graph_id = path[len(prefix) + 1:]
                        if "/" not in graph_id:
                            payload, status = server_ref._studio_graph_update(graph_id, body)
                            self._send_json(status, payload)
                            return
                        break
                for prefix in STUDIO_PROJECT_PATHS:
                    if path.startswith(prefix + "/"):
                        suffix = path[len(prefix) + 1:]
                        if "/" not in suffix:
                            payload, status = server_ref._studio_project_action(
                                lambda: server_ref.projects.update_project(suffix, body),
                            )
                            self._send_json(status, payload)
                            return
                        break
                self._send_json(404, {"ok": False, "error": "not_found"})

            def _stream_sse(self, after: int) -> None:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, no-store")
                self.send_header("Connection", "keep-alive")
                # Disable proxy buffering where supported (nginx etc.).
                self.send_header("X-Accel-Buffering", "no")
                self.end_headers()

                last = after
                last_ping = time.monotonic()
                try:
                    # Immediate comment so clients (and proxies) see bytes before
                    # the first durable event and do not treat the stream as idle.
                    self.wfile.write(b": connected\n\n")
                    self.wfile.flush()
                    while not server_ref._closed.is_set():
                        events = server_ref.service.events_after(last)
                        for event in events:
                            self.wfile.write(_format_sse(event))
                            self.wfile.flush()
                            last = event.sequence
                        now = time.monotonic()
                        if now - last_ping >= server_ref.heartbeat_seconds:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                            last_ping = now
                        time.sleep(server_ref.poll_interval_seconds)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return
                except OSError:
                    return

            def _maybe_serve_static(self, path: str) -> bool:
                """Serve a static file from styles dir. Returns True if handled."""
                root = server_ref.static_root
                if root is None:
                    return False
                if path in ("", "/"):
                    rel = "index.html"
                elif path == "/studio":
                    rel = "studio.html"
                else:
                    rel = path.lstrip("/")
                target = (root / rel).resolve()
                # Prevent path traversal outside static_root.
                try:
                    target.relative_to(root.resolve())
                except ValueError:
                    self._send_json(403, {"ok": False, "error": "forbidden"})
                    return True
                if not target.is_file():
                    return False
                data = target.read_bytes()
                ctype = _content_type_for(target.name)
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self._send_cors_headers()
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(data)
                return True

        return Handler

    # ── Frontend-compat file helpers (read/write styles dir) ────────────

    def _read_openings(self) -> list:
        candidates = [
            self.runtime.card_folder / "memory" / "openings.json",
            self.runtime.card_folder / "openings.json",
        ]
        if self.static_root is not None:
            candidates.append(self.static_root / "openings.json")
        for path in candidates:
            if path.is_file():
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    continue
                if isinstance(data, list):
                    return data
        return []

    def _read_settings(self) -> dict:
        if not self.static_root:
            return {}
        p = self.static_root / "settings.json"
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _write_settings(self, updates: dict) -> dict:
        current = self._read_settings()
        if isinstance(updates, dict):
            current.update(updates)
        runtime_cfg = current.get("runtime") if isinstance(current, dict) else None
        if self.config_store is not None and isinstance(runtime_cfg, dict):
            return self.config_store.update_settings(updates)
        if self.static_root is not None:
            (self.static_root / "settings.json").write_text(
                json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return current

    def _config_root_for(self, kind: str) -> Path | None:
        if kind == "preset":
            return self.preset_root
        if kind == "graph":
            return self.graph_root
        return None

    def _config_file_for(self, kind: str, config_id: str) -> tuple[Path | None, str | None]:
        if not isinstance(config_id, str) or not CONFIG_ID_RE.fullmatch(config_id):
            return None, "invalid_config_id"
        root = self._config_root_for(kind)
        if root is None:
            return None, "config_root_unavailable"
        root = root.resolve()
        target = (root / f"{config_id}.json").resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return None, "invalid_config_id"
        return target, None

    def _list_json_configs(self, kind: str) -> list[dict[str, Any]]:
        if self.config_store is None:
            return []
        try:
            return self.config_store.list_configs(kind)
        except RuntimeConfigError:
            return []

    def _read_json_config(self, kind: str, config_id: str) -> tuple[dict[str, Any], int]:
        if self.config_store is None:
            return {"ok": False, "error": "config_root_unavailable", "kind": kind, "id": config_id}, 400
        try:
            data = self.config_store.read_config(kind, config_id)
        except RuntimeConfigError as exc:
            return {"ok": False, "kind": kind, "id": config_id, **exc.to_dict()}, 400
        except OSError:
            return {"ok": False, "error": "not_found", "kind": kind, "id": config_id}, 404
        return {
            "ok": True,
            "kind": kind,
            "id": config_id,
            "text": json.dumps(data, ensure_ascii=False, indent=2),
            "data": data,
        }, 200

    def _coerce_config_payload(self, body: dict) -> tuple[Any, str | None]:
        if not isinstance(body, dict):
            return None, "invalid_payload"
        if "text" in body:
            text = body.get("text")
            if not isinstance(text, str) or not text.strip():
                return None, "missing_text"
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                return None, "invalid_json"
        elif "data" in body:
            data = body.get("data")
        else:
            data = body
        if not isinstance(data, (dict, list)):
            return None, "config_must_be_object_or_array"
        return data, None

    def _write_json_config(self, kind: str, config_id: str, body: dict) -> tuple[dict[str, Any], int]:
        data, payload_error = self._coerce_config_payload(body)
        if payload_error:
            return {"ok": False, "error": payload_error, "kind": kind, "id": config_id}, 400
        if self.config_store is None:
            return {"ok": False, "error": "config_root_unavailable", "kind": kind, "id": config_id}, 400
        try:
            self.config_store.write_config(kind, config_id, data)
        except RuntimeConfigError as exc:
            return {"ok": False, "kind": kind, "id": config_id, **exc.to_dict()}, 400
        text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
        return {
            "ok": True,
            "kind": kind,
            "id": config_id,
            "text": text,
            "data": data,
            "saved": True,
        }, 200

    def _write_runtime_selection(self, body: dict) -> tuple[dict[str, Any], int]:
        if not isinstance(body, dict):
            return {"ok": False, "error": "invalid_payload"}, 400
        if self.config_store is None:
            return {"ok": False, "error": "config_root_unavailable"}, 400
        current = self._runtime_selection()
        preset_id = body.get("preset_id", current.get("preset_id") or "default")
        graph_id = body.get("graph_id", current.get("graph_id") or "default")
        if preset_id in (None, ""):
            preset_id = "default"
        if graph_id in (None, ""):
            graph_id = "default"
        try:
            # Graphs are edited in Studio.  Materialize the selected Studio
            # definition before the compatibility RuntimeConfigStore validates
            # the selection, then bind the active card Project to that graph.
            if self.graph_definitions is not None:
                try:
                    studio_graph = self.graph_definitions.get_graph(graph_id)
                except GraphDefinitionError:
                    studio_graph = None
                if studio_graph is not None:
                    self.config_store.write_config("graph", graph_id, self._legacy_graph_from_studio(studio_graph))
            runtime_cfg = self.config_store.write_selection(preset_id, graph_id)
            if self.projects is not None:
                try:
                    self.projects.update_project(self.runtime.project_id, {"graph_id": graph_id})
                except ProjectLibraryError:
                    pass
                self._configure_runtime_studio_graph()
        except RuntimeConfigError as exc:
            return {"ok": False, **exc.to_dict()}, 400
        except (GraphDefinitionError, AgentDefinitionError, ValueError) as exc:
            return {"ok": False, "error": "invalid_runtime_config", "message": str(exc)}, 400
        except OSError as exc:
            return {"ok": False, "error": "not_found", "message": str(exc)}, 404
        return {
            "ok": True,
            "runtime": runtime_cfg,
            "snapshot": self._snapshot_payload(),
        }, 200

    def _read_style_profiles(self) -> list:
        if not self.static_root:
            return []
        profiles_dir = self.static_root / "profiles"
        out = []
        if not profiles_dir.is_dir():
            return out
        for f in sorted(profiles_dir.glob("*.md")):
            name = f.stem
            content = f.read_text(encoding="utf-8")
            title, desc = name, ""
            for line in content.strip().split("\n"):
                if line.startswith("# ") and not line.startswith("## "):
                    title = line[2:].strip()
                elif line.strip() and not line.startswith("#"):
                    desc = line.strip()
                    break
            out.append({"name": name, "title": title, "description": desc})
        return out

    def _delete_style_profile(self, name: str) -> bool:
        if not self.static_root or not name or not CONFIG_ID_RE.fullmatch(name):
            return False
        root = (self.static_root / "profiles").resolve()
        target = (root / f"{name}.md").resolve()
        try:
            target.relative_to(root)
        except ValueError:
            return False
        if target.is_file():
            target.unlink()
            return True
        return False

    def _switch_opening(self, opening_id) -> bool:
        """Switch the active opening and rebuild runtime-derived projections."""
        import handler
        try:
            resolved_id = int(opening_id or 0)
            settings = self._read_settings()
            facts = self.runtime.card_facts()
            ok = bool(
                handler.switch_opening(
                    str(self.runtime.card_folder),
                    resolved_id,
                    user_name=settings.get("charName") or settings.get("user") or "旅行者",
                    character_name=facts.get("name") or "",
                )
            )
        except Exception:
            return False
        if ok:
            try:
                self.runtime.capture_opening_from_chat_log(
                    event_type="session.opening_switched",
                    event_payload={"opening_id": resolved_id},
                )
                self.runtime.resume_projection()
            except Exception:
                return False
        return ok


def _content_type_for(name: str) -> str:
    lower = name.lower()
    if lower.endswith(".html"):
        return "text/html; charset=utf-8"
    if lower.endswith(".js"):
        return "application/javascript; charset=utf-8"
    if lower.endswith(".json"):
        return "application/json; charset=utf-8"
    if lower.endswith(".css"):
        return "text/css; charset=utf-8"
    if lower.endswith(".md"):
        return "text/markdown; charset=utf-8"
    return "application/octet-stream"


def _parse_after(query: dict, headers) -> int:
    raw = None
    if "after" in query and query["after"]:
        raw = query["after"][0]
    if raw is None:
        raw = headers.get("Last-Event-ID") or headers.get("Last-Event-Id")
    if raw is None or raw == "":
        return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0
