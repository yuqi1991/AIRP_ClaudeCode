from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from airp.engine.graph_runtime import AgentArtifact, NodeResult  # noqa: E402
from airp.engine.active_graph import ActiveGraphSelectionError  # noqa: E402
from airp.engine.project_library import ProjectLibraryError  # noqa: E402
from airp.host.rp.session_runtime import SessionTurnRuntime  # noqa: E402
from airp.host.rp.project_runtime import ProjectRuntimeStore  # noqa: E402
from airp.server import SessionRuntimeServer  # noqa: E402


def _json_request(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = Request(
        url,
        data=data,
        method=method,
        headers={"Accept": "application/json", "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_server_exposes_the_complete_keyless_suite_through_ordinary_studio_crud(tmp_path):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    card.mkdir()
    (card / ".card_data.json").write_text('{"name":"HTTP Suite"}', encoding="utf-8")
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
    )

    with SessionRuntimeServer(
        runtime,
        static_root=styles,
        workspace=tmp_path / "workspace",
    ) as server:
        status, startup = _json_request("GET", f"{server.base_url}/v1/studio/startup")
        assert status == 200
        assert startup == {
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
        endpoints = {
            "providers": ("profiles", "default-deepseek", "profile"),
            "agents": ("agents", "default-writer", "agent"),
            "regex-collections": ("collections", "default-content", "collection"),
            "graphs": ("graphs", "default-two-round-review", "graph"),
        }
        fetched = {}
        for endpoint, (list_key, object_id, get_key) in endpoints.items():
            base_url = f"{server.base_url}/v1/studio/{endpoint}"
            status, listed = _json_request("GET", base_url)
            assert status == 200
            assert object_id in {
                item.get("id", item.get("agent_id")) for item in listed[list_key]
            }
            status, payload = _json_request("GET", f"{base_url}/{object_id}")
            assert status == 200
            fetched[get_key] = payload[get_key]

        assert fetched["profile"]["key_configured"] is False
        assert fetched["agent"]["provider_profile_id"] == fetched["profile"]["id"]
        assert fetched["agent"]["regex_collection_id"] == fetched["collection"]["id"]
        assert fetched["graph"]["output_node_id"] == "final-writer"

        writer_url = f"{server.base_url}/v1/studio/agents/default-writer"
        status, updated = _json_request(
            "PUT",
            writer_url,
            {
                "expected_revision": fetched["agent"]["revision"],
                "instruction": "用户通过普通 Studio API 编辑后的 Instruction",
            },
        )
        assert status == 200
        assert updated["agent"]["instruction"] == "用户通过普通 Studio API 编辑后的 Instruction"
        status, reloaded = _json_request("GET", writer_url)
        assert status == 200
        assert reloaded["agent"]["instruction"] == updated["agent"]["instruction"]

        status, conflict = _json_request(
            "PUT", writer_url,
            {"expected_revision": fetched["agent"]["revision"], "instruction": "stale"},
        )
        assert status == 409
        assert conflict["error"] == "revision_conflict"
        assert conflict["current_revision"] == updated["agent"]["revision"]
        assert conflict["current_object"] == updated["agent"]
        assert conflict["reload_source"] == "/v1/studio/agents/default-writer"


def test_recreated_project_id_gets_a_fresh_default_graph_activation(tmp_path):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    card.mkdir()
    (card / ".card_data.json").write_text('{"name":"Lifecycle"}', encoding="utf-8")
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
    )

    with SessionRuntimeServer(
        runtime,
        static_root=styles,
        workspace=tmp_path / "workspace",
    ) as server:
        status, _ = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/graphs",
            {
                "id": "user-graph",
                "name": "User graph",
                "nodes": [{"node_id": "writer", "agent_id": "default-writer"}],
                "output_node_id": "writer",
            },
        )
        assert status == 201
        project_url = f"{server.base_url}/v1/studio/projects/reused"
        status, _ = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/projects",
            {"id": "reused", "name": "First lifecycle"},
        )
        assert status == 201
        status, _ = _json_request(
            "POST",
            f"{server.base_url}/v1/session/project/switch",
            {"project_id": "reused"},
        )
        assert status == 200
        status, _ = _json_request(
            "PUT",
            f"{server.base_url}/v1/session/runtime/graph",
            {"graph_id": "user-graph"},
        )
        assert status == 200

        status, _ = _json_request("DELETE", project_url)
        assert status == 200
        status, _ = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/projects",
            {"id": "reused", "name": "Second lifecycle"},
        )
        assert status == 201
        status, _ = _json_request(
            "POST",
            f"{server.base_url}/v1/session/project/switch",
            {"project_id": "reused"},
        )
        assert status == 200
        status, selected = _json_request(
            "GET",
            f"{server.base_url}/v1/session/runtime/graph",
        )

        assert status == 200
        assert selected["selected"]["graph_id"] == "default-two-round-review"


