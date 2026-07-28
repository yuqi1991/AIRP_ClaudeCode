from __future__ import annotations

import copy
import json
import shutil
import sys
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest


SKILLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILLS))

from engine.graph_definitions import GraphDefinitionError, GraphDefinitionStore  # noqa: E402
from engine.graph_runtime import (  # noqa: E402
    AgentArtifact,
    ExecutionPlanCompiler,
    GraphRuntime,
    NodeResult,
)
from engine.node_runner import ProviderNodeRunner  # noqa: E402
from engine.provider import FakeProvider  # noqa: E402
from engine.runtime import FakeNarrativeExecutor, SessionTurnRuntime  # noqa: E402
from runtime_server import SessionRuntimeServer  # noqa: E402


def _agent(agent_id: str, *, instruction: str = "Write") -> dict:
    return {
        "agent_id": agent_id,
        "name": agent_id.title(),
        "instruction": instruction,
        "prompt_preset_id": "default",
        "provider_profile_id": "provider",
        "model_id": "model",
        "generation": {"temperature": 0.2},
        "advanced": {"response_format": {"type": "text"}},
        "tool_allowlist": ["get_recent_memory"],
        "prompt": [{"role": "system", "content": instruction}],
    }


def _graph() -> dict:
    return {
        "id": "writing",
        "name": "Writing",
        "nodes": [
            {"node_id": "draft", "agent_id": "writer", "order": 0},
            {
                "node_id": "review",
                "agent_id": "writer",
                "label": "Review",
                "order": 1,
                "enabled": False,
            },
        ],
        "output_node_id": "draft",
    }


def test_graph_store_allows_repeated_agent_references_and_explicit_output(tmp_path):
    store = GraphDefinitionStore(tmp_path)
    created = store.create_graph(_graph())

    assert [node["node_id"] for node in created["nodes"]] == ["draft", "review"]
    assert [node["agent_id"] for node in created["nodes"]] == ["writer", "writer"]
    assert created["nodes"][1]["label"] == "Review"
    assert created["output_node_id"] == "draft"

    updated = store.update_graph(
        "writing",
        {
            "nodes": [
                {"node_id": "review", "agent_id": "writer", "enabled": True},
                {"node_id": "draft", "agent_id": "writer", "enabled": True},
            ],
            "output_node_id": "draft",
        },
    )
    assert [node["node_id"] for node in updated["nodes"]] == ["review", "draft"]
    assert updated["output_node_id"] == "draft"


def test_graph_store_rejects_output_that_is_not_final_enabled_node(tmp_path):
    store = GraphDefinitionStore(tmp_path)
    with pytest.raises(GraphDefinitionError, match="output"):
        store.create_graph(
            {
                **_graph(),
                "nodes": [
                    {"node_id": "draft", "agent_id": "writer"},
                    {"node_id": "review", "agent_id": "writer"},
                ],
                "output_node_id": "draft",
            }
        )


class _Runner:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []

    def run(self, node, input_artifact):
        self.calls.append((node.node_id, input_artifact.content))
        return self.outputs.pop(0)


def test_execution_plan_freezes_project_agents_graph_prompt_tools_and_worldbook():
    agents = {"writer": _agent("writer")}
    graph = _graph()
    project = {
        "id": "project",
        "name": "Project",
        "graph_id": "writing",
        "worldbook_ids": ["book"],
        "description": "Original card",
    }
    worldbooks = [{"id": "book", "name": "Book", "entries": [{"title": "Harbor", "content": "Old"}]}]
    plan = ExecutionPlanCompiler().compile(
        project=project,
        graph=graph,
        agents=agents,
        worldbooks=worldbooks,
        player_input="Enter the harbor",
    )

    agents["writer"]["instruction"] = "Changed"
    graph["nodes"][0]["enabled"] = False
    project["description"] = "Changed card"
    worldbooks[0]["entries"][0]["content"] = "Changed"

    assert plan.graph.nodes[0].agent.agent_id == "writer"
    assert plan.graph.nodes[0].prompt[0]["content"] == "Write"
    assert plan.project["description"] == "Original card"
    assert plan.worldbooks[0]["entries"][0]["content"] == "Old"
    assert plan.player_input == "Enter the harbor"


