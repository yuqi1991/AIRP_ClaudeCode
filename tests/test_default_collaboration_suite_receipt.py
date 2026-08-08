from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from airp.application import Application  # noqa: E402
from airp.workspace import Workspace  # noqa: E402


def test_complete_receipt_makes_install_an_unconditional_no_op(tmp_path):
    static_root = tmp_path / "web"
    static_root.mkdir()
    application = Application.assemble(
        static_root=static_root,
        workspace=Workspace.from_root(tmp_path / "workspace"),
    )
    first = application.default_collaboration_suite.install_once()
    writer = application.agent_store.get_agent(first["writer_agent_id"])
    application.agent_store.update_agent(
        writer["agent_id"],
        {"expected_revision": writer["revision"], "instruction": "user edited"},
    )
    application.graph_store.delete_graph(first["graph_id"])

    second = application.default_collaboration_suite.install_once()

    assert second == {**first, "installed": False}
    assert application.agent_store.get_agent(first["writer_agent_id"])["instruction"] == "user edited"
    assert all(graph["id"] != first["graph_id"] for graph in application.graph_store.list_graphs())
