"""Disposable real-runtime fixture for the browser Monitor acceptance path."""

from __future__ import annotations

import json
import signal
import shutil
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from airp.application import Application
from airp.host.rp.session_runtime import SessionTurnRuntime
from airp.server import SessionRuntimeServer


REPO_ROOT = Path(__file__).resolve().parents[1]
WEB_ROOT = REPO_ROOT / "src" / "airp" / "web"
PROJECT_ID = "runtime-browser-project"


class DeterministicStreamingProvider:
    """OpenAI-compatible boundary server consumed by the bundled Pi sidecar."""

    def __init__(self) -> None:
        self._node_outputs = 0
        self._failed_second_turn = False
        self._requested_real_tool = False
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, _format, *_args):
                return

            def do_POST(self):  # noqa: N802
                if self.path != "/v1/chat/completions":
                    self.send_error(404)
                    return
                request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                assert request["model"] == "deepseek-v4-flash"
                assert self.headers.get("Authorization") == "Bearer browser-runtime-key"
                request_text = json.dumps(request, ensure_ascii=False)
                current_text = json.dumps([message for message in request.get("messages") or [] if message.get("role") == "user"], ensure_ascii=False)
                second_turn = "Follow the lanterns home." in current_text
                empty_turn = "Commit empty output." in current_text
                whitespace_turn = "Commit whitespace output." in current_text
                prompt_text = json.dumps(request.get("messages") or [], ensure_ascii=False)
                if second_turn and not provider._failed_second_turn:
                    provider._failed_second_turn = True
                    payload = json.dumps({"error": {"message": "temporary fixture failure"}}).encode()
                    self.send_response(503)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if second_turn and "Harbor answer" not in request_text:
                    self.send_error(422, "first committed turn missing from frozen history")
                    return
                if (
                    "Ring the harbor bell." in current_text
                    and not provider._requested_real_tool
                    and not any(message.get("role") == "tool" for message in request.get("messages") or [])
                ):
                    provider._requested_real_tool = True
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-cache")
                    self.end_headers()
                    tool_event = {
                        "choices": [{
                            "delta": {
                                "tool_calls": [{
                                    "index": 0,
                                    "id": "browser-memory-lookup",
                                    "type": "function",
                                    "function": {"name": "get_recent_memory", "arguments": "{}"},
                                }]
                            },
                            "finish_reason": "tool_calls",
                        }]
                    }
                    self.wfile.write(f"data: {json.dumps(tool_event)}\n\ndata: [DONE]\n\n".encode())
                    self.wfile.flush()
                    return
                call = provider._node_outputs % 5
                provider._node_outputs += 1
                outputs = (
                    ("draft loop 1",),
                    ("review loop 1",),
                    ("draft loop 2",),
                    ("review loop 2",),
                    ("<content>", "## Harbor answer\n\n**Bell heard.**", "\nSecond line.</content>"),
                )
                if second_turn:
                    outputs = (*outputs[:4], ("<content>", "## Lantern answer\n\n**Home found.**", "\n\nSecond line.</content>"))
                elif empty_turn:
                    outputs = (*outputs[:4], ("<content></content>",))
                elif whitespace_turn:
                    outputs = (*outputs[:4], ("<content> \n\t </content>",))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                if "Ring the harbor bell." in current_text and call == 0:
                    time.sleep(1.2)
                for chunk in outputs[call]:
                    event = {"choices": [{"delta": {"content": chunk}, "finish_reason": None}]}
                    self.wfile.write(f"data: {json.dumps(event)}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(0.18)
                if call == 4:
                    time.sleep(0.8)
                usage = {
                    "choices": [{"delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
                }
                self.wfile.write(f"data: {json.dumps(usage)}\n\ndata: [DONE]\n\n".encode())
                self.wfile.flush()

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._httpd.server_address[1]}/v1"

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *_args):
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)


def prepare_workspace(root: Path, static_root: Path, provider_url: str) -> None:
    workspace = root / "workspace"
    application = Application.assemble(static_root=static_root, workspace=workspace)
    application.initialize()
    project = application.projects.create_project({"id": PROJECT_ID, "name": "Runtime Browser Project"})
    application.default_collaboration_suite.initialize_project(project["id"])
    profile = application.provider_profile_store.get_profile("default-deepseek")
    application.provider_profile_store.update_profile(
        "default-deepseek",
        {
            "expected_revision": profile["revision"],
            "base_url": provider_url,
            "api_format": "chat_completions",
            "model_ids": ["deepseek-v4-flash"],
        },
    )
    application.provider_secret_store.set("default-deepseek", "browser-runtime-key")


def main() -> None:
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda _signum, _frame: stopped.set())
    signal.signal(signal.SIGINT, lambda _signum, _frame: stopped.set())
    with tempfile.TemporaryDirectory(prefix="airp-runtime-browser-") as folder:
        root = Path(folder)
        card = root / "card"
        (card / "memory").mkdir(parents=True)
        (card / ".initvar.json").write_text("{}", encoding="utf-8")
        (card / "chat_log.json").write_text("[]", encoding="utf-8")
        (card / ".card_data.json").write_text(
            '{"name":"Runtime Browser Project","description":"Real Pi browser fixture."}',
            encoding="utf-8",
        )
        static_root = root / "web"
        shutil.copytree(WEB_ROOT, static_root)
        with DeterministicStreamingProvider() as provider:
            prepare_workspace(root, static_root, provider.base_url)
            runtime = SessionTurnRuntime(
                database_path=root / "runtime.sqlite3",
                card_folder=card,
                projection_root=static_root,
                project_id=PROJECT_ID,
                bootstrap_legacy_history=False,
            )
            with SessionRuntimeServer(
                runtime,
                static_root=static_root,
                workspace=root / "workspace",
            ) as server:
                print(server.base_url, flush=True)
                stopped.wait()


if __name__ == "__main__":
    main()