def test_graph_runtime_hands_artifacts_in_order_and_returns_output_artifact():
    plan = ExecutionPlanCompiler().compile(
        project={"id": "project", "graph_id": "writing"},
        graph={
            "id": "writing",
            "nodes": [
                {"node_id": "first", "agent_id": "writer"},
                {"node_id": "second", "agent_id": "writer"},
            ],
            "output_node_id": "second",
        },
        agents={"writer": _agent("writer")},
        worldbooks=[],
        player_input="hello",
    )
    runner = _Runner(
        [
            NodeResult.succeeded(AgentArtifact.text("draft")),
            NodeResult.succeeded(AgentArtifact.text("final")),
        ]
    )

    result = GraphRuntime(runner).run(plan)

    assert result.status == "succeeded"
    assert result.output_artifact.content == "final"
    assert runner.calls == [("first", "hello"), ("second", "draft")]


def test_graph_runtime_fails_fast_and_does_not_run_following_nodes():
    plan = ExecutionPlanCompiler().compile(
        project={"id": "project", "graph_id": "writing"},
        graph={
            "id": "writing",
            "nodes": [
                {"node_id": "first", "agent_id": "writer"},
                {"node_id": "second", "agent_id": "writer"},
                {"node_id": "third", "agent_id": "writer"},
            ],
            "output_node_id": "third",
        },
        agents={"writer": _agent("writer")},
        worldbooks=[],
        player_input="hello",
    )
    runner = _Runner(
        [
            NodeResult.succeeded(AgentArtifact.text("draft")),
            NodeResult.failed("provider unavailable"),
        ]
    )

    result = GraphRuntime(runner).run(plan)

    assert result.status == "failed"
    assert result.failed_node_id == "second"
    assert result.output_artifact is None
    assert runner.calls == [("first", "hello"), ("second", "draft")]


def test_provider_node_runner_maps_provider_output_to_artifact_boundary():
    plan = ExecutionPlanCompiler().compile(
        project={"id": "project", "graph_id": "writing"},
        graph={
            "id": "writing",
            "nodes": [{"node_id": "writer-node", "agent_id": "writer"}],
            "output_node_id": "writer-node",
        },
        agents={"writer": _agent("writer")},
        worldbooks=[],
        player_input="hello",
    )
    provider = FakeProvider(
        [
            {"type": "text", "text": "model output"},
            {"type": "final"},
        ],
        model="writer-model",
    )

    result = ProviderNodeRunner(lambda _node: provider).run(
        plan.graph.nodes[0], AgentArtifact.input("hello")
    )

    assert result.status == "succeeded"
    assert result.primary_artifact.content == "model output"
    assert provider.requests[0].model == "model"
    assert provider.requests[0].messages[-1]["content"] == "hello"


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


def test_graph_http_api_can_create_and_reorder_agent_nodes(tmp_path):
    styles = tmp_path / "styles"
    styles.mkdir()
    shutil.copy(SKILLS / "styles" / "studio.html", styles / "studio.html")
    card = tmp_path / "card"
    (card / "memory").mkdir(parents=True)
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    (card / ".card_data.json").write_text('{"name":"Test"}', encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
        executor=FakeNarrativeExecutor("<content>ok</content>"),
        bootstrap_legacy_history=False,
    )
    with SessionRuntimeServer(runtime, static_root=styles) as server:
        status, _ = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/agents",
            {"agent_id": "writer", "name": "Writer", "instruction": "Write"},
        )
        assert status == 201
        status, created = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/graphs",
            {
                "id": "writing",
                "name": "Writing",
                "nodes": [
                    {"node_id": "draft", "agent_id": "writer"},
                    {"node_id": "review", "agent_id": "writer", "label": "Review", "enabled": False},
                ],
                "output_node_id": "draft",
            },
        )
        assert status == 201
        assert created["graph"]["nodes"][1]["agent_id"] == "writer"
        status, updated = _json_request(
            "PUT",
            f"{server.base_url}/v1/studio/graphs/writing",
            {
                "nodes": [
                    {"node_id": "review", "agent_id": "writer", "enabled": True},
                    {"node_id": "draft", "agent_id": "writer", "enabled": True},
                ],
                "output_node_id": "draft",
            },
        )
        assert status == 200
        assert [node["node_id"] for node in updated["graph"]["nodes"]] == ["review", "draft"]