def test_project_creation_reports_a_warning_when_default_graph_cannot_be_written(
    tmp_path, monkeypatch
):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    card.mkdir()
    (card / ".card_data.json").write_text('{"name":"Warning"}', encoding="utf-8")
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
    )

    with SessionRuntimeServer(
        runtime,
        static_root=styles,
        workspace=tmp_path / "workspace",
    ) as server:
        real_replace = os.replace

        def fail_active_graph_write(source, destination):
            if Path(destination).name == "active_graphs.json":
                raise OSError("cannot persist this legal selection")
            return real_replace(source, destination)

        monkeypatch.setattr("airp.engine.active_graph.os.replace", fail_active_graph_write)
        status, created = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/projects",
            {"id": "warning-project", "name": "Warning project"},
        )
        monkeypatch.undo()

        assert status == 201
        assert created["project"]["id"] == "warning-project"
        assert created["warnings"] == [
            {
                "code": "default_collaboration_suite_project_activation_failed",
                "boundary": "project_activation",
                "project_id": "warning-project",
                "message": "默认协作套件已安装，但未能为一个 Project 自动选择 Graph。",
                "action": "在游戏 Monitor 的 Graph 下拉中手动选择 Graph。",
            }
        ]
        status, loaded = _json_request(
            "GET",
            f"{server.base_url}/v1/studio/projects/warning-project",
        )
        assert status == 200
        assert loaded["project"]["id"] == "warning-project"
        assert server.active_graphs.graph_id_for("warning-project") is None


def test_project_creation_fails_closed_for_corrupt_active_graph_data(tmp_path):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    card.mkdir()
    (card / ".card_data.json").write_text('{"name":"Corrupt"}', encoding="utf-8")
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
    )

    with SessionRuntimeServer(
        runtime,
        static_root=styles,
        workspace=tmp_path / "workspace",
    ) as server:
        selection_path = server.application.workspace.active_graph_selections_path
        selection_path.write_text("{", encoding="utf-8")

        status, failed = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/projects",
            {"id": "corrupt-project", "name": "Corrupt project"},
        )

        assert status == 500
        assert failed["error"] == "active_graph_selection_unreadable"
        assert selection_path.read_text(encoding="utf-8") == "{"
        assert "corrupt-project" not in {
            project["id"] for project in server.projects.list_projects()
        }


def test_project_creation_during_generation_preserves_the_running_plan_until_next_task(
    tmp_path, monkeypatch
):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    card.mkdir()
    (card / ".card_data.json").write_text('{"name":"Running"}', encoding="utf-8")
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
    )

    with SessionRuntimeServer(
        runtime,
        static_root=styles,
        workspace=tmp_path / "workspace",
    ) as server:
        running_trace = object()
        runtime.running_trace = running_trace
        frozen = (
            runtime.project_id,
            runtime.execution_plan_compiler,
            runtime.graph_runtime,
            runtime.running_trace,
        )
        monkeypatch.setattr(runtime, "generation_active", lambda: True)

        status, created = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/projects",
            {"id": "next-project", "name": "Next project"},
        )
        status2, created2 = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/projects",
            {"id": "later-project", "name": "Later project"},
        )
        assert status2 == 201 and created2["project"]["id"] == "later-project"

        assert status == 201
        assert created["project"]["id"] == "next-project"
        assert server.active_graphs.graph_id_for("next-project") == "default-two-round-review"
        assert (
            runtime.project_id,
            runtime.execution_plan_compiler,
            runtime.graph_runtime,
            runtime.running_trace,
        ) == frozen

        monkeypatch.undo()
        server.submit_async("Use the next Project snapshot", "next-project-task")
        assert server.runtime.project_id == "next-project"
        assert server.runtime.execution_graph_id == "default-two-round-review"
        assert server.runtime.execution_plan_compiler is not None
        assert server.runtime.graph_runtime is not None


