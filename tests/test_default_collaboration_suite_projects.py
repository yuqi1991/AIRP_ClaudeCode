from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from airp.application import Application  # noqa: E402
from airp.default_collaboration_suite import DefaultCollaborationSuiteError, ProjectInitializationResult  # noqa: E402
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

    assert application.default_collaboration_suite.initialize_project("fresh").selected is True
    assert application.active_graphs.graph_id_for("fresh") == graph_id

    application.active_graphs.clear("fresh")
    assert application.default_collaboration_suite.initialize_project("fresh").selected is False
    assert application.active_graphs.graph_id_for("fresh") is None


def test_initialize_project_preserves_an_existing_choice_and_never_reactivates(tmp_path):
    application, _ = _installed_application(tmp_path)
    application.projects.create_project({"id": "configured", "name": "Configured"})
    application.active_graphs.select("configured", "user-graph")

    assert application.default_collaboration_suite.initialize_project("configured").selected is False
    assert application.active_graphs.graph_id_for("configured") == "user-graph"

    application.active_graphs.clear("configured")
    assert application.default_collaboration_suite.initialize_project("configured").selected is False
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


def test_failed_initialization_compensation_preserves_a_concurrent_user_selection(
    tmp_path, monkeypatch
):
    application, _ = _installed_application(tmp_path)
    application.projects.create_project({"id": "race", "name": "Race"})
    real_replace = os.replace

    def fail_ledger_after_user_change(source, destination):
        if Path(destination).name == "initialized_projects.json":
            application.active_graphs.select("race", "user-graph")
            raise OSError("ledger failed")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_ledger_after_user_change)

    with pytest.raises(DefaultCollaborationSuiteError):
        application.default_collaboration_suite.initialize_project("race")

    assert application.active_graphs.graph_id_for("race") == "user-graph"


def test_failed_initialization_compensation_preserves_same_value_user_reselection(
    tmp_path, monkeypatch
):
    application, graph_id = _installed_application(tmp_path)
    application.projects.create_project({"id": "aba-race", "name": "ABA race"})
    real_replace = os.replace

    def fail_ledger_after_same_value_user_change(source, destination):
        if Path(destination).name == "initialized_projects.json":
            application.active_graphs.select("aba-race", "user-graph")
            application.active_graphs.select("aba-race", graph_id)
            raise OSError("ledger failed")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_ledger_after_same_value_user_change)

    with pytest.raises(DefaultCollaborationSuiteError):
        application.default_collaboration_suite.initialize_project("aba-race")

    assert application.active_graphs.graph_id_for("aba-race") == graph_id


def test_project_deletion_token_removes_and_can_restore_the_initialization_lifecycle(tmp_path):
    application, _ = _installed_application(tmp_path)
    project = application.projects.create_project({"id": "forgotten", "name": "Forgotten"})
    application.default_collaboration_suite.initialize_project("forgotten")

    deletion = application.default_collaboration_suite.begin_project_deletion("forgotten")
    deletion.rollback()
    deletion = application.default_collaboration_suite.begin_project_deletion("forgotten")
    application.active_graphs.clear("forgotten")
    application.projects.delete_project("forgotten")
    recreated = application.projects.create_project({"id": "forgotten", "name": "Recreated"})
    deletion.commit()

    assert recreated["instance_id"] != project["instance_id"]
    assert application.default_collaboration_suite.initialize_project("forgotten").selected is True


def test_initialize_project_returns_an_explicit_result(tmp_path):
    application, graph_id = _installed_application(tmp_path)
    application.projects.create_project({"id": "result", "name": "Result"})

    initialized = application.default_collaboration_suite.initialize_project("result")
    repeated = application.default_collaboration_suite.initialize_project("result")

    assert initialized == ProjectInitializationResult(selected=True, warning=None)
    assert repeated == ProjectInitializationResult(selected=False, warning=None)
    assert application.active_graphs.graph_id_for("result") == graph_id


def test_selection_claim_is_scoped_per_project_and_store(tmp_path):
    application, graph_id = _installed_application(tmp_path)
    claim = application.active_graphs.select_if_unset_claim("claim-a", graph_id)

    application.active_graphs.select("claim-b", "other-graph")

    assert application.active_graphs.clear_claim(claim) is True
    assert application.active_graphs.graph_id_for("claim-a") is None
    assert application.active_graphs.graph_id_for("claim-b") == "other-graph"


def test_memory_selection_claim_cannot_be_used_by_another_store():
    from airp.engine.active_graph import ActiveGraphSelectionStore

    first = ActiveGraphSelectionStore()
    second = ActiveGraphSelectionStore()
    claim = first.select_if_unset_claim("same-project", "same-graph")
    second.select("same-project", "same-graph")

    assert second.clear_claim(claim) is False
    assert second.graph_id_for("same-project") == "same-graph"
