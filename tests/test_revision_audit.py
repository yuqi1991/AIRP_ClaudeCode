from __future__ import annotations

import json
from pathlib import Path

import pytest

from airp.engine.agent_definitions import AgentDefinitionError, AgentDefinitionStore
from airp.engine.graph_definitions import GraphDefinitionError, GraphDefinitionStore
from airp.engine.project_library import ProjectLibrary, ProjectLibraryError
from airp.engine.regex_collections import RegexCollectionError, RegexCollectionLibrary
from airp.engine.studio_library import ProviderProfileError, ProviderProfileStore
from airp.engine.worldbook_library import WorldbookLibrary, WorldbookLibraryError


def _audit_records(root: Path) -> list[dict]:
    return [json.loads(line) for line in (root / ".audit.jsonl").read_text(encoding="utf-8").splitlines()]


def test_project_revision_conflict_and_redacted_audit(tmp_path: Path):
    library = ProjectLibrary(tmp_path / "static", project_root=tmp_path / "projects")
    created = library.create_project({"id": "project-a", "name": "A"})
    assert created["revision"] == 1
    updated = library.update_project("project-a", {"name": "A2", "expected_revision": 1})
    assert updated["revision"] == 2
    with pytest.raises(ProjectLibraryError) as caught:
        library.update_project("project-a", {"name": "stale", "expected_revision": 1})
    assert caught.value.code == "revision_conflict"
    assert caught.value.status == 409
    assert caught.value.to_dict()["current_revision"] == 2
    records = _audit_records(tmp_path / "projects")
    assert [(record["parent_revision"], record["revision"]) for record in records] == [(0, 1), (1, 2)]
    assert all("name" not in record and "after" not in record for record in records)


def test_worldbook_revision_and_project_binding_revision(tmp_path: Path):
    worldbooks = WorldbookLibrary(tmp_path / "static", library_root=tmp_path / "worldbooks", workspace=None)
    created, _ = worldbooks.create_worldbook({"id": "book-a", "name": "A", "entries": []})
    assert created["revision"] == 1
    updated, _ = worldbooks.update_worldbook(
        "book-a", {"name": "A2", "entries": [], "expected_revision": 1}
    )
    assert updated["revision"] == 2
    with pytest.raises(WorldbookLibraryError) as caught:
        worldbooks.update_worldbook("book-a", {"name": "stale", "entries": [], "expected_revision": 1})
    assert caught.value.code == "revision_conflict"

    binding = worldbooks.set_project_bindings("project-a", {"name": "A", "worldbook_ids": ["book-a"]})
    assert binding["revision"] == 1
    with pytest.raises(WorldbookLibraryError):
        worldbooks.set_project_bindings(
            "project-a", {"name": "stale", "worldbook_ids": [], "expected_revision": 0}
        )
    assert _audit_records(tmp_path / "worldbooks")[-1]["object_type"] == "worldbook"
    assert _audit_records(tmp_path / "static" / "studio" / "projects")[-1]["object_type"] == "project_worldbooks"


def test_provider_agent_graph_and_regex_revisions(tmp_path: Path):
    providers = ProviderProfileStore(tmp_path / "static", library_root=tmp_path / "providers")
    provider = providers.create_profile({"id": "provider-a", "name": "Provider", "base_url": "https://example.test"})
    assert provider["revision"] == 1
    provider = providers.update_profile("provider-a", {"name": "Provider 2", "expected_revision": 1})
    assert provider["revision"] == 2
    with pytest.raises(ProviderProfileError) as provider_error:
        providers.update_profile("provider-a", {"name": "stale", "expected_revision": 1})
    assert provider_error.value.code == "revision_conflict"

    agents = AgentDefinitionStore(tmp_path / "static", library_root=tmp_path / "agents")
    agent = agents.create_agent({"agent_id": "agent-a", "name": "Agent", "instruction": "Write."})
    assert agent["revision"] == 1
    agent = agents.update_agent("agent-a", {"name": "Agent 2", "expected_revision": 1})
    assert agent["revision"] == 2
    with pytest.raises(AgentDefinitionError):
        agents.update_agent("agent-a", {"name": "stale", "expected_revision": 1})

    graphs = GraphDefinitionStore(tmp_path / "static", library_root=tmp_path / "graphs")
    graph = graphs.create_graph(
        {"id": "graph-a", "name": "Graph", "nodes": [{"node_id": "node-a", "agent_id": "agent-a"}]}
    )
    assert graph["revision"] == 1
    graph = graphs.update_graph("graph-a", {"name": "Graph 2", "expected_revision": 1})
    assert graph["revision"] == 2
    with pytest.raises(GraphDefinitionError):
        graphs.update_graph("graph-a", {"name": "stale", "expected_revision": 1})

    regex = RegexCollectionLibrary(tmp_path / "static", library_root=tmp_path / "regex")
    collection = regex.create_collection({"id": "regex-a", "name": "Regex", "rules": []})
    assert collection["revision"] == 1
    collection = regex.update_collection("regex-a", {"name": "Regex 2", "expected_revision": 1})
    assert collection["revision"] == 2
    with pytest.raises(RegexCollectionError):
        regex.update_collection("regex-a", {"name": "stale", "expected_revision": 1})

    for root in (tmp_path / "providers", tmp_path / "agents", tmp_path / "graphs", tmp_path / "regex"):
        records = _audit_records(root)
        assert records[-1]["revision"] == 2
        assert records[-1]["changed_paths"]