def test_agent_save_during_a_running_task_only_changes_the_next_task_plan(
    tmp_path, monkeypatch
):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    card.mkdir()
    (card / ".card_data.json").write_text('{"name":"Frozen config"}', encoding="utf-8")
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
    )

    class BlockingRunner:
        def __init__(self):
            self.started = threading.Event()
            self.release = threading.Event()
            self.instructions = []

        def next_task(self):
            self.started.clear()
            self.release.clear()

        def run(self, node, input_artifact):
            self.instructions.append(node.agent.instruction)
            if not self.started.is_set():
                self.started.set()
                if not self.release.wait(5):
                    raise TimeoutError("test runner release timed out")
            return NodeResult.succeeded(AgentArtifact.text("<content>candidate</content>"))

    with SessionRuntimeServer(
        runtime,
        static_root=styles,
        workspace=tmp_path / "workspace",
    ) as server:
        status, created = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/projects",
            {"id": "frozen-project", "name": "Frozen project"},
        )
        assert status == 201
        status, _ = _json_request(
            "POST",
            f"{server.base_url}/v1/session/project/switch",
            {"project_id": created["project"]["id"]},
        )
        assert status == 200

        writer_url = f"{server.base_url}/v1/studio/agents/default-writer"
        status, loaded = _json_request("GET", writer_url)
        assert status == 200
        old_revision = loaded["agent"]["revision"]
        old_instruction = loaded["agent"]["instruction"]

        runner = BlockingRunner()
        server.runtime.graph_runtime.node_runner = runner
        monkeypatch.setattr(server, "_configure_runtime_studio_graph", lambda: None)

        server.submit_async("first", "frozen-config-first")
        assert runner.started.wait(5)
        status, first_snapshot = _json_request("GET", f"{server.base_url}/v1/session/snapshot")
        assert status == 200
        first_plan = first_snapshot["graph_runs"]["current"]["plan"]
        first_agents = {item["id"]: item for item in first_plan["provenance"]["agents"]}
        assert first_agents["default-writer"]["revision"] == old_revision

        status, saved = _json_request(
            "PUT",
            writer_url,
            {
                "expected_revision": old_revision,
                "instruction": "Instruction saved while the prior Task is running",
            },
        )
        assert status == 200
        new_revision = saved["agent"]["revision"]
        assert new_revision > old_revision
        assert runner.instructions[0] == old_instruction

        runner.release.set()
        server._submit_threads[-1].join(timeout=5)
        assert not server._submit_threads[-1].is_alive()

        runner.next_task()
        server.submit_async("second", "frozen-config-second")
        assert runner.started.wait(5)
        status, second_snapshot = _json_request("GET", f"{server.base_url}/v1/session/snapshot")
        assert status == 200
        second_plan = second_snapshot["graph_runs"]["current"]["plan"]
        second_agents = {item["id"]: item for item in second_plan["provenance"]["agents"]}
        assert second_agents["default-writer"]["revision"] == new_revision
        assert runner.instructions[-1] == saved["agent"]["instruction"]
        runner.release.set()


def test_imported_first_project_is_ready_for_the_next_task(tmp_path):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    card.mkdir()
    (card / ".card_data.json").write_text('{"name":"Import runtime"}', encoding="utf-8")
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
    )

    with SessionRuntimeServer(
        runtime,
        static_root=styles,
        workspace=tmp_path / "workspace",
    ) as server:
        status, imported = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/projects/import",
            {
                "id": "imported",
                "document": {
                    "spec": "chara_card_v2",
                    "data": {"name": "Imported runtime"},
                },
            },
        )

        assert status == 201
        assert imported["project"]["id"] == "imported"
        assert server.runtime.project_id == "imported"
        assert server.runtime.execution_graph_id == "default-two-round-review"
        assert server.runtime.execution_plan_compiler is not None



