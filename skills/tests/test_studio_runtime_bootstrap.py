from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.runtime import SessionTurnRuntime
from airp.engine.active_graph import ActiveGraphSelectionStore
from airp.workspace import Workspace
from runtime_server import SessionRuntimeServer


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


def test_game_page_only_exposes_graph_activation_selector():
    page = (Path(__file__).resolve().parents[1] / "styles" / "index.html").read_text(encoding="utf-8")
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
