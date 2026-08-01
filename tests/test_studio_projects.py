from __future__ import annotations

import json
import shutil
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from airp.host.rp.session_runtime import SessionTurnRuntime
from airp.server import SessionRuntimeServer


REPO_ROOT = Path(__file__).resolve().parents[1]
STUDIO_SOURCE = REPO_ROOT / "src" / "airp" / "web" / "studio.html"


def _write_card(card: Path) -> None:
    card.mkdir(parents=True, exist_ok=True)
    (card / "memory").mkdir(exist_ok=True)
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    (card / ".card_data.json").write_text(json.dumps({"name": "Test"}), encoding="utf-8")


def _server(tmp_path: Path, *, workspace: bool = False) -> SessionRuntimeServer:
    styles = tmp_path / "styles"
    styles.mkdir(exist_ok=True)
    shutil.copy(REPO_ROOT / "src" / "airp" / "web" / "studio.html", styles / "studio.html")
    card = tmp_path / "card"
    _write_card(card)
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
    )
    return SessionRuntimeServer(
        runtime,
        static_root=styles,
        workspace=tmp_path / "workspace" if workspace else None,
    )


def _json_request(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(
        url,
        data=data,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def _worldbook(server: SessionRuntimeServer) -> dict:
    status, payload = _json_request(
        "POST",
        f"{server.base_url}/v1/studio/worldbooks",
        {
            "name": "Shared setting",
            "entries": [
                {
                    "title": "Harbor",
                    "usage": "Read for harbor scenes",
                    "content": "Old harbor facts",
                }
            ],
        },
    )
    assert status == 201
    return payload["worldbook"]


def test_project_editor_normalizes_card_openings_and_preserves_runtime_inputs(tmp_path: Path):
    with _server(tmp_path) as server:
        book = _worldbook(server)
        status, created = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/projects",
            {
                "id": "keqing-project",
                "card_data": {
                    "spec": "chara_card_v2",
                    "data": {
                        "name": "Keqing",
                        "avatar": "keqing.png",
                        "description": "A careful secretary.",
                        "personality": "Decisive and honest.",
                        "scenario": "The harbor is busy.",
                        "system_prompt": "Stay in character.",
                        "post_history_instructions": "Keep continuity.",
                        "first_mes": "Welcome to the harbor.",
                        "alternate_greetings": ["Welcome to the office."],
                        "example_messages": [["ignored", "data"]],
                        "extensions": {"ignored": True},
                    },
                },
                "variables": {"entrypoint": "state"},
                "assets": [{"id": "portrait", "path": "keqing.png"}],
                "worldbook_ids": [book["id"]],
            },
        )
        assert status == 201
        project = created["project"]
        assert project["id"] == "keqing-project"
        assert project["name"] == "Keqing"
        assert project["avatar"] == "keqing.png"
        assert project["description"] == "A careful secretary."
        assert project["personality"] == "Decisive and honest."
        assert project["scenario"] == "The harbor is busy."
        assert project["card_prompt"] == {
            "system": "Stay in character.",
            "post_history": "Keep continuity.",
        }
        assert [opening["content"] for opening in project["openings"]] == [
            "Welcome to the harbor.",
            "Welcome to the office.",
        ]
        assert project["openings"][0]["is_default"] is True
        assert project["openings"][1]["is_default"] is False
        assert project["variables"] == {"entrypoint": "state"}
        assert project["assets"] == [{"id": "portrait", "path": "keqing.png"}]
        assert "turn_adapter" not in project
        assert project["worldbook_ids"] == [book["id"]]
        assert "first_mes" not in project
        assert "alternate_greetings" not in project
        assert "example_messages" not in project
        assert "extensions" not in project

        status, copied = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/projects/keqing-project/copy",
            {},
        )
        assert status == 201
        assert copied["project"]["id"] != project["id"]
        assert copied["project"]["name"] == "Keqing-copy"
        assert copied["project"]["openings"] == project["openings"]

        status, copied_again = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/projects/keqing-project/copy",
            {},
        )
        assert status == 201
        assert copied_again["project"]["name"] == "Keqing-copy-2"


