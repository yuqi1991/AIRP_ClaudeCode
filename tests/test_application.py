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


def test_application_startup_reports_receipt_ids_after_library_collisions(tmp_path):
    static_root = tmp_path / "web"
    static_root.mkdir()
    application = Application.assemble(
        static_root=static_root,
        workspace=Workspace.from_root(tmp_path / "workspace"),
    )
    application.provider_profiles.create_profile({
        "id": "default-deepseek", "name": "Existing Provider",
        "base_url": "https://example.invalid", "api_format": "chat_completions",
    })
    application.regex_collections.create_collection({
        "id": "default-content", "name": "Existing Regex", "rules": [],
    })
    for agent_id in ("default-writer", "default-reviewer"):
        application.agent_definitions.create_agent({
            "agent_id": agent_id, "name": "Existing " + agent_id, "instruction": "existing",
        })
    application.graph_definitions.create_graph({
        "id": "default-two-round-review", "name": "Existing Graph",
        "nodes": [{"node_id": "existing", "agent_id": "default-writer"}],
        "output_node_id": "existing",
    })

    report = application.initialize()

    assert report["initial_resource_ids"] == {
        "provider": "default-deepseek-copy",
        "regex": "default-content-copy",
        "writer": "default-writer-copy",
        "reviewer": "default-reviewer-copy",
        "graph": "default-two-round-review-copy",
    }


def test_application_initialize_returns_only_the_public_startup_report(tmp_path):
    static_root = tmp_path / "web"
    static_root.mkdir()
    application = Application.assemble(
        static_root=static_root,
        workspace=Workspace.from_root(tmp_path / "workspace"),
    )

    report = application.initialize()

    assert report == {
        "ok": True,
        "status": "success",
        "diagnostics": [],
        "initial_resource_ids": {
            "provider": "default-deepseek",
            "regex": "default-content",
            "writer": "default-writer",
            "reviewer": "default-reviewer",
            "graph": "default-two-round-review",
        },
    }
    assert application.startup == report
    assert "provider_profile_id" not in report
    assert "transaction_id" not in report
    assert "api_key" not in str(report)
