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

SSE_HEARTBEAT_SECONDS = 15.0
SSE_POLL_INTERVAL_SECONDS = 0.05
SUBMIT_ACCEPT_WAIT_SECONDS = 2.0


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
    ):
        self.runtime = runtime
        self.service = command_service or SessionCommandService(runtime)
        self.host = host
        self.port = port
        self.heartbeat_seconds = heartbeat_seconds
        self.poll_interval_seconds = poll_interval_seconds
        # Frontend compat: serve static files (index.html/content.js/state.js/...)
        # from styles dir, and adapt the legacy /api/* calls onto the runtime.
        self.static_root = Path(static_root) if static_root else None
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

    # ── HTTP handler factory ───────────────────────────────────────────

    def _make_handler(self):
        server_ref = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):  # noqa: A003 — silence default stderr logs
                return

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
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)

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
                    snap = server_ref.service.snapshot()
                    running = bool(snap.current_task and snap.current_task.status
                                   in ("queued", "running", "projection_pending"))
                    self._send_json(200, {"ok": True, "pending": running})
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
                    self._send_json(200, {"initialized": True})
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
                    key = body.get("idempotency_key") or f"api-submit-{time.time_ns()}"
                    server_ref.submit_async(text, key)
                    # Acknowledge immediately; frontend polls content.js for the result.
                    self._send_json(200, {"ok": True})
                    return

                if path == "/api/reroll":
                    head = server_ref.runtime.active_revision()
                    if head <= 0:
                        self._send_json(400, {"ok": False, "error": "no turns to reroll"})
                        return
                    key = body.get("idempotency_key") or f"api-reroll-{time.time_ns()}"
                    server_ref.reroll_async(head, key)
                    self._send_json(200, {"ok": True})
                    return

                if path == "/api/delete_turns":
                    # Map legacy delete_turns(from_index) onto rollback to the
                    # parent of the deleted range. Tracer-bullet: roll back to
                    # the revision at from_index-1 if resolvable, else reject.
                    from_index = body.get("from_index")
                    head = server_ref.runtime.active_revision()
                    target = None
                    if isinstance(from_index, int) and from_index > 0:
                        lineage = server_ref.runtime.commit_lineage(head) if head else None
                        # Best-effort: rollback to (from_index-1)-th committed ancestor.
                        chain = server_ref.runtime.active_lineage_turns(limit=from_index)
                        if len(chain) >= from_index:
                            target = chain[from_index - 1]["revision"] if from_index - 1 < len(chain) else None
                    if target is None:
                        self._send_json(400, {"ok": False, "error": "cannot resolve rollback target"})
                        return
                    result = server_ref.service.rollback(revision=target, idempotency_key=f"api-delete-{time.time_ns()}")
                    self._send_json(200 if result.ok else 400, result.to_dict())
                    return

                if path == "/api/settings":
                    settings = server_ref._write_settings(body)
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
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(data)
                return True

        return Handler

    # ── Frontend-compat file helpers (read/write styles dir) ────────────

    def _read_openings(self) -> list:
        p = self.static_root / "openings.json" if self.static_root else None
        if p and p.is_file():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                return []
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
        if self.static_root is not None:
            (self.static_root / "settings.json").write_text(
                json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        return current

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
        if not self.static_root or not name:
            return False
        target = self.static_root / "profiles" / f"{name}.md"
        if target.is_file():
            target.unlink()
            return True
        return False

    def _switch_opening(self, opening_id) -> bool:
        """Switch the active opening (index 0). Delegates to handler.switch_opening."""
        import handler  # local import; handler reads/writes the card + projection
        try:
            return bool(handler.switch_opening(str(self.runtime.card_folder), int(opening_id or 0)))
        except Exception:
            return False


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