def test_project_import_endpoint_uses_card_name_and_strips_source_only_fields(tmp_path: Path):
    with _server(tmp_path) as server:
        status, imported = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/projects/import",
            {
                "document": {
                    "spec": "chara_card_v2",
                    "data": {
                        "name": "Imported card",
                        "first_mes": "First scene",
                        "alternate_greetings": ["Another scene"],
                        "example_messages": ["source only"],
                        "extensions": {"source only": True},
                    },
                }
            },
        )
        assert status == 201
        project = imported["project"]
        assert project["name"] == "Imported card"
        assert [opening["content"] for opening in project["openings"]] == ["First scene", "Another scene"]
        assert "example_messages" not in project
        assert "extensions" not in project


def test_project_import_creates_and_binds_embedded_sillytavern_worldbook(tmp_path: Path):
    with _server(tmp_path) as server:
        status, imported = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/projects/import",
            {
                "document": {
                    "spec": "chara_card_v2",
                    "data": {
                        "name": "Card with lore",
                        "character_book": {
                            "name": "Embedded lore",
                            "entries": [
                                {
                                    "id": 7,
                                    "comment": "Harbor",
                                    "content": "The harbor is foggy.",
                                    "enabled": True,
                                    "insertion_order": 3,
                                }
                            ],
                        },
                    },
                }
            },
        )

        assert status == 201
        project = imported["project"]
        assert len(project["worldbook_ids"]) == 1
        status, worldbooks = _json_request("GET", f"{server.base_url}/v1/studio/worldbooks")
        assert status == 200
        imported_book = next(book for book in worldbooks["worldbooks"] if book["id"] == project["worldbook_ids"][0])
        assert imported_book["name"] == "Embedded lore"
        assert imported_book["entries"][0]["title"] == "Harbor"


def test_project_delete_endpoint_removes_imported_project(tmp_path: Path):
    with _server(tmp_path) as server:
        status, imported = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/projects/import",
            {"document": {"spec": "chara_card_v2", "data": {"name": "Disposable card"}}},
        )
        assert status == 201
        project_id = imported["project"]["id"]

        status, deleted = _json_request("DELETE", f"{server.base_url}/v1/studio/projects/{project_id}")
        assert status == 200
        assert deleted["deleted_id"] == project_id
        status, missing = _json_request("GET", f"{server.base_url}/v1/studio/projects/{project_id}")
        assert status == 404
        assert missing["error"] == "project_not_found"


def test_project_save_reads_worldbook_bindings_on_the_next_runtime_context(tmp_path: Path):
    with _server(tmp_path) as server:
        book = _worldbook(server)
        status, saved = _json_request(
            "PUT",
            f"{server.base_url}/v1/studio/projects/card/worldbooks",
            {"name": "Card", "worldbook_ids": [book["id"]]},
        )
        assert status == 200
        assert saved["project"]["worldbook_ids"] == [book["id"]]

        runtime = server.runtime
        runtime.configure_worldbook_library(server.worldbooks.snapshot_for_project, project_id="card")
        first = runtime.compile_opening_context("Begin")
        catalog = next(section for section in first.manifest["sections"] if section["kind"] == "worldbook_catalog")
        assert catalog["content"][0]["title"] == "Harbor"

        entry = book["entries"][0]
        status, updated = _json_request(
            "PUT",
            f"{server.base_url}/v1/studio/worldbooks/{book['id']}",
            {"name": book["name"], "entries": [{**entry, "content": "Fresh harbor facts"}]},
        )
        assert status == 200
        assert updated["worldbook"]["entries"][0]["content"] == "Fresh harbor facts"

        second = runtime.compile_opening_context("Continue")
        catalog = next(section for section in second.manifest["sections"] if section["kind"] == "worldbook_catalog")
        assert catalog["content"][0]["title"] == "Harbor"
        snapshot = server.worldbooks.snapshot_for_project("card")
        assert snapshot["worldbook_reference"] == "## Harbor\nFresh harbor facts"


