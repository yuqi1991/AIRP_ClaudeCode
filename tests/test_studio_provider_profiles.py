from __future__ import annotations

import json
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]

from airp.host.rp.session_runtime import SessionTurnRuntime  # noqa: E402
from airp.server import SessionRuntimeServer  # noqa: E402


def _write_card(card: Path) -> None:
    card.mkdir(exist_ok=True)
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    (card / ".card_data.json").write_text(json.dumps({"name": "Test"}), encoding="utf-8")


def _server(tmp_path: Path) -> SessionRuntimeServer:
    styles = tmp_path / "styles"
    styles.mkdir(exist_ok=True)
    card = tmp_path / "card"
    _write_card(card)
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
    )
    return SessionRuntimeServer(runtime, static_root=styles)


def _json_request(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_provider_profile_crud_persists_safe_library_fields(tmp_path):
    with _server(tmp_path) as server:
        status, created = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/providers",
            {
                "name": "Local Responses",
                "base_url": "https://llm.example/v1/",
                "api_format": "responses",
                "enabled": True,
                "model_ids": ["writer-1", "writer-1", ""],
            },
        )
        assert status == 201
        profile = created["profile"]
        assert profile["id"]
        assert profile["name"] == "Local Responses"
        assert profile["base_url"] == "https://llm.example/v1"
        assert profile["api_format"] == "responses"
        assert profile["enabled"] is True
        assert profile["model_ids"] == ["writer-1"]
        profile_id = profile["id"]

        status, listed = _json_request("GET", f"{server.base_url}/v1/studio/providers")
        assert status == 200
        assert [item["id"] for item in listed["profiles"]] == [profile_id]

        status, fetched = _json_request(
            "GET", f"{server.base_url}/v1/studio/providers/{profile_id}"
        )
        assert status == 200
        assert fetched["profile"] == profile

        status, updated = _json_request(
            "PUT",
            f"{server.base_url}/v1/studio/providers/{profile_id}",
            {"name": "Chat Provider", "protocol": "chat_completions", "model_ids": ["chat-1"]},
        )
        assert status == 200
        assert updated["profile"]["name"] == "Chat Provider"
        assert updated["profile"]["api_format"] == "chat_completions"

        status, disabled = _json_request(
            "POST", f"{server.base_url}/v1/studio/providers/{profile_id}/disable"
        )
        assert status == 200
        assert disabled["profile"]["enabled"] is False
        status, enabled = _json_request(
            "POST", f"{server.base_url}/v1/studio/providers/{profile_id}/enable"
        )
        assert status == 200
        assert enabled["profile"]["enabled"] is True

    with _server(tmp_path) as restarted:
        status, listed = _json_request(f"GET", f"{restarted.base_url}/v1/studio/providers")
        assert status == 200
        assert listed["profiles"][0]["name"] == "Chat Provider"


def test_delete_provider_profile_reports_agent_reference_and_preserves_file(tmp_path):
    server = _server(tmp_path)
    with server:
        status, created = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/providers",
            {"name": "Shared", "base_url": "https://shared.example", "protocol": "chat_completions"},
        )
        assert status == 201
        profile_id = created["profile"]["id"]
        agents = tmp_path / "styles" / "studio" / "agents"
        agents.mkdir(parents=True)
        (agents / "writer.json").write_text(
            json.dumps({"id": "writer", "name": "Writer", "provider_profile_id": profile_id}),
            encoding="utf-8",
        )

        status, blocked = _json_request(
            "DELETE", f"{server.base_url}/v1/studio/providers/{profile_id}"
        )
        assert status == 409
        assert blocked["error"] == "provider_profile_in_use"
        assert blocked["references"] == [{"type": "agent", "id": "writer", "name": "Writer"}]

        status, fetched = _json_request(
            "GET", f"{server.base_url}/v1/studio/providers/{profile_id}"
        )
        assert status == 200
        assert fetched["profile"]["id"] == profile_id

        (agents / "writer.json").unlink()
        status, deleted = _json_request(
            "DELETE", f"{server.base_url}/v1/studio/providers/{profile_id}"
        )
        assert status == 200
        assert deleted["deleted_id"] == profile_id


