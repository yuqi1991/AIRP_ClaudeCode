"""Thin stdlib HTTP + SSE server for the AIRP Graph runtime.

The canonical AIRP HTTP transport. It never writes ``input.txt`` / ``.pending``
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

``GET /v1/session/project``
    Active Project, Project catalog and its last active Session.

``POST /v1/session/project/switch``
    Body ``{project_id}``. Refuses to switch while generation is active, then
    restores the target Project's last active Session and returns its snapshot.

SSE quiet / lifetime policy
---------------------------
The stream stays open until the client disconnects (or the server is shut
down). There is no automatic close after a terminal task — browsers reconnect
via ``Last-Event-ID`` / ``?after=`` and the server is at-least-once for the gap
(clients dedupe by sequence).

Thread model
------------
* One :class:`~airp.host.rp.session_runtime.SessionTurnRuntime` per server (single-session
  tracer bullet, ADR-0004).
* ``submit`` work runs on a daemon worker thread so the request thread and any
  concurrent SSE handlers remain responsive.
* Event reads (``events_after``) do not take the runtime generation lock; the
  director can emit preview events while SSE polls them.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from airp.workspace import Workspace
from airp.application import Application
from airp.engine.agent_definitions import AgentDefinitionError
from airp.host.rp.commands import SessionCommandService
from airp.engine.graph_definitions import GraphDefinitionError
from airp.engine.graph_runtime import ExecutionPlanCompiler, GraphRuntime
from airp.engine.node_runner import ProviderNodeRunner
from airp.engine.provider_profiles import ProviderConnectionError
from airp.engine.project_library import ProjectLibraryError
from airp.engine.regex_collections import RegexCollectionError
from airp.host.rp.session_runtime import RuntimeEvent, SessionTurnRuntime
from airp.host.rp.session_manager import SessionManager, SessionManagerError
from airp.host.rp.project_runtime import ProjectRuntimeStore
from airp.engine.studio_library import ProviderProfileError
from airp.compat.studio_migration import bootstrap_legacy_runtime_library
from airp.engine.worldbook_library import WorldbookLibraryError

SSE_HEARTBEAT_SECONDS = 15.0
SSE_POLL_INTERVAL_SECONDS = 0.05
SUBMIT_ACCEPT_WAIT_SECONDS = 2.0
RUNNING_TASK_STATUSES = frozenset({"queued", "leased", "running", "projection_pending"})
STUDIO_PROVIDER_PATHS = ("/v1/studio/providers",)
STUDIO_AGENT_PATHS = ("/v1/studio/agents", "/v1/studio/agent-definitions")
STUDIO_GRAPH_PATHS = ("/v1/studio/graphs",)
STUDIO_WORLDBOOK_PATHS = ("/v1/studio/worldbooks",)
STUDIO_REGEX_COLLECTION_PATHS = ("/v1/studio/regex-collections",)
STUDIO_PROJECT_PATHS = ("/v1/studio/projects",)
STUDIO_PROJECT_CONTEXT_PATHS = ("/v1/studio/project-context",)
PROJECT_RUNTIME_PATHS = ("/v1/session/project", "/v1/session/projects")
PROJECT_SWITCH_PATHS = ("/v1/session/project/switch", "/v1/session/projects/switch")
STUDIO_GRAPH_RUN_PATHS = ("/v1/studio/graph-runs",)
STUDIO_NODE_RUN_PATHS = ("/v1/studio/node-runs",)
STUDIO_DEBUG_REPLAY_PATHS = ("/v1/studio/debug-replays",)
AGENT_TRACE_DETAIL_PATH = "/v1/session/agent-traces"
_CANONICAL_COMPAT_PATHS = {
    "/v1/session/sessions": "/api/sessions",
    "/v1/session/sessions/switch": "/api/sessions/switch",
    "/v1/session/sessions/rename": "/api/sessions/rename",
    "/v1/session/openings": "/api/openings",
    "/v1/session/openings/switch": "/api/switch_opening",
    "/v1/session/turns/delete": "/api/delete_turns",
    "/v1/session/status": "/api/session_status",
    "/v1/session/runtime/graph": "/api/runtime/graph",
}


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
        graph_root: str | None = None,
        session_manager: SessionManager | None = None,
        workspace: Workspace | str | Path | None = None,
    ):
        self.runtime = runtime
        self.service = command_service or SessionCommandService(runtime)
        self.session_manager = session_manager
        self.host = host
        self.port = port
        self.heartbeat_seconds = heartbeat_seconds
        self.poll_interval_seconds = poll_interval_seconds
        if workspace is None:
            self.workspace = None
        elif isinstance(workspace, Workspace):
            self.workspace = workspace.ensure()
        else:
            self.workspace = Workspace.from_root(workspace).ensure()
        # Frontend compat: serve static files (index.html/content.js/state.js/...)
        # from styles dir, and adapt the legacy /api/* calls onto the runtime.
        self.static_root = Path(static_root).resolve() if static_root else None
        repo_root = Path(__file__).resolve().parents[2]
        self.graph_root = Path(graph_root).resolve() if graph_root else ((self.static_root / "graphs").resolve() if self.static_root else (repo_root / "graphs").resolve())
        self.application = Application.assemble(
            static_root=self.static_root,
            workspace=self.workspace,
            graph_root=self.graph_root,
        )
        self.provider_profile_store = self.application.provider_profile_store
        self.provider_secret_store = self.application.provider_secret_store
        self.provider_profiles = self.application.provider_profiles
        self.agent_store = self.application.agent_store
        self.agent_definitions = self.application.agent_definitions
        # Short alias for callers that use the Studio object name directly.
        self.agents = self.agent_definitions
        self.graph_store = self.application.graph_store
        self.graph_definitions = self.application.graph_definitions
        self.graphs = self.graph_definitions
        self.worldbooks = self.application.worldbooks
        self.regex_collections = self.application.regex_collections
        self.projects = self.application.projects
        self.active_graphs = self.application.active_graphs
        self._bootstrap_legacy_studio_library()
        self.project_runtimes = None
        if self.projects is not None:
            self.project_runtimes = ProjectRuntimeStore(
                self.projects,
                workspace=self.workspace,
                static_root=self.static_root,
                projection_root=self.static_root or self.runtime.projection.projection_root,
                initial_runtime=self.runtime,
                initial_sessions=self.session_manager,
            )
            preferred = self.project_runtimes.restore_project_id(self.runtime.project_id)
            if preferred is not None:
                context = self.project_runtimes.get(preferred)
                self.runtime = context.runtime
                self.session_manager = context.sessions
                self.project_runtimes.remember(preferred)
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

    @staticmethod
    def _canonical_compat_path(path: str) -> str:
        """Route transitional session paths through one internal adapter.

        ``/v1`` is the maintained transport seam. The old handlers remain
        behind this map for card-local compatibility, so the frontend and new
        clients do not need to know the legacy URL vocabulary.
        """
        mapped = _CANONICAL_COMPAT_PATHS.get(path)
        if mapped is not None:
            return mapped
        prefix = "/v1/session/sessions/"
        if path.startswith(prefix):
            return "/api/sessions/" + path[len(prefix):]
        return path

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

    def _project_runtime_payload(self, project: dict[str, Any] | None = None) -> dict[str, Any]:
        """Return the small cross-Project state consumed by the game drawer."""
        active_id = self.runtime.project_id
        projects = []
        if self.projects is not None:
            try:
                for item in self.projects.list_projects():
                    entry = dict(item)
                    entry["active"] = item.get("id") == active_id
                    if self.project_runtimes is not None:
                        try:
                            entry.update(self.project_runtimes.session_payload(item["id"]))
                        except Exception:
                            entry["last_session_id"] = None
                    projects.append(entry)
            except ProjectLibraryError:
                projects = []
        current = project
        if current is None and self.projects is not None:
            try:
                current = self.projects.get_project(active_id)
            except ProjectLibraryError:
                current = None
        session = self._sessions_payload()
        return {
            "ok": True,
            "active_project_id": active_id if current is not None else None,
            "runtime": {
                "project_id": active_id if current is not None else None,
                "session_id": session.get("active_session_id"),
            },
            "project": current,
            "projects": projects,
            "active_session_id": session.get("active_session_id"),
            "sessions": session.get("sessions", []),
            "snapshot": self._snapshot_payload(),
        }

    def _switch_active_project(self, project_id: Any) -> tuple[dict[str, Any], int]:
        if self.projects is None or self.project_runtimes is None:
            return {"ok": False, "error": "project_runtime_unavailable"}, 501
        if not isinstance(project_id, str) or not project_id.strip():
            return {"ok": False, "error": "invalid_project", "message": "project_id is required"}, 400
        project_id = project_id.strip()
        try:
            project = self.projects.get_project(project_id)
        except ProjectLibraryError as exc:
            return self._studio_project_error(exc)
        if self.runtime.generation_active():
            return {
                "ok": False,
                "error": "generation_active",
                "message": "生成进行中，完成或取消后才能切换游戏",
            }, 409
        try:
            context = self.project_runtimes.get(project_id)
            self.runtime = context.runtime
            self.session_manager = context.sessions
            self.project_runtimes.remember(project_id)
            self._studio_graph_configured = False
            self._configure_runtime_worldbooks()
            self._configure_runtime_studio_graph()
            self.runtime.resume_projection()
            self.service = SessionCommandService(self.runtime)
            with self._submit_lock:
                self._submit_results.clear()
            return self._project_runtime_payload(project), 200
        except (ProjectLibraryError, SessionManagerError, ValueError) as exc:
            if isinstance(exc, ProjectLibraryError):
                return self._studio_project_error(exc)
            return {"ok": False, "error": "project_switch_failed", "message": str(exc)}, 409

    def _bind_project_runtime_if_active(self, project: dict[str, Any] | None) -> None:
        """Attach the per-Project Session manager after a new Project appears."""
        if not project or self.project_runtimes is None or project.get("id") != self.runtime.project_id:
            return
        context = self.project_runtimes.adopt(project["id"], self.runtime, self.session_manager)
        if context.runtime is self.runtime and context.sessions is self.session_manager:
            return
        self.runtime = context.runtime
        self.session_manager = context.sessions
        self.service = SessionCommandService(self.runtime)

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
        if self.active_graphs is None:
            return {"graph_id": None}
        try:
            return {"graph_id": self.active_graphs.graph_id_for(self.runtime.project_id)}
        except ValueError:
            return {"graph_id": None}

    def _active_graph_payload(self) -> dict[str, Any]:
        return {
            "ok": True,
            "selected": self._runtime_selection(),
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
        return []

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
            selection = self._runtime_selection().get("graph_id")
            graphs = self.graph_definitions.list_graphs() if self.graph_definitions else []
            if not selection:
                legacy_selection = (self._read_legacy_settings().get("runtime") or {}).get("graph_id")
                graph_ids = {item["id"] for item in graphs}
                selected = legacy_selection if legacy_selection in graph_ids else None
                if selected is None and len(graphs) == 1:
                    selected = graphs[0]["id"]
                if selected:
                    self.active_graphs.select(self.runtime.project_id, selected)
        except Exception:
            # A malformed optional legacy file must not prevent the game from
            # starting; Studio will still expose any valid existing objects.
            return

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
            return {"ok": True, **result}, 201
        except AgentDefinitionError as exc:
            return self._studio_agent_error(exc)

    def _studio_agent_update(self, agent_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if self.agent_definitions is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            result = self.agent_definitions.update_agent(agent_id, body)
            return {"ok": True, **result}, 200
        except AgentDefinitionError as exc:
            return self._studio_agent_error(exc)

    def _studio_agent_copy(self, agent_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if self.agent_definitions is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            result = self.agent_definitions.copy_agent(agent_id, body)
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

    # ── Studio Regex Collections ─────────────────────────────────────

    @staticmethod
    def _studio_regex_collection_error(exc: RegexCollectionError) -> tuple[dict[str, Any], int]:
        return exc.to_dict(), exc.status

    def _studio_regex_collection_list(self) -> tuple[dict[str, Any], int]:
        if self.regex_collections is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            return {"ok": True, "collections": self.regex_collections.list_collections()}, 200
        except RegexCollectionError as exc:
            return self._studio_regex_collection_error(exc)

    def _studio_regex_collection_get(self, collection_id: str) -> tuple[dict[str, Any], int]:
        if self.regex_collections is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            return {"ok": True, "collection": self.regex_collections.get_collection(collection_id)}, 200
        except RegexCollectionError as exc:
            return self._studio_regex_collection_error(exc)

    def _studio_regex_collection_create(self, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if self.regex_collections is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            return {"ok": True, "collection": self.regex_collections.create_collection(body)}, 201
        except RegexCollectionError as exc:
            return self._studio_regex_collection_error(exc)

    def _studio_regex_collection_update(
        self, collection_id: str, body: dict[str, Any]
    ) -> tuple[dict[str, Any], int]:
        if self.regex_collections is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            return {
                "ok": True,
                "collection": self.regex_collections.update_collection(collection_id, body),
            }, 200
        except RegexCollectionError as exc:
            return self._studio_regex_collection_error(exc)

    def _studio_regex_collection_copy(
        self, collection_id: str, body: dict[str, Any]
    ) -> tuple[dict[str, Any], int]:
        if self.regex_collections is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            return {
                "ok": True,
                "collection": self.regex_collections.copy_collection(collection_id, body),
            }, 201
        except RegexCollectionError as exc:
            return self._studio_regex_collection_error(exc)

    def _studio_regex_collection_test(
        self, collection_id: str, body: dict[str, Any]
    ) -> tuple[dict[str, Any], int]:
        if self.regex_collections is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            candidate = body.get("collection") if isinstance(body, dict) else None
            text = body.get("text", "") if isinstance(body, dict) else ""
            target = body.get("target", "output") if isinstance(body, dict) else "output"
            return {
                "ok": True,
                "result": self.regex_collections.test_collection(
                    collection_id,
                    candidate,
                    text=text,
                    target=target,
                ),
            }, 200
        except RegexCollectionError as exc:
            return self._studio_regex_collection_error(exc)

    def _studio_regex_collection_delete(self, collection_id: str) -> tuple[dict[str, Any], int]:
        if self.regex_collections is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            self.regex_collections.delete_collection(collection_id)
            return {"ok": True, "deleted_id": collection_id}, 200
        except RegexCollectionError as exc:
            return self._studio_regex_collection_error(exc)

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
            return {"ok": True, **result}, 201
        except GraphDefinitionError as exc:
            return self._studio_graph_error(exc)

    def _studio_graph_update(self, graph_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if self.graph_definitions is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            result = self.graph_definitions.update_graph(graph_id, body)
            return {"ok": True, **result}, 200
        except GraphDefinitionError as exc:
            return self._studio_graph_error(exc)

    def _studio_graph_copy(self, graph_id: str, body: dict[str, Any]) -> tuple[dict[str, Any], int]:
        if self.graph_definitions is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            result = self.graph_definitions.copy_graph(graph_id, body)
            return {"ok": True, **result}, 201
        except GraphDefinitionError as exc:
            return self._studio_graph_error(exc)

    def _studio_graph_delete(self, graph_id: str) -> tuple[dict[str, Any], int]:
        if self.graph_definitions is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            self.graph_definitions.delete_graph(graph_id)
            affected_projects = self.active_graphs.clear_graph(graph_id) if self.active_graphs is not None else ()
            if self.runtime.project_id in affected_projects:
                self._configure_runtime_studio_graph()
            return {"ok": True, "deleted_id": graph_id}, 200
        except GraphDefinitionError as exc:
            return self._studio_graph_error(exc)

    # ── Studio Worldbooks and Project bindings ────────────────────────

    def _configure_runtime_worldbooks(self) -> None:
        if self.worldbooks is not None:
            self.runtime.configure_worldbook_library(self.worldbooks.snapshot_for_project)

    def _configure_runtime_studio_graph(self) -> None:
        """Attach the active Studio Graph without mutating Project content."""
        if not all((self.agent_store, self.graphs, self.projects, self.worldbooks, self.provider_profiles)):
            return
        try:
            project = self.projects.get_project(self.runtime.project_id)
        except ProjectLibraryError:
            # Legacy/browser launches may not have an explicit ``local``
            # Project yet. If Studio has exactly one Project, make it the
            # active runtime Project so the Runtime Graph selector remains
            # useful without reviving the removed Preset requirement.
            try:
                projects = self.projects.list_projects()
            except Exception:
                projects = []
            if len(projects) != 1:
                if self._studio_graph_configured:
                    self.runtime.configure_execution_graph(None, None)
                    self._studio_graph_configured = False
                return
            project = projects[0]
            self.runtime.project_id = project["id"]
        selected_graph = self._runtime_selection().get("graph_id")
        if not selected_graph:
            if self._studio_graph_configured:
                self.runtime.configure_execution_graph(None, None)
                self._studio_graph_configured = False
            return
        compiler = ExecutionPlanCompiler(
            agent_store=self.agent_store,
            graph_store=self.graphs,
            project_store=self.projects,
            worldbook_store=self.worldbooks,
            regex_collection_store=self.regex_collections,
        )
        runner = ProviderNodeRunner(self._provider_for_studio_graph_node)
        self.runtime.configure_execution_graph(
            compiler,
            GraphRuntime(runner),
            project_id=project["id"],
            graph_id=selected_graph,
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
            if self.project_runtimes is not None and isinstance(project, dict):
                self.project_runtimes.refresh(project)
            self._bind_project_runtime_if_active(project)
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
            if key == "project" and isinstance(project, dict) and self.project_runtimes is not None:
                self.project_runtimes.refresh(project)
            self._configure_runtime_studio_graph()
            self._bind_project_runtime_if_active(project if isinstance(project, dict) else None)
            self._configure_runtime_studio_graph()
            return {"ok": True, key: project}, status
        except ProjectLibraryError as exc:
            return self._studio_project_error(exc)

    def _studio_project_import(self, body):
        if self.projects is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        try:
            project = self.projects.import_card(body)
            if self.project_runtimes is not None:
                self.project_runtimes.refresh(project)
            self._bind_project_runtime_if_active(project)
            return {"ok": True, "project": project}, 201
        except ProjectLibraryError as exc:
            return self._studio_project_error(exc)

    def _studio_project_delete(self, project_id: str) -> tuple[dict[str, Any], int]:
        if self.projects is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        if project_id == self.runtime.project_id and self.runtime.generation_active():
            return {
                "ok": False,
                "error": "generation_active",
                "message": "生成进行中，完成或取消后才能删除当前游戏",
            }, 409
        try:
            deleting_active = project_id == self.runtime.project_id
            fallback = next(
                (item for item in self.projects.list_projects() if item.get("id") != project_id),
                None,
            )
            if deleting_active and fallback is not None and self.project_runtimes is not None:
                switched, status = self._switch_active_project(fallback["id"])
                if status != 200:
                    return switched, status
            self.projects.delete_project(project_id)
            payload = self._project_runtime_payload()
            return {"ok": True, "deleted_id": project_id, **payload}, 200
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
                path = server_ref._canonical_compat_path(parsed.path.rstrip("/") or "/")
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

                # ── Runtime Studio Regex Collections ──────────────────
                if path in STUDIO_REGEX_COLLECTION_PATHS:
                    payload, status = server_ref._studio_regex_collection_list()
                    self._send_json(status, payload)
                    return
                for prefix in STUDIO_REGEX_COLLECTION_PATHS:
                    if path.startswith(prefix + "/"):
                        suffix = path[len(prefix) + 1:]
                        parts = suffix.split("/")
                        if len(parts) == 1:
                            payload, status = server_ref._studio_regex_collection_get(parts[0])
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
                if path in PROJECT_RUNTIME_PATHS:
                    self._send_json(200, server_ref._project_runtime_payload())
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
                if path == "/api/session_status":
                    self._send_json(200, server_ref._session_status_payload())
                    return
                if path == "/api/session_snapshot":
                    self._send_json(200, server_ref._snapshot_payload())
                    return
                if path == "/api/sessions":
                    self._send_json(200, server_ref._sessions_payload())
                    return
                if path == "/api/runtime/graph":
                    self._send_json(200, server_ref._active_graph_payload())
                    return

                # ── Static files from styles dir (index.html/content.js/...) ──
                if server_ref.static_root is not None and self._maybe_serve_static(path):
                    return

                self._send_json(404, {"ok": False, "error": "not_found"})

            def do_POST(self):  # noqa: N802
                parsed = urlparse(self.path)
                path = server_ref._canonical_compat_path(parsed.path.rstrip("/") or "/")
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

                # ── Runtime Studio Regex Collections ──────────────────
                if path in STUDIO_REGEX_COLLECTION_PATHS:
                    payload, status = server_ref._studio_regex_collection_create(body)
                    self._send_json(status, payload)
                    return
                for prefix in STUDIO_REGEX_COLLECTION_PATHS:
                    if path.startswith(prefix + "/"):
                        suffix = path[len(prefix) + 1:]
                        parts = suffix.split("/")
                        if len(parts) == 2 and parts[1] in {"copy", "duplicate"}:
                            payload, status = server_ref._studio_regex_collection_copy(parts[0], body)
                            self._send_json(status, payload)
                            return
                        if len(parts) == 2 and parts[1] == "test":
                            payload, status = server_ref._studio_regex_collection_test(parts[0], body)
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
                if path in PROJECT_SWITCH_PATHS:
                    payload, status = server_ref._switch_active_project(body.get("project_id"))
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

                if path == "/api/switch_opening":
                    ok = server_ref._switch_opening(body.get("opening_id"))
                    self._send_json(
                        200 if ok else 400,
                        {"ok": ok, "snapshot": server_ref._snapshot_payload()},
                    )
                    return

                self._send_json(404, {"ok": False, "error": "not_found"})

            def do_DELETE(self):  # noqa: N802
                parsed = urlparse(self.path)
                path = server_ref._canonical_compat_path(parsed.path.rstrip("/") or "/")
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
                for prefix in STUDIO_REGEX_COLLECTION_PATHS:
                    if path.startswith(prefix + "/"):
                        collection_id = path[len(prefix) + 1:]
                        if "/" not in collection_id:
                            payload, status = server_ref._studio_regex_collection_delete(collection_id)
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
                for prefix in STUDIO_PROJECT_PATHS:
                    if path.startswith(prefix + "/"):
                        project_id = path[len(prefix) + 1:]
                        if "/" not in project_id:
                            payload, status = server_ref._studio_project_delete(project_id)
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
                path = server_ref._canonical_compat_path(parsed.path.rstrip("/") or "/")
                body = self._read_json()

                if path in PROJECT_SWITCH_PATHS:
                    payload, status = server_ref._switch_active_project(body.get("project_id"))
                    self._send_json(status, payload)
                    return

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

                for prefix in STUDIO_REGEX_COLLECTION_PATHS:
                    if path.startswith(prefix + "/"):
                        collection_id = path[len(prefix) + 1:]
                        if "/" not in collection_id:
                            payload, status = server_ref._studio_regex_collection_update(collection_id, body)
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

                if path == "/api/runtime/graph":
                    payload, code = server_ref._write_runtime_selection(body)
                    self._send_json(code, payload)
                    return

                self._send_json(404, {"ok": False, "error": "not_found"})

            def do_PATCH(self):  # noqa: N802
                parsed = urlparse(self.path)
                path = server_ref._canonical_compat_path(parsed.path.rstrip("/") or "/")
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

    def _read_legacy_settings(self) -> dict:
        """Read old static settings only while importing existing installs."""
        if not self.static_root:
            return {}
        p = self.static_root / "settings.json"
        if p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return {}
        return {}

    def _write_runtime_selection(self, body: dict) -> tuple[dict[str, Any], int]:
        if not isinstance(body, dict):
            return {"ok": False, "error": "invalid_payload"}, 400
        current = self._runtime_selection()
        graph_id = body.get("graph_id", current.get("graph_id"))
        if self.graph_definitions is None or self.active_graphs is None:
            return {"ok": False, "error": "studio_library_unavailable"}, 501
        if graph_id in (None, ""):
            self.active_graphs.clear(self.runtime.project_id)
            self._configure_runtime_studio_graph()
            return {
                "ok": True,
                "runtime": {"graph_id": None},
                "snapshot": self._snapshot_payload(),
            }, 200
        if not isinstance(graph_id, str) or not graph_id.strip():
            return {"ok": False, "error": "graph_id_required"}, 400
        try:
            self.graph_definitions.get_graph(graph_id)
            self.active_graphs.select(self.runtime.project_id, graph_id)
        except GraphDefinitionError as exc:
            return {"ok": False, **exc.to_dict()}, exc.status
        except ValueError as exc:
            return {"ok": False, "error": "invalid_graph_selection", "message": str(exc)}, 400
        self._configure_runtime_studio_graph()
        return {
            "ok": True,
            "runtime": {"graph_id": graph_id},
            "snapshot": self._snapshot_payload(),
        }, 200

    def _switch_opening(self, opening_id) -> bool:
        """Switch the active opening and rebuild runtime-derived projections."""
        from airp import handler
        try:
            resolved_id = int(opening_id or 0)
            settings = self.runtime.session_settings
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
