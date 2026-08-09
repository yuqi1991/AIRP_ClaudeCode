from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from airp.application import Application  # noqa: E402
from airp.default_collaboration_suite import DefaultCollaborationSuiteError  # noqa: E402
from airp.workspace import Workspace  # noqa: E402


def _application(root: Path) -> Application:
    static_root = root / "web"
    static_root.mkdir(exist_ok=True)
    return Application.assemble(
        static_root=static_root,
        workspace=Workspace.from_root(root / "workspace"),
    )


def _interrupt_after_replace(root: Path, boundary: str = "regex_collection") -> subprocess.CompletedProcess[str]:
    script = """
import os
from pathlib import Path
from unittest.mock import patch

from airp.application import Application
from airp.workspace import Workspace
import airp.engine.regex_collections as regex_module

root = Path(os.environ["AIRP_TEST_ROOT"])
boundary = os.environ["AIRP_TEST_BOUNDARY"]
real_replace = os.replace

def exit_after_regex_replace(source, destination):
    real_replace(source, destination)
    destination = Path(destination)
    matches = {
        "provider": destination.parent.name == "providers" and destination.suffix == ".json",
        "regex_collection": destination.parent.name == "regex_collections" and destination.suffix == ".json",
        "writer_agent": destination.name == "default-writer.json",
        "reviewer_agent": destination.name == "default-reviewer.json",
        "graph": destination.parent.name == "graphs" and destination.suffix == ".json",
        "project_activation": destination.name == "active_graphs.json",
    }
    if matches[boundary]:
        os._exit(73)

application = Application.assemble(
    static_root=root / "web",
    workspace=Workspace.from_root(root / "workspace"),
)
with patch.object(regex_module.os, "replace", exit_after_regex_replace):
    application.default_collaboration_suite.install_once()
"""
    environment = {
        **os.environ,
        "AIRP_TEST_ROOT": str(root),
        "AIRP_TEST_BOUNDARY": boundary,
        "PYTHONPATH": str(ROOT / "src"),
    }
    return subprocess.run(
        [sys.executable, "-c", script],
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    "boundary",
    ["provider", "regex_collection", "writer_agent", "reviewer_agent", "graph", "project_activation"],
)
def test_restart_compensates_an_object_persisted_before_the_creating_process_exits(
    tmp_path, boundary
):
    static_root = tmp_path / "web"
    static_root.mkdir()
    if boundary == "project_activation":
        _application(tmp_path).projects.create_project({"id": "story", "name": "Story"})

    interrupted = _interrupt_after_replace(tmp_path, boundary)

    assert interrupted.returncode == 73
    restarted = _application(tmp_path)
    recovered = restarted.default_collaboration_suite.install_once()
    assert recovered == {
        "installed": False,
        "status": "degraded",
        "diagnostics": [
            {
                "code": "default_collaboration_suite_install_failed",
                "boundary": "recovery",
                "message": "默认协作套件安装失败；已撤销本次创建的对象，可在解决本地存储问题后重试。",
                "action": "检查 Workspace 是否可写，然后重启 AIRP。",
            }
        ],
    }
    assert restarted.provider_profile_store.list_profiles() == []
    assert restarted.regex_collections.list_collections() == []
    assert restarted.agent_store.list_agents() == []
    assert restarted.graph_store.list_graphs() == []
    if boundary == "project_activation":
        assert restarted.active_graphs.graph_id_for("story") is None

    installed = restarted.default_collaboration_suite.install_once()
    assert installed["installed"] is True
    assert len(restarted.provider_profile_store.list_profiles()) == 1
    assert len(restarted.regex_collections.list_collections()) == 1
    assert len(restarted.agent_store.list_agents()) == 2
    assert len(restarted.graph_store.list_graphs()) == 1


def test_restart_rejects_a_shape_valid_but_modified_pending_journal(tmp_path):
    static_root = tmp_path / "web"
    static_root.mkdir()
    assert _interrupt_after_replace(tmp_path).returncode == 73
    pending_path = tmp_path / "workspace/runtime/default_collaboration_suite/pending.json"
    pending = json.loads(pending_path.read_text(encoding="utf-8"))
    writer = next(item for item in pending["resources"] if item["kind"] == "writer_agent")
    writer["payload"]["instruction"] = "modified outside the installer"
    pending_path.write_text(json.dumps(pending), encoding="utf-8")

    restarted = _application(tmp_path)
    with pytest.raises(DefaultCollaborationSuiteError) as error:
        restarted.default_collaboration_suite.install_once()
    assert error.value.code == "default_collaboration_suite_journal_invalid"


def test_recovery_preserves_a_reused_provider_and_its_secret(tmp_path):
    static_root = tmp_path / "web"
    static_root.mkdir()
    application = _application(tmp_path)
    provider = application.provider_profile_store.create_profile(
        {
            "id": "user-deepseek",
            "name": "User DeepSeek",
            "base_url": "https://api.deepseek.com",
            "api_format": "chat_completions",
            "enabled": True,
        }
    )
    application.provider_secret_store.set(provider["id"], "existing-secret")

    assert _interrupt_after_replace(tmp_path).returncode == 73
    restarted = _application(tmp_path)
    result = restarted.default_collaboration_suite.install_once()

    assert result["status"] == "degraded"
    assert restarted.provider_profile_store.get_profile(provider["id"]) == provider
    assert restarted.provider_secret_store.get(provider["id"]) == "existing-secret"
    assert restarted.regex_collections.list_collections() == []
    assert restarted.agent_store.list_agents() == []
    assert restarted.graph_store.list_graphs() == []


