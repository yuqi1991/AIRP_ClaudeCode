from __future__ import annotations

import json
import shutil
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from airp.host.rp.session_runtime import SessionTurnRuntime
from airp.server import SessionRuntimeServer


ROOT = Path(__file__).resolve().parents[1]


def _request(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
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


def _server(tmp_path: Path) -> SessionRuntimeServer:
    styles = tmp_path / "styles"
    styles.mkdir()
    for filename in ("index.html", "game-workspace.css", "game-workspace-contract.js", "game-drawer.js"):
        shutil.copy(ROOT / "src" / "airp" / "web" / filename, styles / filename)
    card = tmp_path / "card"
    (card / "memory").mkdir(parents=True)
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    (card / ".card_data.json").write_text(json.dumps({"name": "Legacy card"}), encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
        bootstrap_legacy_history=False,
    )
    return SessionRuntimeServer(runtime, static_root=styles, workspace=tmp_path / "workspace")


def _create_project(server: SessionRuntimeServer, project_id: str) -> dict:
    status, payload = _request(
        "POST",
        f"{server.base_url}/v1/studio/projects",
        {
            "id": project_id,
            "name": project_id.title(),
            "description": f"Description for {project_id}",
            "openings": [{"id": "opening-1", "label": "Start", "content": f"Welcome to {project_id}", "is_default": True}],
        },
    )
    assert status == 201
    return payload["project"]


def test_game_page_wires_integrated_project_drawer_contract():
    page = (ROOT / "src" / "airp" / "web" / "index.html").read_text(encoding="utf-8")
    drawer = (ROOT / "src" / "airp" / "web" / "game-drawer.js").read_text(encoding="utf-8")
    assert 'id="game-drawer-toggle"' in page
    assert '<script src="game-drawer.js"></script>' in page
    for marker in ("data-game-search", "data-game-import", "data-game-tab", "data-game-save-card", "data-game-save-openings", "data-game-save-worldbooks", "data-game-save-graph"):
        assert marker in drawer
    assert "/v1/session/project/switch" in drawer


def test_worldbook_binding_pane_has_explicit_loading_empty_and_error_states():
    drawer = (ROOT / "src" / "airp" / "web" / "game-drawer.js").read_text(encoding="utf-8")
    for marker in (
        "worldbooksLoading",
        "worldbooksLoaded",
        "worldbooksError",
        "暂无可用世界书",
        "读取世界书失败",
        "data-game-retry-worldbooks",
        "stateRequestId",
    ):
        assert marker in drawer


def test_project_switch_restores_each_project_last_session(tmp_path: Path):
    with _server(tmp_path) as server:
        _create_project(server, "game-a")
        _create_project(server, "game-b")

        status, switched = _request("POST", f"{server.base_url}/v1/session/project/switch", {"project_id": "game-a"})
        assert status == 200
        assert switched["active_project_id"] == "game-a"
        assert switched["runtime"]["project_id"] == "game-a"

        status, created = _request("POST", f"{server.base_url}/api/sessions", {"title": "A save"})
        assert status == 201
        game_a_session = created["active_session_id"]
        assert game_a_session.startswith("session-")

        status, switched = _request("POST", f"{server.base_url}/v1/session/project/switch", {"project_id": "game-b"})
        assert status == 200
        assert switched["active_project_id"] == "game-b"
        assert switched["active_session_id"] == "local"

        status, switched = _request("POST", f"{server.base_url}/v1/session/project/switch", {"project_id": "game-a"})
        assert status == 200
        assert switched["active_session_id"] == game_a_session
        assert switched["project"]["description"] == "Description for game-a"

        status, catalog = _request("GET", f"{server.base_url}/v1/session/project")
        assert status == 200
        assert catalog["active_project_id"] == "game-a"
        assert {item["id"] for item in catalog["projects"]} == {"game-a", "game-b"}
        assert next(item for item in catalog["projects"] if item["id"] == "game-a")["last_session_id"] == game_a_session


def test_deleting_active_project_switches_to_remaining_game(tmp_path: Path):
    with _server(tmp_path) as server:
        _create_project(server, "game-a")
        _create_project(server, "game-b")
        status, switched = _request("POST", f"{server.base_url}/v1/session/project/switch", {"project_id": "game-b"})
        assert status == 200
        assert switched["active_project_id"] == "game-b"

        status, deleted = _request("DELETE", f"{server.base_url}/v1/studio/projects/game-b")
        assert status == 200
        assert deleted["deleted_id"] == "game-b"
        assert deleted["active_project_id"] == "game-a"
        assert {item["id"] for item in deleted["projects"]} == {"game-a"}


def test_project_switch_rejects_active_generation_without_mutating_project(tmp_path: Path):
    with _server(tmp_path) as server:
        _create_project(server, "game-a")
        _create_project(server, "game-b")
        status, switched = _request("POST", f"{server.base_url}/v1/session/project/switch", {"project_id": "game-a"})
        assert status == 200
        assert switched["active_project_id"] == "game-a"

        server.runtime.generation_active = lambda: True
        status, rejected = _request("POST", f"{server.base_url}/v1/session/project/switch", {"project_id": "game-b"})
        assert status == 409
        assert rejected["error"] == "generation_active"
        assert server.runtime.project_id == "game-a"


def test_startup_restores_saved_project_and_falls_back_when_deleted(tmp_path: Path):
    with _server(tmp_path) as server:
        _create_project(server, "game-a")
        _create_project(server, "game-b")
        status, switched = _request("POST", f"{server.base_url}/v1/session/project/switch", {"project_id": "game-b"})
        assert status == 200
        assert switched["active_project_id"] == "game-b"
        server.stop()

    (tmp_path / "workspace" / "projects" / "game-b.json").unlink()

    styles = tmp_path / "styles"
    card = tmp_path / "card"
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
        bootstrap_legacy_history=False,
    )
    with SessionRuntimeServer(runtime, static_root=styles, workspace=tmp_path / "workspace") as restarted:
        assert restarted.runtime.project_id == "game-a"
        status, payload = _request("GET", f"{restarted.base_url}/v1/session/project")
        assert status == 200
        assert payload["active_project_id"] == "game-a"
