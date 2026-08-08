from __future__ import annotations

import copy
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from airp.application import Application  # noqa: E402
from airp.workspace import Workspace  # noqa: E402


def test_install_once_suffixes_colliding_objects_and_rewrites_references(tmp_path):
    static_root = tmp_path / "web"
    static_root.mkdir()
    application = Application.assemble(
        static_root=static_root,
        workspace=Workspace.from_root(tmp_path / "workspace"),
    )
    originals = {
        "provider": application.provider_profile_store.create_profile(
            {
                "id": "default-deepseek",
                "name": "User Provider",
                "base_url": "https://example.com/v1",
                "api_format": "chat_completions",
            }
        ),
        "regex": application.regex_collections.create_collection(
            {"id": "default-content", "name": "User Regex", "rules": []}
        ),
        "writer": application.agent_store.create_agent(
            {"id": "default-writer", "name": "User Writer", "instruction": "mine"}
        ),
        "reviewer": application.agent_store.create_agent(
            {"id": "default-reviewer", "name": "User Reviewer", "instruction": "mine"}
        ),
    }
    originals["graph"] = application.graph_store.create_graph(
        {
            "id": "default-two-round-review",
            "name": "User Graph",
            "nodes": [{"node_id": "only", "agent_id": "default-writer"}],
        }
    )
    before = copy.deepcopy(originals)

    result = application.default_collaboration_suite.install_once()

    assert result == {
        "installed": True,
        "provider_profile_id": "default-deepseek-copy",
        "regex_collection_id": "default-content-copy",
        "writer_agent_id": "default-writer-copy",
        "reviewer_agent_id": "default-reviewer-copy",
        "graph_id": "default-two-round-review-copy",
    }
    graph = application.graph_store.get_graph(result["graph_id"])
    assert [node["agent_id"] for node in graph["nodes"]] == [
        result["writer_agent_id"],
        result["reviewer_agent_id"],
        result["writer_agent_id"],
    ]
    assert application.provider_profile_store.get_profile("default-deepseek") == before["provider"]
    assert application.regex_collections.get_collection("default-content") == before["regex"]
    assert application.agent_store.get_agent("default-writer") == before["writer"]
    assert application.agent_store.get_agent("default-reviewer") == before["reviewer"]
    assert application.graph_store.get_graph("default-two-round-review") == before["graph"]