class _MemoryStore:
    def __init__(self, values):
        self.values = values

    def get_project(self, item_id):
        return copy.deepcopy(self.values[item_id])

    def get_graph(self, item_id):
        return copy.deepcopy(self.values[item_id])

    def get_agent(self, item_id):
        return copy.deepcopy(self.values[item_id])


class _CommitRunner:
    def run(self, node, input_artifact):
        del input_artifact
        return NodeResult.succeeded(
            AgentArtifact.text(
                "<content>final artifact</content><summary>done</summary>",
            )
        )


class _FailingCommitRunner:
    def __init__(self):
        self.calls = []

    def run(self, node, input_artifact):
        self.calls.append(node.node_id)
        if node.node_id == "failed-node":
            return NodeResult.failed("provider unavailable")
        return NodeResult.succeeded(AgentArtifact.text("draft"))


def test_graph_output_artifact_uses_existing_runtime_draft_commit_path(tmp_path):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    (card / "memory").mkdir(parents=True)
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    (card / ".card_data.json").write_text('{"name":"Test"}', encoding="utf-8")
    project = {"id": "project", "graph_id": "writing", "worldbook_ids": []}
    graph = {
        "id": "writing",
        "nodes": [{"node_id": "writer-node", "agent_id": "writer"}],
        "output_node_id": "writer-node",
    }
    agent = _agent("writer")
    compiler = ExecutionPlanCompiler(
        project_store=_MemoryStore({"project": project}),
        graph_store=_MemoryStore({"writing": graph}),
        agent_store=_MemoryStore({"writer": agent}),
    )
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
        executor=FakeNarrativeExecutor("unused"),
        execution_plan_compiler=compiler,
        graph_runtime=GraphRuntime(_CommitRunner()),
        project_id="project",
        bootstrap_legacy_history=False,
    )

    result = runtime.submit("Enter", "graph-submit")

    assert result.status == "succeeded"
    assert result.commit_id
    log = json.loads((card / "chat_log.json").read_text(encoding="utf-8"))
    assert "final artifact" in log[0]["ai"]


def test_graph_node_failure_aborts_session_task_without_story_commit(tmp_path):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    (card / "memory").mkdir(parents=True)
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    (card / ".card_data.json").write_text('{"name":"Test"}', encoding="utf-8")
    project = {"id": "project", "graph_id": "writing", "worldbook_ids": []}
    graph = {
        "id": "writing",
        "nodes": [
            {"node_id": "first-node", "agent_id": "writer"},
            {"node_id": "failed-node", "agent_id": "writer"},
            {"node_id": "never-run", "agent_id": "writer"},
        ],
        "output_node_id": "never-run",
    }
    agent = _agent("writer")
    compiler = ExecutionPlanCompiler(
        project_store=_MemoryStore({"project": project}),
        graph_store=_MemoryStore({"writing": graph}),
        agent_store=_MemoryStore({"writer": agent}),
    )
    runner = _FailingCommitRunner()
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
        executor=FakeNarrativeExecutor("unused"),
        execution_plan_compiler=compiler,
        graph_runtime=GraphRuntime(runner),
        project_id="project",
        bootstrap_legacy_history=False,
    )

    result = runtime.submit("Enter", "graph-failure")

    assert result.status == "failed_terminal"
    assert result.commit_id is None
    assert runner.calls == ["first-node", "failed-node"]
    assert json.loads((card / "chat_log.json").read_text(encoding="utf-8")) == []