def test_project_delete_restores_ledger_when_selection_cleanup_fails(tmp_path, monkeypatch):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    card.mkdir()
    (card / ".card_data.json").write_text("{}", encoding="utf-8")
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
    )

    with SessionRuntimeServer(
        runtime, static_root=styles, workspace=tmp_path / "workspace"
    ) as server:
        status, created = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/projects",
            {"id": "delete-failure", "name": "Delete failure"},
        )
        assert status == 201
        lifecycle = created["project"]["instance_id"]
        selection = server.active_graphs.graph_id_for("delete-failure")
        ledger_path = (
            server.application.workspace.runtime_root
            / "default_collaboration_suite"
            / "initialized_projects.json"
        )

        def fail_clear(project_id):
            raise ActiveGraphSelectionError(
                "active_graph_selection_write_failed", "selection cleanup failed"
            )

        monkeypatch.setattr(server.active_graphs, "clear", fail_clear)
        status, failed = _json_request(
            "DELETE", f"{server.base_url}/v1/studio/projects/delete-failure"
        )

        assert status == 500
        assert failed["error"] == "active_graph_selection_write_failed"
        assert server.projects.get_project("delete-failure")["instance_id"] == lifecycle
        assert server.active_graphs.graph_id_for("delete-failure") == selection
        assert json.loads(ledger_path.read_text(encoding="utf-8"))["delete-failure"] == lifecycle



def test_project_delete_restores_definition_and_metadata_when_runtime_cleanup_fails(
    tmp_path, monkeypatch
):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    card.mkdir()
    (card / ".card_data.json").write_text("{}", encoding="utf-8")
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
    )

    with SessionRuntimeServer(
        runtime, static_root=styles, workspace=tmp_path / "workspace"
    ) as server:
        status, created = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/projects",
            {"id": "runtime-failure", "name": "Runtime failure"},
        )
        assert status == 201
        project = created["project"]
        selection = server.active_graphs.graph_id_for("runtime-failure")

        def fail_discard(project_id):
            raise OSError("runtime cleanup failed")

        monkeypatch.setattr(server.project_runtimes, "stage_discard", fail_discard)
        status, failed = _json_request(
            "DELETE", f"{server.base_url}/v1/studio/projects/runtime-failure"
        )

        assert status == 500
        assert failed["error"] == "project_runtime_delete_failed"
        assert server.projects.get_project("runtime-failure") == project
        assert server.active_graphs.graph_id_for("runtime-failure") == selection


def test_project_delete_rolls_back_when_quarantine_commit_rename_fails(
    tmp_path, monkeypatch
):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    card.mkdir()
    (card / ".card_data.json").write_text("{}", encoding="utf-8")
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3", card_folder=card, projection_root=styles
    )

    with SessionRuntimeServer(runtime, static_root=styles, workspace=tmp_path / "workspace") as server:
        status, created = _json_request(
            "POST", f"{server.base_url}/v1/studio/projects",
            {"id": "commit-rename-failure", "name": "Commit rename failure"},
        )
        assert status == 201
        project = created["project"]
        selection = server.active_graphs.graph_id_for(project["id"])
        ledger_path = (
            server.application.workspace.runtime_root
            / "default_collaboration_suite"
            / "initialized_projects.json"
        )
        context = server.project_runtimes.get(project["id"])
        context.runtime.set_opening_turn(
            {"user": "", "content": "rename rollback history", "is_opening": True, "index": 0},
            event_type="test.opening",
            event_payload={},
        )
        real_replace = Path.replace

        def fail_commit_rename(source, destination):
            if ".deleting-" in source.name and ".deleted-" in Path(destination).name:
                raise OSError("commit rename failed")
            return real_replace(source, destination)

        monkeypatch.setattr(Path, "replace", fail_commit_rename)
        status, failed = _json_request(
            "DELETE", f"{server.base_url}/v1/studio/projects/{project['id']}"
        )
        monkeypatch.undo()

        assert status == 500
        assert failed["error"] == "project_runtime_commit_failed"
        assert server.projects.get_project(project["id"]) == project
        assert server.active_graphs.graph_id_for(project["id"]) == selection
        assert json.loads(ledger_path.read_text(encoding="utf-8"))[project["id"]] == project["instance_id"]
        assert context.database_path.exists()
        assert context.card_folder.exists()
        assert server.project_runtimes.get(project["id"]).runtime.opening_turn()["content"] == "rename rollback history"

        restarted_store = ProjectRuntimeStore(
            server.projects,
            workspace=server.application.workspace,
            projection_root=styles,
            initial_runtime=server.runtime,
            initial_sessions=server.session_manager,
        )
        assert restarted_store.cleanup_quarantine() == ()
        assert restarted_store.get(project["id"]).runtime.opening_turn()["content"] == "rename rollback history"
        assert not list((tmp_path / "workspace").rglob("*.deleting-*"))
        assert not list((tmp_path / "workspace").rglob("*.deleted-*"))


