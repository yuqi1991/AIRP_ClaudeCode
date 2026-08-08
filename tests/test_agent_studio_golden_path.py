from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request, urlopen

import pytest

try:
    from websockets.sync.client import connect as _connect
except ImportError:  # Optional dependency for the local Chrome acceptance slice.
    _connect = None

REPO_ROOT = Path(__file__).resolve().parents[1]

from airp.host.rp.session_runtime import SessionTurnRuntime
from airp.server import SessionRuntimeServer


def _json_request(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    request = Request(
        url,
        data=None if body is None else json.dumps(body).encode("utf-8"),
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _wait_for(url: str, predicate, *, timeout: float = 5) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        _, payload = _json_request("GET", url)
        if predicate(payload):
            return payload
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {url}")


def _sse_events(base_url: str) -> str:
    host_port = base_url.removeprefix("http://")
    host, port = host_port.rsplit(":", 1)
    with socket.create_connection((host, int(port)), timeout=5) as connection:
        connection.settimeout(0.2)
        connection.sendall(
            b"GET /v1/session/events/stream?after=0 HTTP/1.1\r\n"
            + f"Host: {host_port}\r\nConnection: close\r\n\r\n".encode()
        )
        chunks = []
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                chunk = connection.recv(8192)
            except TimeoutError:
                continue
            if not chunk:
                break
            chunks.append(chunk)
            data = b"".join(chunks).decode("utf-8", errors="replace")
            if "event: graph.node.delta" in data and "event: graph.run.finished" in data:
                return data
    return b"".join(chunks).decode("utf-8", errors="replace")


class _HeadlessBrowser:
    """Small CDP driver for the browser-facing Studio acceptance slice."""

    def __init__(self, page_url: str, profile_path: Path):
        self.page_url = page_url
        self.profile_path = profile_path
        self.process: subprocess.Popen | None = None
        self.socket = None
        self.message_id = 0

    def __enter__(self):
        chrome = shutil.which("google-chrome")
        if chrome is None:
            raise RuntimeError("google-chrome is required for the browser acceptance test")
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            debug_port = listener.getsockname()[1]
        self.process = subprocess.Popen(
            [
                chrome,
                "--headless=new",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--remote-allow-origins=*",
                f"--remote-debugging-port={debug_port}",
                f"--user-data-dir={self.profile_path}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        endpoint = f"http://127.0.0.1:{debug_port}/json/version"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                with urlopen(endpoint, timeout=0.2):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise AssertionError("Chrome DevTools did not start")
        new_target = Request(
            f"http://127.0.0.1:{debug_port}/json/new?{quote(self.page_url, safe='')}",
            method="PUT",
        )
        with urlopen(new_target, timeout=5) as response:
            target = json.loads(response.read().decode("utf-8"))
        assert _connect is not None
        self.socket = _connect(target["webSocketDebuggerUrl"], open_timeout=5)
        self.wait_for("document.readyState === 'complete'")
        return self

    def __exit__(self, *_):
        if self.socket is not None:
            self.socket.close()
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)

    def evaluate(self, expression: str):
        assert self.socket is not None
        self.message_id += 1
        self.socket.send(
            json.dumps(
                {
                    "id": self.message_id,
                    "method": "Runtime.evaluate",
                    "params": {"expression": expression, "awaitPromise": True, "returnByValue": True},
                }
            )
        )
        while True:
            payload = json.loads(self.socket.recv(timeout=10))
            if payload.get("id") != self.message_id:
                continue
            if "error" in payload:
                raise AssertionError(payload["error"])
            result = payload["result"]["result"]
            if "exceptionDetails" in payload["result"]:
                raise AssertionError(payload["result"]["exceptionDetails"])
            return result.get("value")

    def click(self, selector: str) -> None:
        assert self.evaluate(
            f"(() => {{ const node = document.querySelector({json.dumps(selector)}); if (!node) return false; node.click(); return true; }})()"
        ), selector

    def wait_for(self, expression: str, *, timeout: float = 10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.evaluate(f"(async () => Boolean(await ({expression})))()"):
                return
            time.sleep(0.05)
        raise AssertionError(f"timed out waiting for browser condition: {expression}")


class _GoldenPathProvider:
    def __init__(self):
        outer = self
        self.calls = 0
        self.requests: list[dict] = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *args):
                return

            def do_GET(self):  # noqa: N802
                assert self.headers.get("Authorization") == "Bearer studio-secret"
                payload = json.dumps({"data": [{"id": "studio-model"}]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_POST(self):  # noqa: N802
                assert self.headers.get("Authorization") == "Bearer studio-secret"
                outer.calls += 1
                outer.requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
                if outer.calls == 1:
                    payload = json.dumps({"error": {"message": "temporary provider failure"}}).encode("utf-8")
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                payload = (
                    'data: {"choices":[{"delta":{"content":"<content>Studio output</content><summary>done</summary>"},"finish_reason":null}]}\n\n'
                    'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":4,"total_tokens":7}}\n\n'
                    "data: [DONE]\n\n"
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def test_studio_to_game_golden_path_drives_saved_configuration_live_trace_and_retry(tmp_path: Path):
    styles = tmp_path / "styles"
    styles.mkdir()
    shutil.copytree(
        REPO_ROOT / "src" / "airp" / "web",
        styles,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("studio", "__pycache__"),
    )
    card = tmp_path / "golden-path"
    (card / "memory").mkdir(parents=True)
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    (card / ".card_data.json").write_text('{"name":"Golden"}', encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
        bootstrap_legacy_history=False,
    )

    with _GoldenPathProvider() as provider, SessionRuntimeServer(runtime, static_root=styles) as server:
        status, profile = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/providers",
            {
                "id": "studio-provider",
                "name": "Studio Provider",
                "base_url": provider.base_url,
                "api_format": "chat_completions",
                "api_key": "studio-secret",
            },
        )
        assert status == 201
        assert profile["profile"]["key_configured"] is True
        assert "studio-secret" not in json.dumps(profile)

        status, agent = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/agents",
            {
                "agent_id": "studio-writer",
                "name": "Studio Writer",
                "instruction": "Write a concise scene.",
                "provider_profile_id": profile["profile"]["id"],
                "model_id": "studio-model",
            },
        )
        assert status == 201

        status, graph = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/graphs",
            {
                "id": "studio-graph",
                "name": "Studio Graph",
                "nodes": [{"node_id": "writer", "agent_id": agent["agent"]["id"]}],
                "output_node_id": "writer",
            },
        )
        assert status == 201

        status, worldbook = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/worldbooks",
            {
                "name": "Studio Lore",
                "entries": [{"title": "Harbor", "usage": "Harbor scenes", "content": "Foggy docks."}],
            },
        )
        assert status == 201

        status, project = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/projects",
            {
                "id": "golden-path",
                "name": "Golden Project",
                "description": "A saved Studio project.",
                "worldbook_ids": [worldbook["worldbook"]["id"]],
                "openings": [{"id": "default", "label": "Default", "content": "Hello", "is_default": True}],
            },
        )
        assert status == 201
        assert "graph_id" not in project["project"]

        status, selected = _json_request(
            "PUT",
            f"{server.base_url}/v1/session/runtime/graph",
            {"graph_id": graph["graph"]["id"]},
        )
        assert status == 200
        assert selected["runtime"]["graph_id"] == "studio-graph"

        status, accepted = _json_request(
            "POST",
            f"{server.base_url}/v1/session/commands/submit",
            {"text": "Enter the harbor.", "idempotency_key": "golden-failure"},
        )
        assert status in {200, 202}
        assert accepted["task_id"]
        failed = _wait_for(
            f"{server.base_url}/v1/studio/graph-runs",
            lambda payload: (payload.get("most_recent") or {}).get("status") == "failed",
        )["most_recent"]
        failed_node = failed["nodes"][0]
        assert failed_node["state"] == "failed"

        status, detail = _json_request(
            "GET", f"{server.base_url}/v1/studio/node-runs/{failed_node['node_run_id']}"
        )
        assert status == 200
        assert detail["node_run"]["effective_config"]["model_id"] == "studio-model"
        assert detail["node_run"]["prompt_provenance"]
        assert detail["node_run"]["error"]["code"] == "provider_unavailable"

        status, retry = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/graph-runs/{failed['graph_run_id']}/retry",
            {"idempotency_key": "golden-retry"},
        )
        assert status in {200, 202}
        assert retry["task_id"], json.dumps(retry, sort_keys=True)
        succeeded = _wait_for(
            f"{server.base_url}/v1/studio/graph-runs",
            lambda payload: (payload.get("most_recent") or {}).get("status") == "succeeded",
        )["most_recent"]
        assert succeeded["retry_of"] == failed["graph_run_id"]
        node_run_id = succeeded["nodes"][0]["node_run_id"]
        assert "graph.node.delta" in _sse_events(server.base_url)

        # Graph execution finishes before the host has necessarily completed
        # its distinct commit/projection phase. A debug replay must not be
        # blamed for that in-flight task's one legitimate revision.
        deadline = time.monotonic() + 5
        while runtime.task(retry["task_id"]).status != "succeeded":
            if time.monotonic() >= deadline:
                raise AssertionError("retry task did not complete its commit/projection")
            time.sleep(0.02)

        revision_before = runtime.active_revision()
        status, replay = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/node-runs/{node_run_id}/replay",
            {"idempotency_key": "golden-replay"},
        )
        assert status == 200
        assert replay["debug_replay"]["state"] == "succeeded"
        assert runtime.active_revision() == revision_before
        assert all("studio-secret" not in json.dumps(request) for request in provider.requests)

        status, context = _json_request("GET", f"{server.base_url}/v1/studio/project-context")
        assert status == 200
        assert context["project_id"] == "golden-path"

        with urlopen(f"{server.base_url}/studio", timeout=5) as response:
            studio_page = response.read().decode("utf-8")
        with urlopen(f"{server.base_url}/", timeout=5) as response:
            game_page = response.read().decode("utf-8")
        assert 'data-airp-app="game-workspace"' in studio_page
        assert 'id="studio-drawer-host"' in studio_page
        assert 'id="studio-agents-toggle"' in studio_page
        assert 'id="studio-graph-form"' in studio_page
        assert 'id="studio-worldbooks-toggle"' in studio_page
        assert "retryGraphRun" in game_page
        assert "runDebugReplay" in game_page

