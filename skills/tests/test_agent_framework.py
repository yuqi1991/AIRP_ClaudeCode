from __future__ import annotations

import sys
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILLS))

from engine.agent_framework import AdaptedGraphExecutor, AgentFrameworkExecutor  # noqa: E402
from engine.graph_runtime import AgentArtifact, ExecutionPlanCompiler, GraphRuntime, NodeResult  # noqa: E402
from engine.rp_turn_adapter import RPTurnAdapter  # noqa: E402


def _agent() -> dict:
    return {"agent_id": "writer", "name": "Writer", "instruction": "Write"}


def _plan(output: str) -> object:
    plan = ExecutionPlanCompiler().compile(
        project={"id": "project", "graph_id": "graph"},
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


def test_rp_adapter_keeps_tag_rules_outside_framework():
    plan, runtime = _plan("<content>story</content><summary>brief</summary>")

    draft = AdaptedGraphExecutor(
        AgentFrameworkExecutor(runtime, plan),
        RPTurnAdapter(),
    ).run("hello")

    assert draft.content == "story"
    assert draft.summary == "brief"
