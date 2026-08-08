from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from airp.application import Application  # noqa: E402
from airp.engine.graph_definitions import GraphDefinitionError  # noqa: E402
from airp.workspace import Workspace  # noqa: E402


def _installed_application(tmp_path: Path) -> tuple[Application, dict]:
    static_root = tmp_path / "web"
    static_root.mkdir()
    application = Application.assemble(
        static_root=static_root,
        workspace=Workspace.from_root(tmp_path / "workspace"),
    )
    return application, application.default_collaboration_suite.install_once()


def test_default_regex_extracts_tagged_multiline_and_clears_untagged_or_malformed(tmp_path):
    application, receipt = _installed_application(tmp_path)
    collection_id = receipt["regex_collection_id"]

    examples = {
        "<content>正文</content>": "正文",
        "<content>第一行\n第二行</content>": "第一行\n第二行",
        "没有标签": "",
        "<content>缺少闭合标签": "",
        "前缀<content>正文</content>": "",
        "<content>正文</content>后缀": "",
        "<content>一</content><content>二</content>": "",
        "<content>外层<content>内层</content></content>": "",
    }
    for raw, expected in examples.items():
        transformed = application.regex_collections.test_collection(
            collection_id, text=raw, target="output"
        )
        assert transformed["text"] == expected


def test_project_update_does_not_create_a_new_initialization_lifecycle(tmp_path):
    application, receipt = _installed_application(tmp_path)
    application.projects.create_project({"id": "updated", "name": "Before"})
    assert application.default_collaboration_suite.initialize_project("updated") is True

    project = application.projects.get_project("updated")
    application.projects.update_project(
        "updated",
        {"expected_revision": project["revision"], "name": "After"},
    )
    application.active_graphs.clear("updated")

    assert application.default_collaboration_suite.initialize_project("updated") is False
    assert application.active_graphs.graph_id_for("updated") is None
    assert receipt["graph_id"] in {graph["id"] for graph in application.graph_store.list_graphs()}


def test_recreated_project_id_has_a_new_initialization_lifecycle(tmp_path):
    application, receipt = _installed_application(tmp_path)
    application.projects.create_project({"id": "reused", "name": "First"})
    first_instance = application.projects.project_instance_id("reused")
    assert application.default_collaboration_suite.initialize_project("reused") is True

    application.projects.delete_project("reused")
    application.active_graphs.clear("reused")
    application.projects.create_project({"id": "reused", "name": "Second"})

    assert application.projects.project_instance_id("reused") != first_instance
    assert application.default_collaboration_suite.initialize_project("reused") is True
    assert application.active_graphs.graph_id_for("reused") == receipt["graph_id"]


def test_corrupt_graph_error_propagates_without_marking_project_initialized(tmp_path):
    application, receipt = _installed_application(tmp_path)
    application.projects.create_project({"id": "corrupt", "name": "Corrupt"})
    graph_path = application.workspace.graphs_root / f"{receipt['graph_id']}.json"
    original = graph_path.read_text(encoding="utf-8")
    graph_path.write_text("{", encoding="utf-8")

    with pytest.raises(GraphDefinitionError) as error:
        application.default_collaboration_suite.initialize_project("corrupt")
    assert error.value.code == "invalid_graph_data"

    graph_path.write_text(original, encoding="utf-8")
    assert application.default_collaboration_suite.initialize_project("corrupt") is True
