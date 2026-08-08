from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from airp.host.rp.session_runtime import SessionTurnRuntime
from airp.engine.active_graph import ActiveGraphSelectionError, ActiveGraphSelectionStore
from airp.engine.agent_definitions import AgentDefinitionStore
from airp.engine.graph_definitions import GraphDefinitionStore
from airp.engine.project_library import ProjectLibrary
from airp.engine.secret_store import LocalSecretStore
from airp.engine.studio_library import ProviderProfileStore
from airp.engine.worldbook_library import WorldbookLibrary
from airp.compat.studio_migration import migrate_legacy_studio_directory
from airp.workspace import Workspace
from airp.server import SessionRuntimeServer


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


def _write_card(card: Path) -> None:
    card.mkdir(parents=True)
    (card / ".card_data.json").write_text(json.dumps({"name": "Imported card"}), encoding="utf-8")
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")


def test_server_bootstraps_legacy_runtime_into_studio_and_binds_project(tmp_path: Path):
    styles = tmp_path / "styles"
    (styles / "graphs").mkdir(parents=True)
    (styles / "presets").mkdir()
    (styles / "settings.json").write_text(
        json.dumps(
            {
                "style": "北棱特调",
                "nsfw": "直白",
                "person": "第二人称",
                "wordCount": 1200,
                "antiImpersonation": True,
                "bgNpc": True,
                "runtime": {"preset_id": "default", "graph_id": "default"},
                "provider": {"base_url": "https://api.deepseek.com"},
            }
        ),
        encoding="utf-8",
    )
    (styles / "presets" / "default.json").write_text(
        json.dumps({"id": "default", "entries": [{"id": "system", "role": "system", "content": "Stay in role."}]}),
        encoding="utf-8",
    )
    (styles / "graphs" / "default.json").write_text(
        json.dumps(
            {
                "id": "default",
                "nodes": [
                    {
                        "id": "director",
                        "role": "narrative_director",
                        "provider": "deepseek",
                        "model": "deepseek-chat",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    card = tmp_path / "card"
    _write_card(card)
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
    )

    with SessionRuntimeServer(runtime, static_root=styles, workspace=tmp_path / "workspace") as server:
        assert [item["id"] for item in server.provider_profiles.list_profiles()] == ["legacy-deepseek"]
        agents = server.agent_definitions.list_agents()
        assert [item["agent_id"] for item in agents] == ["legacy-default-director"]
        assert "北棱特调" in agents[0]["instruction"]
        assert "第二人称" in agents[0]["instruction"]
        assert "不代替玩家发言" in agents[0]["instruction"]
        assert [item["id"] for item in server.graph_definitions.list_graphs()] == ["default"]
        status, active = _json_request(
            "GET",
            f"{server.base_url}/v1/session/runtime/graph",
        )
        assert status == 200
        assert active["selected"]["graph_id"] == "default"
        assert active["graphs"][0]["id"] == "default"
        assert server.active_graphs.graph_id_for(runtime.project_id) == "default"

        graph_root = Workspace.from_root(tmp_path / "workspace").graphs_root
        graph_root.chmod(0o500)
        try:
            status, failed_delete = _json_request(
                "DELETE",
                f"{server.base_url}/v1/studio/graphs/default",
            )
        finally:
            graph_root.chmod(0o700)
        assert status == 500
        assert failed_delete["error"] == "graph_delete_failed"
        assert server.graph_definitions.get_graph("default")["id"] == "default"
        assert server.active_graphs.graph_id_for(runtime.project_id) == "default"

        class UnreadableSelections:
            def graph_id_for(self, project_id):
                raise ActiveGraphSelectionError(
                    "active_graph_selection_unreadable",
                    "cannot read active Graph selections",
                )

            def clear_graph(self, graph_id):
                raise ActiveGraphSelectionError(
                    "active_graph_selection_unreadable",
                    "cannot read active Graph selections",
                )

        server.active_graphs = UnreadableSelections()
        server._configure_runtime_studio_graph()
        assert runtime.execution_graph_id == "default"

        status, damaged = _json_request(
            "GET",
            f"{server.base_url}/api/runtime/graph",
        )
        assert status == 500
        assert damaged["error"] == "active_graph_selection_unreadable"

        status, damaged_write = _json_request(
            "PUT",
            f"{server.base_url}/api/runtime/graph",
            {"graph_id": "default"},
        )
        assert status == 500
        assert damaged_write["error"] == "active_graph_selection_unreadable"

        status, damaged_delete = _json_request(
            "DELETE",
            f"{server.base_url}/v1/studio/graphs/default",
        )
        assert status == 500
        assert damaged_delete["error"] == "active_graph_selection_unreadable"
        assert server.graph_definitions.get_graph("default")["id"] == "default"


def test_migrate_legacy_studio_directory_is_idempotent_and_preserves_worldbooks(tmp_path: Path):
    source = tmp_path / "old-styles"
    studio = source / "studio"
    for name in ("providers", "agents", "graphs", "worldbooks", "projects"):
        (studio / name).mkdir(parents=True)
    (studio / "providers" / "provider-a.json").write_text(
        json.dumps({"id": "provider-a", "name": "Provider A", "base_url": "https://example.test"}),
        encoding="utf-8",
    )
    (studio / "agents" / "agent-a.json").write_text(
        json.dumps({"id": "agent-a", "name": "Agent A", "instruction": "debug"}),
        encoding="utf-8",
    )
    (studio / "graphs" / "graph-a.json").write_text(
        json.dumps(
            {
                "id": "graph-a",
                "name": "Graph A",
                "nodes": [{"id": "node-a", "agent_id": "agent-a"}],
            }
        ),
        encoding="utf-8",
    )
    (studio / "worldbooks" / "book-a.json").write_text(
        json.dumps(
            {
                "id": "book-a",
                "name": "Book A",
                "entries": [{"id": "entry-a", "title": "Facts", "content": "text"}],
            }
        ),
        encoding="utf-8",
    )
    (studio / "projects" / "project-a.json").write_text(
        json.dumps(
            {
                "id": "project-a",
                "name": "Project A",
                "description": "card",
                "worldbook_ids": ["book-a"],
            }
        ),
        encoding="utf-8",
    )
    (studio / "secrets.json").write_text(json.dumps({"provider-a": "secret-value"}), encoding="utf-8")

    workspace = Workspace.from_root(tmp_path / "workspace").ensure()
    provider_store = ProviderProfileStore(source, workspace=workspace)
    agent_store = AgentDefinitionStore(source, workspace=workspace)
    graph_store = GraphDefinitionStore(source, agent_store=agent_store, workspace=workspace)
    worldbook_store = WorldbookLibrary(source, workspace=workspace)
    project_store = ProjectLibrary(source, worldbooks=worldbook_store, workspace=workspace)
    secret_store = LocalSecretStore(workspace.secrets_path)
    stores = {
        "source_root": source,
        "provider_store": provider_store,
        "secret_store": secret_store,
        "agent_store": agent_store,
        "graph_store": graph_store,
        "worldbook_store": worldbook_store,
        "project_store": project_store,
    }

    first = migrate_legacy_studio_directory(**stores)
    second = migrate_legacy_studio_directory(**stores)

    assert first["providers"] == 1
    assert first["agents"] == 1
    assert first["graphs"] == 1
    assert first["worldbooks"] == 1
    assert first["projects"] == 1
    assert first["secrets"] == 1
    assert not first["errors"]
    assert second["providers"] == 0
    assert second["worldbooks"] == 0
    assert worldbook_store.get_worldbook("book-a")["entries"][0]["content"] == "text"
    assert project_store.get_project("project-a")["worldbook_ids"] == ["book-a"]
    assert secret_store.get("provider-a") == "secret-value"


def test_game_page_only_exposes_graph_activation_selector():
    page = (Path(__file__).resolve().parents[1] / "src" / "airp" / "web" / "index.html").read_text(encoding="utf-8")
    assert 'id="runtime-graph-select"' in page
    assert 'id="set-style"' not in page
    assert 'id="set-nsfw"' not in page
    assert 'id="set-person"' not in page
    assert 'id="set-wordcount"' not in page
    assert 'id="runtime-preset-editor"' not in page
    assert 'id="runtime-graph-editor"' not in page
    assert 'id="provider-card"' not in page
    assert "loadProviderConfig()" not in page


def test_active_graph_selection_persists_per_project_without_project_content(tmp_path: Path):
    workspace = Workspace.from_root(tmp_path / "workspace")
    store = ActiveGraphSelectionStore(workspace)

    assert store.graph_id_for("story-a") is None
    store.select("story-a", "graph-a")
    store.select("story-b", "graph-b")

    restarted = ActiveGraphSelectionStore(workspace)
    assert restarted.graph_id_for("story-a") == "graph-a"
    assert restarted.graph_id_for("story-b") == "graph-b"

    restarted.clear("story-a")
    assert restarted.graph_id_for("story-a") is None
    assert restarted.graph_id_for("story-b") == "graph-b"
    assert not (workspace.projects_root / "story-a.json").exists()

    restarted.select("story-c", "graph-b")
    assert restarted.clear_graph("graph-b") == ("story-b", "story-c")
    assert restarted.graph_id_for("story-b") is None
    assert restarted.graph_id_for("story-c") is None
