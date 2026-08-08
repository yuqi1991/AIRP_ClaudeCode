from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from airp.application import Application  # noqa: E402
from airp.workspace import Workspace  # noqa: E402


def _application(tmp_path: Path) -> Application:
    static_root = tmp_path / "web"
    static_root.mkdir()
    return Application.assemble(
        static_root=static_root,
        workspace=Workspace.from_root(tmp_path / "workspace"),
    )


def test_install_once_materializes_the_editable_recipe_and_activates_unconfigured_projects(tmp_path):
    application = _application(tmp_path)
    application.projects.create_project({"id": "story", "name": "Story"})

    result = application.default_collaboration_suite.install_once()

    assert result["installed"] is True
    assert application.active_graphs.graph_id_for("story") == result["graph_id"]

    profiles = application.provider_profile_store.list_profiles()
    assert profiles == [
        {
            "id": result["provider_profile_id"],
            "name": "DeepSeek",
            "base_url": "https://api.deepseek.com",
            "api_format": "chat_completions",
            "enabled": True,
            "model_ids": ["deepseek-v4-flash"],
            "created_at": profiles[0]["created_at"],
            "updated_at": profiles[0]["updated_at"],
            "revision": 1,
        }
    ]
    assert application.provider_secret_store.get(result["provider_profile_id"]) is None

    regex = application.regex_collections.get_collection(result["regex_collection_id"])
    assert regex["rules"] == [
        {
            "id": "extract-content",
            "name": "提取 content 正文",
            "enabled": True,
            "target": "output",
            "pattern": r"^(?:<content>((?:(?!</?content>)[\s\S])*)</content>|[\s\S]*)$",
            "flags": "",
            "replacement": "$1",
        }
    ]

    writer = application.agent_store.get_agent(result["writer_agent_id"])
    reviewer = application.agent_store.get_agent(result["reviewer_agent_id"])
    assert writer["provider_profile_id"] == reviewer["provider_profile_id"] == result["provider_profile_id"]
    assert writer["model_id"] == reviewer["model_id"] == "deepseek-v4-flash"
    assert writer["generation"] == {"temperature": 0.8, "max_output_tokens": 8000}
    assert reviewer["generation"] == {"temperature": 0.2, "max_output_tokens": 8000}
    assert writer["regex_collection_id"] == result["regex_collection_id"]
    assert reviewer["regex_collection_id"] is None
    assert writer["tool_allowlist"] == reviewer["tool_allowlist"] == [
        "load_worldbook_entry",
        "get_recent_memory",
    ]
    assert "{{card_facts}}" in writer["instruction"]
    assert "恰好使用一对 <content> 与 </content>" in writer["instruction"]
    assert "不要使用 <content> 标签" in reviewer["instruction"]

    graph = application.graph_store.get_graph(result["graph_id"])
    assert graph["mode"] == "handoff"
    assert [(node["node_id"], node["agent_id"]) for node in graph["nodes"]] == [
        ("writer", result["writer_agent_id"]),
        ("reviewer", result["reviewer_agent_id"]),
        ("final-writer", result["writer_agent_id"]),
    ]
    assert graph["output_node_id"] == "final-writer"
    assert graph["loops"] == [
        {
            "id": "draft-review",
            "mode": "fixed",
            "start_node_id": "writer",
            "end_node_id": "reviewer",
            "iterations": 2,
            "exit_handoff_prompt": graph["loops"][0]["exit_handoff_prompt"],
        }
    ]
    assert "{{node.loop_iteration}}" in graph["nodes"][0]["handoff_prompt"]
    assert "第一轮审查意见" in graph["nodes"][1]["handoff_prompt"]
    assert "第二轮也是最后一轮审查意见" in graph["loops"][0]["exit_handoff_prompt"]