def test_deleted_default_graph_stays_deleted_and_later_projects_remain_unselected(tmp_path):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    card.mkdir()
    (card / ".card_data.json").write_text("{}", encoding="utf-8")
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
    )

    with SessionRuntimeServer(
        runtime, static_root=styles, workspace=tmp_path / "workspace"
    ) as server:
        status, _ = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/projects",
            {"id": "before-delete", "name": "Before delete"},
        )
        assert status == 201
        assert server.active_graphs.graph_id_for("before-delete") == "default-two-round-review"

        status, deleted = _json_request(
            "DELETE",
            f"{server.base_url}/v1/studio/graphs/default-two-round-review",
        )
        assert status == 200
        assert deleted["deleted_id"] == "default-two-round-review"
        assert server.active_graphs.graph_id_for("before-delete") is None

        status, _ = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/projects",
            {"id": "after-delete", "name": "After delete"},
        )
        assert status == 201
        assert server.active_graphs.graph_id_for("after-delete") is None
        status, graphs = _json_request("GET", f"{server.base_url}/v1/studio/graphs")
        assert status == 200
        assert "default-two-round-review" not in {item["id"] for item in graphs["graphs"]}



def test_active_project_delete_failure_restores_original_runtime_surface(tmp_path, monkeypatch):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    card.mkdir()
    (card / ".card_data.json").write_text("{}", encoding="utf-8")
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3", card_folder=card, projection_root=styles
    )

    with SessionRuntimeServer(runtime, static_root=styles, workspace=tmp_path / "workspace") as server:
        for project_id in ("fallback", "delete-target"):
            status, _ = _json_request(
                "POST", f"{server.base_url}/v1/studio/projects",
                {"id": project_id, "name": project_id},
            )
            assert status == 201
        status, _ = _json_request(
            "POST", f"{server.base_url}/v1/session/project/switch",
            {"project_id": "delete-target"},
        )
        assert status == 200
        original_runtime = server.runtime
        original_session_id = server.runtime.session_id
        original_graph_id = server.runtime.execution_graph_id
        server.runtime.set_opening_turn(
            {"user": "", "content": "delete compensation history", "is_opening": True, "index": 0},
            event_type="test.opening",
            event_payload={},
        )
        selection = server.active_graphs.graph_id_for("delete-target")

        def fail_delete(project_id):
            raise ProjectLibraryError("project_delete_failed", "definition delete failed", status=500)

        monkeypatch.setattr(server.projects, "delete_project", fail_delete)
        status, failed = _json_request(
            "DELETE", f"{server.base_url}/v1/studio/projects/delete-target"
        )

        assert status == 500
        assert failed["error"] == "project_delete_failed"
        assert server.runtime is original_runtime
        assert server.runtime.project_id == "delete-target"
        assert server.runtime.session_id == original_session_id
        assert server.runtime.execution_graph_id == original_graph_id
        assert server.runtime.execution_plan_compiler is not None
        assert server.runtime.opening_turn()["content"] == "delete compensation history"
        assert server.projects.get_project("delete-target")["id"] == "delete-target"
        assert server.active_graphs.graph_id_for("delete-target") == selection