@pytest.mark.skipif(
    shutil.which("google-chrome") is None or _connect is None,
    reason="requires google-chrome and websockets",
)
def test_browser_game_exposes_integrated_drawers_and_trace_fallback_detail(tmp_path: Path):
    """The game page keeps its integrated drawers and Trace nodes inspectable before a run id exists."""
    styles = tmp_path / "styles"
    styles.mkdir()
    shutil.copytree(
        REPO_ROOT / "src" / "airp" / "web",
        styles,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("studio", "__pycache__"),
    )
    card = tmp_path / "browser-trace-fallback"
    (card / "memory").mkdir(parents=True)
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    (card / ".card_data.json").write_text('{"name":"Browser Trace Fallback"}', encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
        bootstrap_legacy_history=False,
    )

    with SessionRuntimeServer(runtime, static_root=styles) as server:
        with _HeadlessBrowser(f"{server.base_url}/", tmp_path / "chrome-profile") as browser:
            browser.wait_for('document.querySelector("#game-drawer-toggle")')
            assert browser.evaluate("!document.querySelector('#studio-link[href=\\\"/studio\\\"]')")
            assert browser.evaluate(
                """(() => {
                    const originalFetch = window.fetch.bind(window);
                    window.fetch = (input, init) => {
                        if (String(input).includes('/v1/session/agent-traces')) {
                            return Promise.resolve(new Response(JSON.stringify({agent_trace: {
                                label: 'Planner · director', node_id: 'director', state: 'succeeded',
                                prompt: [{role: 'system', content: 'TRACE_PROMPT'}],
                                output: 'TRACE_OUTPUT', detail_source: 'legacy agent trace'
                            }}), {status: 200, headers: {'Content-Type': 'application/json'}}));
                        }
                        return originalFetch(input, init);
                    };
                    agentTrace = {graphMode: true, events: [], nodes: {
                        fallback: {sequence: 1, order: 0, nodeRunId: null, state: 'idle',
                            label: 'Planner', nodeId: 'director', taskId: 'task-1',
                            meta: '节点未运行', streamedOutput: ''}
                    }};
                    renderAgentTrace();
                    const node = document.querySelector('#agent-trace .trace-node');
                    if (!node) return false;
                    node.click();
                    return true;
                })()"""
            )
            browser.wait_for("!document.getElementById('node-detail-modal').hidden")
            browser.wait_for("document.getElementById('node-detail-body').textContent.includes('TRACE_PROMPT')")
            browser.wait_for("document.getElementById('node-detail-body').textContent.includes('TRACE_OUTPUT')")
            assert "Planner" in browser.evaluate("document.getElementById('node-detail-body').textContent")

