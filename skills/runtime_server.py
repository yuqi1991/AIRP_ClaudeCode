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
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from engine.commands import SessionCommandService
from engine.runtime import RuntimeEvent, SessionTurnRuntime
from engine.runtime_config import CONFIG_ID_RE, RuntimeConfigError, RuntimeConfigStore

SSE_HEARTBEAT_SECONDS = 15.0
SSE_POLL_INTERVAL_SECONDS = 0.05
SUBMIT_ACCEPT_WAIT_SECONDS = 2.0
RUNNING_TASK_STATUSES = frozenset({"queued", "running", "projection_pending"})


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
    ):
        self.runtime = runtime
        self.service = command_service or SessionCommandService(runtime)
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
        self.config_store = (
            RuntimeConfigStore(
                self.static_root,
                preset_root=self.preset_root,
                graph_root=self.graph_root,
            )
            if self.static_root
            else None
        )
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
            worker_fn=lambda: self.service.submit(text, idempotency_key),
            thread_name=f"submit-{idempotency_key[:24]}",
        )

    def reroll_async(self, revision: int, idempotency_key: str) -> dict:
        """Background ``service.reroll`` so SSE can stream the new branch tip."""
        return self._command_async(
            idempotency_key,
            worker_fn=lambda: self.service.reroll(revision=revision, idempotency_key=idempotency_key),
            thread_name=f"reroll-{idempotency_key[:24]}",
        )

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
        return {
            "initialized": True,
            "pending": snapshot["pending"],
            "status": snapshot["status"],
            "task_id": (snapshot.get("current_task") or {}).get("task_id"),
            "snapshot": snapshot,
        }

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
            "graphs": self._list_json_configs("graph"),
        }

    # ── HTTP handler factory ───────────────────────────────────────────

    def _make_handler(self):
        server_ref = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # noqa: A003 — silence default stderr logs
                return

            def _send_cors_headers(self) -> None:
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, OPTIONS")
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

                if path == "/v1/session/snapshot":
                    snap = server_ref.service.snapshot()
                    self._send_json(200, snap.to_dict())
                    return

                if path == "/v1/session/events/stream":
                    after = _parse_after(query, self.headers)
                    self._stream_sse(after)
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
                    char_name = (body.get("charName") or "").strip()
                    submitted_text = f"【{char_name}】{text}" if char_name else text
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
                    self._send_json(200 if ok else 400, {"ok": ok})
                    return

                if path == "/api/style-profiles/delete":
                    name = (body.get("name") or "").strip()
                    ok = server_ref._delete_style_profile(name)
                    self._send_json(200 if ok else 404, {"ok": ok})
                    return

                self._send_json(404, {"ok": False, "error": "not_found"})

            def do_PUT(self):  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path.rstrip("/") or "/"
                body = self._read_json()

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
                rel = "index.html" if path in ("", "/") else path.lstrip("/")
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
            runtime_cfg = self.config_store.write_selection(preset_id, graph_id)
        except RuntimeConfigError as exc:
            return {"ok": False, **exc.to_dict()}, 400
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
            ok = bool(handler.switch_opening(str(self.runtime.card_folder), int(opening_id or 0)))
        except Exception:
            return False
        if ok:
            try:
                self.runtime.capture_opening_from_chat_log()
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