def test_project_http_lifecycle_matrix_initializes_copy_but_not_update(tmp_path):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    card.mkdir()
    (card / ".card_data.json").write_text("{}", encoding="utf-8")
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3", card_folder=card, projection_root=styles
    )

    with SessionRuntimeServer(runtime, static_root=styles, workspace=tmp_path / "workspace") as server:
        status, created = _json_request(
            "POST", f"{server.base_url}/v1/studio/projects",
            {"id": "matrix-source", "name": "Matrix source"},
        )
        assert status == 201
        server.active_graphs.clear("matrix-source")

        status, updated = _json_request(
            "PUT", f"{server.base_url}/v1/studio/projects/matrix-source",
            {"expected_revision": created["project"]["revision"], "name": "Updated"},
        )
        assert status == 200
        assert updated["project"]["name"] == "Updated"
        assert server.active_graphs.graph_id_for("matrix-source") is None

        status, copied = _json_request(
            "POST", f"{server.base_url}/v1/studio/projects/matrix-source/copy",
            {"new_id": "matrix-copy"},
        )
        assert status == 201
        assert copied["project"]["id"] == "matrix-copy"
        assert server.active_graphs.graph_id_for("matrix-copy") == "default-two-round-review"



def test_project_delete_restores_quarantined_runtime_when_state_write_fails(
    tmp_path, monkeypatch
):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    card.mkdir()
    (card / ".card_data.json").write_text("{}", encoding="utf-8")
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3", card_folder=card, projection_root=styles
    )

    with SessionRuntimeServer(runtime, static_root=styles, workspace=tmp_path / "workspace") as server:
        for project_id in ("stage-fallback", "stage-target"):
            status, _ = _json_request(
                "POST", f"{server.base_url}/v1/studio/projects",
                {"id": project_id, "name": project_id},
            )
            assert status == 201
        status, _ = _json_request(
            "POST", f"{server.base_url}/v1/session/project/switch",
            {"project_id": "stage-target"},
        )
        assert status == 200
        target_runtime = server.runtime
        target_runtime.set_opening_turn(
            {"user": "", "content": "history sentinel", "is_opening": True, "index": 0},
            event_type="test.opening",
            event_payload={},
        )
        database_path = Path(target_runtime.database_path)
        card_folder = Path(target_runtime.card_folder)
        real_write_state = server.project_runtimes._write_state
        calls = 0

        def fail_first_state_write(state):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("active state write failed")
            return real_write_state(state)

        monkeypatch.setattr(server.project_runtimes, "_write_state", fail_first_state_write)
        status, failed = _json_request(
            "DELETE", f"{server.base_url}/v1/studio/projects/stage-target"
        )

        assert status == 500
        assert failed["error"] == "project_runtime_delete_failed"
        assert server.runtime is target_runtime
        assert server.projects.get_project("stage-target")["id"] == "stage-target"
        assert database_path.is_file()
        assert card_folder.is_dir()
        assert server.runtime.opening_turn()["content"] == "history sentinel"


def test_empty_workspace_create_during_generation_defers_without_rewriting_runtime(
    tmp_path, monkeypatch
):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    card.mkdir()
    (card / ".card_data.json").write_text("{}", encoding="utf-8")
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3", card_folder=card, projection_root=styles
    )

    with SessionRuntimeServer(runtime, static_root=styles, workspace=tmp_path / "workspace") as server:
        status, _ = _json_request(
            "POST", f"{server.base_url}/v1/studio/projects",
            {"id": "deleted-active", "name": "Deleted active"},
        )
        assert status == 201
        status, _ = _json_request(
            "DELETE", f"{server.base_url}/v1/studio/projects/deleted-active"
        )
        assert status == 200
        stale_runtime = server.runtime
        project_id = stale_runtime.project_id
        monkeypatch.setattr(stale_runtime, "generation_active", lambda: True)

        status, _ = _json_request(
            "POST", f"{server.base_url}/v1/studio/projects",
            {"id": "deferred-after-delete", "name": "Deferred after delete"},
        )

        assert status == 201
        assert server.runtime is stale_runtime
        assert server.runtime.project_id == project_id

        monkeypatch.undo()
        server.submit_async("bind deferred", "bind-deferred-after-delete")
        assert server.runtime.project_id == "deferred-after-delete"


def test_all_task_entry_points_prepare_the_deferred_runtime(tmp_path, monkeypatch):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    card.mkdir()
    (card / ".card_data.json").write_text("{}", encoding="utf-8")
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3", card_folder=card, projection_root=styles
    )

    with SessionRuntimeServer(runtime, static_root=styles, workspace=tmp_path / "workspace") as server:
        prepared = []
        monkeypatch.setattr(server, "_prepare_runtime_for_next_task", lambda: prepared.append(True))
        monkeypatch.setattr(server, "_command_async", lambda *args, **kwargs: {"task_id": "queued"})

        server.submit_async("submit", "submit-key")
        server.reroll_async(1, "reroll-key")
        server.retry_graph_run_async("run-id", "retry-key")

        assert len(prepared) == 3


