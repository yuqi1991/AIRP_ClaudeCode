from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from airp.application import Application  # noqa: E402
from airp.default_collaboration_suite import DefaultCollaborationSuiteError  # noqa: E402
from airp.workspace import Workspace  # noqa: E402


def _installed_application(tmp_path: Path) -> tuple[Application, str]:
    static_root = tmp_path / "web"
    static_root.mkdir()
    application = Application.assemble(
        static_root=static_root,
        workspace=Workspace.from_root(tmp_path / "workspace"),
    )
    graph_id = application.default_collaboration_suite.install_once()["graph_id"]
    return application, graph_id


def test_initialize_project_attempts_activation_only_once_per_project_lifecycle(tmp_path):
    application, graph_id = _installed_application(tmp_path)
    application.projects.create_project({"id": "fresh", "name": "Fresh"})

    assert application.default_collaboration_suite.initialize_project("fresh") is True
    assert application.active_graphs.graph_id_for("fresh") == graph_id

    application.active_graphs.clear("fresh")
    assert application.default_collaboration_suite.initialize_project("fresh") is False
    assert application.active_graphs.graph_id_for("fresh") is None


def test_initialize_project_preserves_an_existing_choice_and_never_reactivates(tmp_path):
    application, _ = _installed_application(tmp_path)
    application.projects.create_project({"id": "configured", "name": "Configured"})
    application.active_graphs.select("configured", "user-graph")

    assert application.default_collaboration_suite.initialize_project("configured") is False
    assert application.active_graphs.graph_id_for("configured") == "user-graph"

    application.active_graphs.clear("configured")
    assert application.default_collaboration_suite.initialize_project("configured") is False
    assert application.active_graphs.graph_id_for("configured") is None


def test_initialize_project_reverts_selection_when_initialization_ledger_write_fails(
    tmp_path, monkeypatch
):
    application, _ = _installed_application(tmp_path)
    application.projects.create_project({"id": "ledger-failure", "name": "Ledger failure"})
    real_replace = os.replace

    def fail_ledger_replace(source, destination):
        if Path(destination).name == "initialized_projects.json":
            raise OSError("Authorization: secret-value")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_ledger_replace)

    with pytest.raises(DefaultCollaborationSuiteError) as error:
        application.default_collaboration_suite.initialize_project("ledger-failure")

    assert error.value.code == "default_collaboration_suite_project_activation_failed"
    assert "secret-value" not in str(error.value)
    assert application.active_graphs.graph_id_for("ledger-failure") is None