@pytest.mark.skipif(
    shutil.which("google-chrome") is None or _connect is None,
    reason="requires google-chrome and websockets",
)
def test_browser_studio_alias_exposes_integrated_workspace_drawers(tmp_path: Path):
    styles = tmp_path / "styles"
    styles.mkdir()
    shutil.copytree(
        REPO_ROOT / "src" / "airp" / "web",
        styles,
        dirs_exist_ok=True,
        ignore=shutil.ignore_patterns("studio", "__pycache__"),
    )
    card = tmp_path / "browser-studio-alias"
    (card / "memory").mkdir(parents=True)
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    (card / ".card_data.json").write_text('{"name":"Browser Studio Alias"}', encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
        bootstrap_legacy_history=False,
    )

    with SessionRuntimeServer(runtime, static_root=styles) as server:
        with _HeadlessBrowser(f"{server.base_url}/studio", tmp_path / "chrome-profile") as browser:
            browser.wait_for("document.body && document.body.dataset.airpApp === 'game-workspace'")
            browser.wait_for("document.getElementById('studio-drawer-host')")
            browser.click("#studio-model-toggle")
            browser.wait_for("!document.getElementById('studio-drawer-host').hidden")
            browser.wait_for("document.getElementById('studio-provider-form')")
            browser.click("#studio-agents-toggle")
            browser.wait_for("document.getElementById('studio-agent-form')")
            browser.click("#studio-worldbooks-toggle")
            browser.wait_for("document.getElementById('worldbook-drawer-mount')")
            browser.click("#studio-regex-toggle")
            browser.wait_for("document.getElementById('regex-drawer-panel')")
            assert browser.evaluate("location.pathname === '/studio'")