def test_recovery_blocks_startup_when_reverse_compensation_fails(
    tmp_path, monkeypatch
):
    static_root = tmp_path / "web"
    static_root.mkdir()
    assert _interrupt_after_replace(tmp_path).returncode == 73
    real_unlink = Path.unlink

    def fail_regex_delete(path, *args, **kwargs):
        if path.parent.name == "regex_collections" and path.suffix == ".json":
            raise OSError("Authorization: secret-value")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_regex_delete)
    restarted = _application(tmp_path)

    with pytest.raises(DefaultCollaborationSuiteError) as error:
        restarted.default_collaboration_suite.install_once()

    assert error.value.code == "default_collaboration_suite_compensation_failed"
    assert "secret-value" not in str(error.value)


def test_restart_rolls_forward_a_matching_receipt_written_before_cleanup(
    tmp_path, monkeypatch
):
    application = _application(tmp_path)
    real_replace = os.replace

    def interrupt_after_receipt(source, destination):
        real_replace(source, destination)
        if Path(destination).name == "receipt.json":
            raise SystemExit(74)

    monkeypatch.setattr(os, "replace", interrupt_after_receipt)
    with pytest.raises(SystemExit):
        application.default_collaboration_suite.install_once()
    monkeypatch.undo()

    restarted = _application(tmp_path)
    result = restarted.default_collaboration_suite.install_once()

    assert result["installed"] is False
    assert len(restarted.provider_profile_store.list_profiles()) == 1
    assert len(restarted.regex_collections.list_collections()) == 1


def test_receipt_post_replace_error_rolls_forward_the_matching_transaction(
    tmp_path, monkeypatch
):
    application = _application(tmp_path)
    real_replace = os.replace

    def fail_after_receipt_replace(source, destination):
        real_replace(source, destination)
        if Path(destination).name == "receipt.json":
            raise OSError("Authorization: secret-value")

    monkeypatch.setattr(os, "replace", fail_after_receipt_replace)

    result = application.default_collaboration_suite.install_once()

    assert result["installed"] is True
    assert "secret-value" not in repr(result)
    monkeypatch.undo()
    restarted = _application(tmp_path)
    assert restarted.default_collaboration_suite.install_once() == {
        **result,
        "installed": False,
    }
    assert len(restarted.agent_store.list_agents()) == 2
    assert len(restarted.graph_store.list_graphs()) == 1


def test_install_blocks_when_activation_and_its_local_compensation_both_fail(
    tmp_path, monkeypatch
):
    application = _application(tmp_path)
    application.projects.create_project({"id": "story", "name": "Story"})
    real_replace = os.replace
    active_writes = 0

    def fail_activation_boundaries(source, destination):
        nonlocal active_writes
        destination = Path(destination)
        if destination.name == "active_graphs.json":
            active_writes += 1
            if active_writes == 2:
                raise OSError("Authorization: clear-secret")
        if destination.name == "initialized_projects.json":
            raise OSError("Authorization: ledger-secret")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_activation_boundaries)

    with pytest.raises(DefaultCollaborationSuiteError) as error:
        application.default_collaboration_suite.install_once()

    assert error.value.code == "default_collaboration_suite_compensation_failed"
    assert "secret" not in str(error.value).casefold()


def test_install_blocks_when_a_frozen_project_becomes_unreadable(
    tmp_path, monkeypatch
):
    application = _application(tmp_path)
    application.projects.create_project({"id": "story", "name": "Story"})
    project_path = application.workspace.projects_root / "story.json"
    real_read_text = Path.read_text
    reads = 0

    def fail_lifecycle_read(path, *args, **kwargs):
        nonlocal reads
        if path == project_path:
            reads += 1
            if reads >= 3:
                raise OSError("Authorization: project-secret")
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", fail_lifecycle_read)

    with pytest.raises(DefaultCollaborationSuiteError) as error:
        application.default_collaboration_suite.install_once()

    assert error.value.code == "default_collaboration_suite_project_lifecycle_invalid"
    assert "project-secret" not in str(error.value)


def test_install_keeps_the_suite_when_one_project_selection_cannot_be_written(
    tmp_path, monkeypatch
):
    application = _application(tmp_path)
    application.projects.create_project({"id": "story", "name": "Story"})
    real_replace = os.replace

    def fail_active_selection(source, destination):
        if Path(destination).name == "active_graphs.json":
            raise OSError("selection write failed")
        return real_replace(source, destination)

    monkeypatch.setattr(os, "replace", fail_active_selection)
    result = application.default_collaboration_suite.install_once()
    monkeypatch.undo()

    assert result["installed"] is True
    assert result["status"] == "degraded"
    assert result["diagnostics"] == [
        {
            "code": "default_collaboration_suite_project_activation_failed",
            "boundary": "project_activation",
            "project_id": "story",
            "message": "默认协作套件已安装，但未能为一个 Project 自动选择 Graph。",
            "action": "在 Studio 的编排选择中手动选择 Graph。",
        }
    ]
    assert application.active_graphs.graph_id_for("story") is None
    assert len(application.graph_store.list_graphs()) == 1
    assert application.default_collaboration_suite.install_once()["installed"] is False
