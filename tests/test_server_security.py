from __future__ import annotations

import json
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from airp.host.rp.session_runtime import SessionTurnRuntime
from airp.server import MAX_REQUEST_BODY_BYTES, SessionRuntimeServer


def _runtime(tmp_path: Path) -> tuple[SessionTurnRuntime, Path]:
    card = tmp_path / "card"
    (card / "memory").mkdir(parents=True)
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / ".card_data.json").write_text(json.dumps({"name": "Security"}), encoding="utf-8")
    styles = tmp_path / "styles"
    styles.mkdir()
    (styles / "index.html").write_text("<html>AIRP</html>", encoding="utf-8")
    return SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
        bootstrap_legacy_history=False,
    ), styles


def _request(method: str, url: str, *, headers: dict[str, str] | None = None, data: bytes | None = None):
    request = Request(url, method=method, headers=headers or {}, data=data)
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, response.headers, response.read()
    except HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def test_exposed_server_requires_capability_and_sets_same_origin_cookie(tmp_path: Path):
    runtime, styles = _runtime(tmp_path)
    with SessionRuntimeServer(runtime, host="0.0.0.0", static_root=styles) as server:
        url = f"http://127.0.0.1:{server.port}"
        status, headers, _ = _request("GET", f"{url}/")
        assert status == 200
        assert headers.get("Set-Cookie", "").startswith("airp_capability=")

        status, _, body = _request("GET", f"{url}/v1/studio/providers")
        assert status == 401
        assert json.loads(body)["error"] == "capability_required"

        status, _, body = _request(
            "GET",
            f"{url}/v1/studio/providers",
            headers={"Authorization": f"Bearer {server.capability}"},
        )
        assert status == 200
        assert json.loads(body)["ok"] is True

        status, _, body = _request(
            "DELETE",
            f"{url}/v1/studio/providers/missing",
        )
        assert status == 401
        assert json.loads(body)["error"] == "capability_required"


def test_disallowed_origin_and_oversized_body_are_rejected(tmp_path: Path):
    runtime, styles = _runtime(tmp_path)
    with SessionRuntimeServer(runtime, static_root=styles) as server:
        url = server.base_url
        status, _, body = _request("GET", f"{url}/", headers={"Origin": "https://evil.example"})
        assert status == 403
        assert json.loads(body)["error"] == "origin_not_allowed"

        body = b"{" + b"x" * MAX_REQUEST_BODY_BYTES + b"}"
        status, _, response_body = _request(
            "POST",
            f"{url}/v1/studio/providers",
            headers={"Content-Type": "application/json"},
            data=body,
        )
        assert status == 413
        assert json.loads(response_body)["error"] == "request_body_too_large"