def test_provider_profile_rejects_invalid_protocol(tmp_path):
    with _server(tmp_path) as server:
        status, invalid = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/providers",
            {"name": "Bad", "base_url": "https://bad.example", "api_format": "legacy"},
        )
        assert status == 400
        assert invalid["error"] == "invalid_provider_profile"

def test_studio_providers_view_is_served_from_runtime_and_uses_profile_seam(tmp_path):
    styles = tmp_path / "styles"
    styles.mkdir()
    shutil.copy(REPO_ROOT / "src" / "airp" / "web" / "index.html", styles / "index.html")
    card = tmp_path / "card"
    _write_card(card)
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
    )
    with SessionRuntimeServer(runtime, static_root=styles) as server:
        with urlopen(f"{server.base_url}/studio", timeout=5) as response:
            page = response.read().decode("utf-8")
            assert response.status == 200
    assert 'data-airp-app="game-workspace"' in page
    assert 'id="studio-drawer-host"' in page
    assert 'id="studio-model-toggle"' in page
    assert 'id="studio-provider-form"' in page
    assert 'id="studio-provider-api-key"' in page
    assert 'id="studio-provider-refresh"' in page
    assert 'src="studio-model-drawer.js"' in page


class _ModelsUpstream:
    def __init__(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_GET(self):  # noqa: N802
                if self.headers.get("Authorization") != "Bearer secret-one":
                    self.send_response(401)
                    self.end_headers()
                    return
                payload = json.dumps(
                    {"data": [{"id": "deepseek-chat"}, {"id": "deepseek-reasoner"}]}
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        outer.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        outer.thread = threading.Thread(target=outer.httpd.serve_forever, daemon=True)

    @property
    def base_url(self):
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/v1"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def test_profile_secret_discovery_test_refresh_and_delete_are_http_observable(tmp_path):
    with _ModelsUpstream() as upstream, _server(tmp_path) as server:
        status, created = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/providers",
            {
                "name": "DeepSeek",
                "base_url": upstream.base_url,
                "api_format": "chat_completions",
                "api_key": "secret-one",
            },
        )
        assert status == 201
        profile = created["profile"]
        profile_id = profile["id"]
        assert profile["key_configured"] is True
        assert profile["model_ids"] == ["deepseek-chat", "deepseek-reasoner"]
        assert created["model_discovery"] == {"ok": True, "model_ids": profile["model_ids"]}
        assert "secret-one" not in json.dumps(created)

        status, tested = _json_request(
            "POST", f"{server.base_url}/v1/studio/providers/{profile_id}/test"
        )
        assert status == 200
        assert tested == {"ok": True, "model_ids": ["deepseek-chat", "deepseek-reasoner"]}

        status, refreshed = _json_request(
            "POST", f"{server.base_url}/v1/studio/providers/{profile_id}/models/refresh"
        )
        assert status == 200
        assert refreshed["profile"]["model_ids"] == ["deepseek-chat", "deepseek-reasoner"]

        status, removed = _json_request(
            "DELETE", f"{server.base_url}/v1/studio/providers/{profile_id}/secret"
        )
        assert status == 200
        assert removed["profile"]["key_configured"] is False

    secrets = tmp_path / "styles" / "studio" / "secrets.json"
    assert "secret-one" not in secrets.read_text(encoding="utf-8")


def test_discovery_failure_does_not_block_save_or_manual_model_id(tmp_path):
    with _server(tmp_path) as server:
        status, created = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/providers",
            {
                "name": "Offline",
                "base_url": "http://127.0.0.1:1/v1",
                "api_format": "responses",
                "api_key": "offline-secret",
                "model_ids": ["manual-model"],
            },
        )
        assert status == 201
        assert created["profile"]["model_ids"] == ["manual-model"]
        assert created["profile"]["key_configured"] is True
        assert created["model_discovery"]["ok"] is False
        assert created["model_discovery"]["error"]["category"] == "provider_unavailable"
        assert "offline-secret" not in json.dumps(created)
