from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from airp.application import Application  # noqa: E402
from airp.workspace import Workspace  # noqa: E402


def test_application_assembly_owns_studio_store_locations(tmp_path):
    static_root = tmp_path / "web"
    static_root.mkdir()
    workspace = Workspace.from_root(tmp_path / "workspace")

    application = Application.assemble(static_root=static_root, workspace=workspace)

    assert application.agent_store.library_root == workspace.agents_root
    assert application.graph_store.library_root == workspace.graphs_root
    assert application.worldbooks.library_root == workspace.worldbooks_root
    assert application.projects.project_root == workspace.projects_root
    assert application.provider_profile_store.library_root == workspace.providers_root
    assert application.provider_secret_store.path == workspace.secrets_path


def test_application_assembly_has_no_legacy_studio_objects_without_static_root():
    application = Application.assemble(static_root=None, workspace=None)

    assert application.provider_profiles is None
    assert application.agent_definitions is None
    assert application.graph_definitions is None
    assert application.worldbooks is None
    assert application.projects is None


def test_application_assembly_preserves_workspace_without_static_root(tmp_path):
    workspace = Workspace.from_root(tmp_path / "workspace")

    application = Application.assemble(static_root=None, workspace=workspace)

    assert application.workspace == workspace
    assert workspace.runtime_root.is_dir()


def test_application_initialize_returns_only_the_public_startup_report(tmp_path):
    static_root = tmp_path / "web"
    static_root.mkdir()
    application = Application.assemble(
        static_root=static_root,
        workspace=Workspace.from_root(tmp_path / "workspace"),
    )

    report = application.initialize()

    assert report == {"ok": True, "status": "success", "diagnostics": []}
    assert application.startup == report
    assert "provider_profile_id" not in report