def test_failed_card_import_removes_only_its_created_embedded_worldbook(tmp_path):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    card.mkdir()
    (card / ".card_data.json").write_text("{}", encoding="utf-8")
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3", card_folder=card, projection_root=styles
    )

    with SessionRuntimeServer(runtime, static_root=styles, workspace=tmp_path / "workspace") as server:
        status, shared = _json_request(
            "POST", f"{server.base_url}/v1/studio/worldbooks",
            {"id": "shared-worldbook", "name": "Shared", "entries": []},
        )
        assert status == 201
        before_ids = {item["id"] for item in server.worldbooks.list_worldbooks()}
        selection_path = server.application.workspace.active_graph_selections_path
        selection_path.write_text("{", encoding="utf-8")

        status, failed = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/projects/import",
            {
                "id": "failed-import",
                "worldbook_ids": [shared["worldbook"]["id"]],
                "document": {
                    "spec": "chara_card_v2",
                    "data": {
                        "name": "Failed import",
                        "character_book": {
                            "name": "Embedded",
                            "entries": [{"id": 1, "keys": ["key"], "content": "entry"}],
                        },
                    },
                },
            },
        )

        assert status == 500
        assert failed["error"] == "active_graph_selection_unreadable"
        assert {item["id"] for item in server.worldbooks.list_worldbooks()} == before_ids
        assert server.projects.list_projects() == []


def test_project_delete_keeps_logical_success_when_quarantine_cleanup_is_partial(
    tmp_path, monkeypatch
):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    card.mkdir()
    (card / ".card_data.json").write_text("{}", encoding="utf-8")
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3", card_folder=card, projection_root=styles
    )

    with SessionRuntimeServer(runtime, static_root=styles, workspace=tmp_path / "workspace") as server:
        status, _ = _json_request(
            "POST", f"{server.base_url}/v1/studio/projects",
            {"id": "cleanup-partial", "name": "Cleanup partial"},
        )
        assert status == 201
        status, _ = _json_request(
            "POST", f"{server.base_url}/v1/session/project/switch",
            {"project_id": "cleanup-partial"},
        )
        assert status == 200
        server.runtime.set_opening_turn(
            {"user": "", "content": "quarantine history", "is_opening": True, "index": 0},
            event_type="test.opening",
            event_payload={},
        )
        real_remove = server.project_runtimes._remove_owned_path
        calls = 0

        def fail_second_cleanup(path, root):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("quarantine cleanup failed")
            return real_remove(path, root)

        monkeypatch.setattr(server.project_runtimes, "_remove_owned_path", fail_second_cleanup)
        status, deleted = _json_request(
            "DELETE", f"{server.base_url}/v1/studio/projects/cleanup-partial"
        )

        assert status == 200
        assert deleted["deleted_id"] == "cleanup-partial"
        assert deleted["warnings"][0]["code"] == "project_runtime_cleanup_pending"
        assert server.projects.list_projects() == []
        pending = list((tmp_path / "workspace" / "sessions" / "projects").glob(".*.deleted-*"))
        assert pending
        assert not (tmp_path / "workspace" / "sessions" / "projects" / "cleanup-partial.sqlite3").exists()

        monkeypatch.undo()
        restarted_store = ProjectRuntimeStore(
            server.projects,
            workspace=server.application.workspace,
            projection_root=styles,
            initial_runtime=runtime,
        )
        assert restarted_store.cleanup_quarantine() == ()
        assert not list((tmp_path / "workspace").rglob("*.deleted-*"))


def test_concurrent_project_delete_has_one_winner_and_does_not_revive_project(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    card.mkdir()
    (card / ".card_data.json").write_text("{}", encoding="utf-8")
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3", card_folder=card, projection_root=styles
    )

    with SessionRuntimeServer(runtime, static_root=styles, workspace=tmp_path / "workspace") as server:
        status, _ = _json_request(
            "POST", f"{server.base_url}/v1/studio/projects",
            {"id": "delete-race", "name": "Delete race"},
        )
        assert status == 201
        url = f"{server.base_url}/v1/studio/projects/delete-race"
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: _json_request("DELETE", url), range(2)))

        assert sorted(status for status, _ in results) == [200, 404]
        assert server.projects.list_projects() == []
        assert server.active_graphs.graph_id_for("delete-race") is None


