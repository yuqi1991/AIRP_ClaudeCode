from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from engine.runtime import FakeNarrativeExecutor, SessionTurnRuntime
from runtime_server import SessionRuntimeServer


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
        executor=FakeNarrativeExecutor(content="ok"),
    )

    with SessionRuntimeServer(runtime, static_root=styles) as server:
        assert [item["id"] for item in server.provider_profiles.list_profiles()] == ["legacy-deepseek"]
        agents = server.agent_definitions.list_agents()
        assert [item["agent_id"] for item in agents] == ["legacy-default-director"]
        assert "北棱特调" in agents[0]["instruction"]
        assert "第二人称" in agents[0]["instruction"]
        assert "不代替玩家发言" in agents[0]["instruction"]
        assert [item["id"] for item in server.graph_definitions.list_graphs()] == ["default"]
        project = server.projects.get_project(runtime.project_id)
        assert project["graph_id"] == "default"
        assert server._runtime_config_payload()["graphs"][0]["id"] == "default"


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
