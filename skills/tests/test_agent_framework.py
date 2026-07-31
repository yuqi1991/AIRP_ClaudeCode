from __future__ import annotations

import sys
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILLS))

from airp.host.graph_turn_commit import GraphTurnCommitExecutor  # noqa: E402
from engine.agent_framework import AgentFrameworkExecutor  # noqa: E402
from engine.graph_runtime import AgentArtifact, ExecutionPlanCompiler, GraphRuntime, NodeResult  # noqa: E402


def _agent() -> dict:
    return {"agent_id": "writer", "name": "Writer", "instruction": "Write"}


def _plan(output: str) -> object:
    plan = ExecutionPlanCompiler().compile(
        project={"id": "project"},
        graph={
            "id": "graph",
            "nodes": [{"node_id": "writer", "agent_id": "writer"}],
            "output_node_id": "writer",
        },
        agents={"writer": _agent()},
        player_input="hello",
    )

    class Runner:
        def run(self, node, input_artifact):
            del node, input_artifact
            return NodeResult.succeeded(AgentArtifact.text(output))

    return plan, GraphRuntime(Runner())


def test_framework_executor_returns_opaque_graph_result_without_parsing_content():
    plan, runtime = _plan("<content>story</content>")

    result = AgentFrameworkExecutor(runtime, plan).run("hello")

    assert result.ok
    assert result.output_artifact.content == "<content>story</content>"
    assert result.output_artifact.kind == "text"


def test_host_commit_keeps_final_artifact_content_opaque():
    plan, runtime = _plan("<content>story</content><summary>brief</summary>")

    draft = GraphTurnCommitExecutor(AgentFrameworkExecutor(runtime, plan)).run("hello")

    assert draft.content == "<content>story</content><summary>brief</summary>"
    assert draft.summary == ""