def test_creation_endpoints_compensate_and_redact_unexpected_configuration_failures(
    tmp_path, monkeypatch
):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    card.mkdir()
    (card / ".card_data.json").write_text("{}", encoding="utf-8")
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3", card_folder=card, projection_root=styles
    )

    with SessionRuntimeServer(runtime, static_root=styles, workspace=tmp_path / "workspace") as server:
        status, _ = _json_request(
            "POST", f"{server.base_url}/v1/studio/projects",
            {"id": "creation-base", "name": "Creation base"},
        )
        assert status == 201
        before_worldbooks = {item["id"] for item in server.worldbooks.list_worldbooks()}
        real_configure = server._configure_runtime_studio_graph
        cases = [
            (
                "failed-create",
                f"{server.base_url}/v1/studio/projects",
                {"id": "failed-create", "name": "Failed create"},
            ),
            (
                "failed-copy",
                f"{server.base_url}/v1/studio/projects/creation-base/copy",
                {"new_id": "failed-copy"},
            ),
            (
                "failed-import",
                f"{server.base_url}/v1/studio/projects/import",
                {
                    "id": "failed-import",
                    "document": {
                        "spec": "chara_card_v2",
                        "data": {
                            "name": "Failed import",
                            "character_book": {
                                "name": "Temporary lore",
                                "entries": [{
                                    "id": 1,
                                    "keys": ["temporary"],
                                    "content": "Temporary content",
                                }],
                            },
                        },
                    },
                },
            ),
        ]
        for project_id, url, body in cases:
            failed_once = False

            def fail_once():
                nonlocal failed_once
                if not failed_once:
                    failed_once = True
                    raise ValueError("secret configure detail")
                return real_configure()

            monkeypatch.setattr(server, "_configure_runtime_studio_graph", fail_once)
            status, failed = _json_request("POST", url, body)
            monkeypatch.setattr(server, "_configure_runtime_studio_graph", real_configure)

            assert status == 500
            assert failed["error"] == "project_runtime_configuration_failed"
            assert "secret configure detail" not in json.dumps(failed)
            assert [item["id"] for item in server.projects.list_projects()] == ["creation-base"]
            assert server.active_graphs.graph_id_for(project_id) is None
            ledger = json.loads((
                server.application.workspace.runtime_root
                / "default_collaboration_suite"
                / "initialized_projects.json"
            ).read_text(encoding="utf-8"))
            assert project_id not in ledger
            assert {item["id"] for item in server.worldbooks.list_worldbooks()} == before_worldbooks


def test_quarantine_cleanup_never_consumes_a_staged_rollback_token(tmp_path):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    card.mkdir()
    (card / ".card_data.json").write_text("{}", encoding="utf-8")
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3", card_folder=card, projection_root=styles
    )

    with SessionRuntimeServer(runtime, static_root=styles, workspace=tmp_path / "workspace") as server:
        status, _ = _json_request(
            "POST", f"{server.base_url}/v1/studio/projects",
            {"id": "staged-runtime", "name": "Staged runtime"},
        )
        assert status == 201
        status, _ = _json_request(
            "POST", f"{server.base_url}/v1/session/project/switch",
            {"project_id": "staged-runtime"},
        )
        assert status == 200
        server.runtime.set_opening_turn(
            {"user": "", "content": "history survives GC", "is_opening": True, "index": 0},
            event_type="test.opening",
            event_payload={},
        )
        context = server.project_runtimes.get("staged-runtime")
        database_path = context.database_path
        card_folder = context.card_folder

        discard = server.project_runtimes.stage_discard("staged-runtime")
        assert not database_path.exists()
        assert not card_folder.exists()

        assert server.project_runtimes.cleanup_quarantine() == ()
        server.project_runtimes.rollback_discard(discard)

        assert database_path.exists()
        assert card_folder.exists()
        restored = server.project_runtimes.get("staged-runtime")
        assert restored.runtime.opening_turn()["content"] == "history survives GC"
