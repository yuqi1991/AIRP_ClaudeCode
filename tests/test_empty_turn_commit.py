from __future__ import annotations

import json

from airp.engine.graph_runtime import AgentArtifact, ExecutionPlanCompiler, GraphRuntime, NodeResult
from airp.host.rp.session_runtime import SessionTurnRuntime


class _Store:
    def __init__(self, values):
        self._values = values

    def get_project(self, item_id):
        return self._values[item_id]

    def get_graph(self, item_id):
        return self._values[item_id]

    def get_agent(self, item_id):
        return self._values[item_id]


class _OutputRunner:
    def __init__(self, outputs):
        self._outputs = iter(outputs)

    def run(self, node, input_artifact):
        del node, input_artifact
        return NodeResult.succeeded(AgentArtifact.text(next(self._outputs)))


def _runtime(tmp_path, outputs):
    card = tmp_path / "card"
    projection = tmp_path / "projection"
    (card / "memory").mkdir(parents=True, exist_ok=True)
    projection.mkdir(exist_ok=True)
    for path, content in (
        (card / ".initvar.json", "{}"),
        (card / "chat_log.json", "[]"),
        (card / ".card_data.json", '{"name":"Test"}'),
    ):
        if not path.exists():
            path.write_text(content, encoding="utf-8")
    compiler = ExecutionPlanCompiler(
        project_store=_Store({"project": {"id": "project", "worldbook_ids": []}}),
        graph_store=_Store(
            {
                "writing": {
                    "id": "writing",
                    "nodes": [{"node_id": "writer", "agent_id": "writer"}],
                    "output_node_id": "writer",
                }
            }
        ),
        agent_store=_Store(
            {
                "writer": {
                    "agent_id": "writer",
                    "name": "Writer",
                    "instruction": "Write",
                    "provider_profile_id": "provider",
                    "model_id": "model",
                    "prompt": [{"role": "system", "content": "Write"}],
                }
            }
        ),
    )
    return SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=projection,
        execution_plan_compiler=compiler,
        graph_runtime=GraphRuntime(_OutputRunner(outputs)),
        project_id="project",
        execution_graph_id="writing",
        bootstrap_legacy_history=False,
    )


def test_empty_graph_output_commits_and_survives_projection_and_restart(tmp_path):
    runtime = _runtime(tmp_path, [""])

    result = runtime.submit("Continue", "empty-output")

    assert result.status == "succeeded"
    assert result.revision == 1
    assert runtime.active_lineage_turns() == [
        {"revision": 1, "user": "Continue", "assistant": ""}
    ]
    assert json.loads((tmp_path / "card" / "chat_log.json").read_text(encoding="utf-8"))[0][
        "ai"
    ] == ""

    restarted = _runtime(tmp_path, [])
    restarted.resume_projection()

    assert restarted.active_revision() == 1
    assert restarted.active_lineage_turns() == [
        {"revision": 1, "user": "Continue", "assistant": ""}
    ]
    assert json.loads((tmp_path / "card" / "chat_log.json").read_text(encoding="utf-8"))[0][
        "ai"
    ] == ""


def test_whitespace_graph_output_is_preserved_in_projection_and_next_context(tmp_path):
    whitespace = " \n\t "
    runtime = _runtime(tmp_path, [whitespace, "next"])

    first = runtime.submit("First", "whitespace-output")
    second = runtime.submit("Second", "after-whitespace")

    assert first.status == "succeeded"
    assert second.status == "succeeded"
    assert (first.revision, second.revision) == (1, 2)
    assert runtime.active_lineage_turns() == [
        {"revision": 1, "user": "First", "assistant": whitespace},
        {"revision": 2, "user": "Second", "assistant": "next"},
    ]
    projected = json.loads((tmp_path / "card" / "chat_log.json").read_text(encoding="utf-8"))
    assert projected[0]["ai"] == whitespace
    graph_run_id = runtime.graph_run_id_for_task(second.task_id)
    recent_turns = runtime.graph_run_detail(graph_run_id)["nodes"][0]["tool_snapshot"]["recent_turns"]
    assert recent_turns == [
        {"revision": 1, "user": "First", "assistant": whitespace}
    ]


def test_empty_revision_supports_reroll_rollback_and_new_branch(tmp_path):
    runtime = _runtime(tmp_path, ["", "replacement", "after rollback"])

    original = runtime.submit("Original", "empty-original")
    rerolled = runtime.reroll(original.revision, "reroll-empty")

    assert rerolled.status == "succeeded"
    assert rerolled.revision == 2
    assert runtime.commit_lineage(rerolled.revision)["parent_revision"] == 0
    assert runtime.active_lineage_turns() == [
        {"revision": 2, "user": "Original", "assistant": "replacement"}
    ]

    rolled_back = runtime.rollback(original.revision, "rollback-to-empty")

    assert rolled_back.status == "rolled_back"
    assert runtime.active_lineage_turns() == [
        {"revision": 1, "user": "Original", "assistant": ""}
    ]
    projected = json.loads((tmp_path / "card" / "chat_log.json").read_text(encoding="utf-8"))
    assert projected == [{"index": 0, "user": "Original", "ai": "", "summary": ""}]

    branched = runtime.submit("Continue", "branch-after-empty")

    assert branched.status == "succeeded"
    assert branched.revision == 3
    assert runtime.commit_lineage(branched.revision)["parent_revision"] == original.revision
    assert runtime.active_lineage_turns() == [
        {"revision": 1, "user": "Original", "assistant": ""},
        {"revision": 3, "user": "Continue", "assistant": "after rollback"},
    ]