def test_project_worldbook_bindings_return_stable_project_errors(tmp_path: Path):
    with _server(tmp_path) as server:
        status, _ = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/projects",
            {"id": "normalized-project", "name": "Normalized Project"},
        )
        assert status == 201

        status, rejected = _json_request(
            "PUT",
            f"{server.base_url}/v1/studio/projects/normalized-project/worldbooks",
            {"name": "Normalized Project", "worldbook_ids": ["missing-worldbook"]},
        )

    assert status == 404
    assert rejected["ok"] is False
    assert rejected["error"] == "worldbook_not_found"


def test_projects_view_exposes_normalized_editor_without_import_format_editors(tmp_path: Path):
    with _server(tmp_path) as server:
        with urlopen(f"{server.base_url}/studio", timeout=5) as response:
            page = response.read().decode("utf-8")
        assert response.status == 200
    assert 'id="project-form"' in page
    assert 'id="project-name"' in page
    assert 'id="project-avatar"' in page
    assert 'id="project-description"' in page
    assert 'id="project-personality"' in page
    assert 'id="project-scenario"' in page
    assert 'id="project-card-system"' in page
    assert 'id="project-card-post-history"' in page
    assert 'id="project-openings"' in page
    assert 'id="project-variables"' in page
    assert 'id="project-assets"' in page
    assert 'id="project-graph"' not in page
    assert "/v1/studio/projects" in page
    assert "alternate_greetings" not in page
    assert "example_messages" not in page
    assert "extensions JSON" not in page


def test_runtime_graph_selection_does_not_require_a_legacy_preset(tmp_path: Path):
    with _server(tmp_path, workspace=True) as server:
        status, agent = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/agents",
            {"agent_id": "writer", "name": "Writer", "instruction": "Write"},
        )
        assert status == 201
        status, graph = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/graphs",
            {
                "id": "writing",
                "name": "Writing",
                "nodes": [{"node_id": "writer-node", "agent_id": agent["agent"]["agent_id"]}],
                "output_node_id": "writer-node",
            },
        )
        assert status == 201

        status, before = _json_request(
            "GET",
            f"{server.base_url}/v1/session/runtime/graph",
        )
        assert status == 200
        assert before["selected"]["graph_id"] is None

        status, selected = _json_request(
            "PUT",
            f"{server.base_url}/v1/session/runtime/graph",
            {"graph_id": graph["graph"]["id"]},
        )

        assert status == 200
        assert selected["runtime"]["graph_id"] == "writing"
        status, active = _json_request(
            "GET",
            f"{server.base_url}/v1/session/runtime/graph",
        )
        assert status == 200
        assert active["selected"]["graph_id"] == "writing"
        assert server.active_graphs.graph_id_for(server.runtime.project_id) == "writing"


def test_packaged_studio_owns_regex_collections_and_keeps_project_editor_minimal():
    page = STUDIO_SOURCE.read_text(encoding="utf-8")

    assert 'data-studio-view="regex-collections"' in page
    assert 'id="regex-collections-view"' in page
    assert 'id="regex-collection-form"' in page
    assert 'id="regex-collection-list"' in page
    assert 'id="regex-collection-name"' in page
    assert 'id="regex-rules"' in page
    assert 'id="add-regex-rule"' in page
    assert 'id="test-regex-collection"' in page
    assert 'id="copy-regex-collection"' in page
    assert 'id="delete-regex-collection"' in page
    assert "/v1/studio/regex-collections" in page
    assert "regex_collection_id" in page
    assert 'id="agent-regex-collection"' in page

    assert 'id="load-project"' not in page
    assert 'id="project-graph"' not in page
    assert 'id="project-adapter-id"' not in page
    assert 'id="project-adapter-config"' not in page
    assert 'graph_id: $("project-graph")' not in page
    assert 'turn_adapter:' not in page
    assert '· ${project.graph_id' not in page
