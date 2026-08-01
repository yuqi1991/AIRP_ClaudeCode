from __future__ import annotations

import copy
import json
import shutil
import sys
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]

from airp.engine.graph_definitions import GraphDefinitionError, GraphDefinitionStore  # noqa: E402
from airp.engine.capabilities import CapabilityDefinition, CapabilityRegistry  # noqa: E402
from airp.engine.graph_runtime import (  # noqa: E402
    AgentArtifact,
    ExecutionPlanCompiler,
    GraphRuntime,
    NodeExecutionContext,
    NodeResult,
)
from airp.engine.node_runner import ProviderNodeRunner  # noqa: E402
from airp.engine.provider import AbortSignal, FakeProvider, ProviderDelta, ProviderResult  # noqa: E402
from airp.host.rp.session_runtime import SessionTurnRuntime  # noqa: E402
from airp.server import SessionRuntimeServer  # noqa: E402


def _agent(agent_id: str, *, instruction: str = "Write") -> dict:
    return {
        "agent_id": agent_id,
        "name": agent_id.title(),
        "instruction": instruction,
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
        project={"id": "project"},
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
        project={"id": "project"},
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
        project={"id": "project"},
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


def test_provider_node_runner_uses_run_execution_context_tool_handler():
    plan = ExecutionPlanCompiler().compile(
        project={"id": "project"},
        graph={
            "id": "writing",
            "nodes": [{"node_id": "writer-node", "agent_id": "writer"}],
            "output_node_id": "writer-node",
        },
        agents={"writer": {**_agent("writer"), "tool_allowlist": ["get_recent_memory"]}},
        worldbooks=[],
        player_input="hello",
    )
    provider = FakeProvider(
        [
            [{"type": "tool_call", "id": "call-1", "name": "get_recent_memory", "args": {"max_chars": 20}}],
            [{"type": "text", "text": "tool-aware output"}, {"type": "final"}],
        ],
        model="writer-model",
    )
    calls = []

    def handle(_node, name, args):
        calls.append((name, args))
        return {"memory": "frozen memory"}

    result = ProviderNodeRunner(lambda _node: provider).run(
        plan.graph.nodes[0],
        AgentArtifact.input("hello"),
        execution_context=NodeExecutionContext(tool_handler=handle),
    )

    assert result.ok
    assert result.primary_artifact.content == "tool-aware output"
    assert calls == [("get_recent_memory", {"max_chars": 20})]
    assert provider.requests[1].messages[-1]["role"] == "tool"
    assert provider.requests[1].messages[-1]["content"] == "{\"memory\": \"frozen memory\"}"


def test_provider_node_runner_uses_explicit_capability_registry_and_skill_catalog():
    plan = ExecutionPlanCompiler().compile(
        project={"id": "project"},
        graph={
            "id": "writing",
            "nodes": [{"node_id": "writer-node", "agent_id": "writer"}],
            "output_node_id": "writer-node",
        },
        agents={
            "writer": {
                **_agent("writer"),
                "tool_allowlist": ["lookup_fact"],
                "instruction": "Available skills: {{skills}}",
                "prompt": [{"role": "system", "content": "Available skills: {{skills}}"}],
            }
        },
        worldbooks=[],
        player_input="hello",
    )
    provider = FakeProvider(
        [
            [{"type": "tool_call", "id": "call-1", "name": "lookup_fact", "args": {"key": "weather"}}],
            [{"type": "text", "text": "capability-aware output"}, {"type": "final"}],
        ],
        model="writer-model",
    )
    registry = CapabilityRegistry()
    registry.register(
        CapabilityDefinition(
            "lookup_fact",
            "Look up one host fact",
            {"type": "object", "properties": {"key": {"type": "string"}}},
        ),
        lambda args: {"key": args["key"], "value": "sunny"},
    )

    result = ProviderNodeRunner(lambda _node: provider).run(
        plan.graph.nodes[0],
        AgentArtifact.input("hello"),
        execution_context=NodeExecutionContext(
            tool_registry=registry,
            skill_catalog={"worldbook": [{"name": "harbor", "description": "Harbor facts"}]},
        ),
    )

    assert result.ok
    assert result.primary_artifact.content == "capability-aware output"
    assert provider.requests[0].tools[0]["function"]["name"] == "lookup_fact"
    assert "harbor" in provider.requests[0].messages[0]["content"]
    assert provider.requests[1].messages[-1]["content"] == '{"key": "weather", "value": "sunny"}'


def test_session_graph_binds_host_tool_registry_for_worldbook_and_memory_tools(tmp_path):
    card = tmp_path / "card"
    (card / "memory").mkdir(parents=True)
    (card / "memory" / "project.md").write_text("frozen memory", encoding="utf-8")
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    (card / ".card_data.json").write_text("{\"name\": \"Test\"}", encoding="utf-8")
    styles = tmp_path / "styles"
    provider = FakeProvider(
        [
            [{"type": "tool_call", "id": "call-1", "name": "load_worldbook_entry", "args": {"title": "Harbor"}}],
            [{"type": "tool_call", "id": "call-2", "name": "get_recent_memory", "args": {"max_chars": 20}}],
            [{"type": "text", "text": "tool-aware output"}, {"type": "final"}],
        ],
        model="writer-model",
    )

    class _WorldbookStore:
        def get_worldbook(self, book_id):
            return {
                "id": book_id,
                "name": "Harbor Book",
                "entries": [
                    {
                        "id": "harbor-entry",
                        "title": "Harbor",
                        "usage": "harbor setting",
                        "content": "The harbor is closed at dawn.",
                        "enabled": True,
                        "order": 0,
                    }
                ],
            }

        def effective_worldbooks(self, project_id):
            del project_id
            return [self.get_worldbook("book")]

    compiler = ExecutionPlanCompiler(
        project_store=_MemoryStore({"project": {"id": "project", "worldbook_ids": ["book"]}}),
        graph_store=_MemoryStore({
            "writing": {
                "id": "writing",
                "nodes": [{"node_id": "writer-node", "agent_id": "writer"}],
                "output_node_id": "writer-node",
            }
        }),
        agent_store=_MemoryStore({
            "writer": {
                **_agent("writer"),
                "tool_allowlist": ["load_worldbook_entry", "get_recent_memory"],
            }
        }),
        worldbook_store=_WorldbookStore(),
    )
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
        execution_plan_compiler=compiler,
        graph_runtime=GraphRuntime(ProviderNodeRunner(lambda _node: provider)),
        project_id="project",
        execution_graph_id="writing",
        bootstrap_legacy_history=False,
    )

    result = runtime.submit("hello", "tool-submit")

    assert result.status == "succeeded"
    assert provider.call_count == 3
    assert {tool["name"] for tool in provider.requests[0].tools} == {
        "load_worldbook_entry",
        "get_recent_memory",
    }
    worldbook_schema = next(
        tool["parameters"]
        for tool in provider.requests[0].tools
        if tool["name"] == "load_worldbook_entry"
    )
    assert worldbook_schema["type"] == "object"
    assert worldbook_schema["required"] == ["title"]
    assert provider.requests[1].messages[-1]["role"] == "tool"
    assert any(
        "closed at dawn" in message.get("content", "")
        for message in provider.requests[1].messages
        if message.get("role") == "tool"
    )
    assert provider.requests[2].messages[-1]["role"] == "tool"
    assert "frozen memory" in provider.requests[2].messages[-1]["content"]
    assert json.loads((card / "chat_log.json").read_text(encoding="utf-8"))[-1]["ai"] == "tool-aware output"


def test_provider_node_runner_forwards_agent_generation_and_advanced_parameters():
    plan = ExecutionPlanCompiler().compile(
        project={"id": "project"},
        graph={
            "id": "writing",
            "nodes": [{"node_id": "writer-node", "agent_id": "writer"}],
            "output_node_id": "writer-node",
        },
        agents={
            "writer": {
                **_agent("writer"),
                "generation": {"temperature": 0.7, "max_output_tokens": 321},
                "advanced": {"response_format": {"type": "json_object"}},
            }
        },
        worldbooks=[],
        player_input="hello",
    )
    provider = FakeProvider(
        [{"type": "text", "text": "model output"}, {"type": "final"}],
        model="writer-model",
    )

    result = ProviderNodeRunner(lambda _node: provider).run(
        plan.graph.nodes[0], AgentArtifact.input("hello")
    )

    assert result.ok
    assert provider.requests[0].parameters == {
        "temperature": 0.7,
        "max_output_tokens": 321,
        "response_format": {"type": "json_object"},
    }


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
    shutil.copy(REPO_ROOT / "src" / "airp" / "web" / "studio.html", styles / "studio.html")
    card = tmp_path / "card"
    (card / "memory").mkdir(parents=True)
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    (card / ".card_data.json").write_text('{"name":"Test"}', encoding="utf-8")
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
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


class _RetryRunner:
    def __init__(self):
        self.calls = []

    def run(self, node, input_artifact):
        self.calls.append((node.node_id, input_artifact.content))
        if len(self.calls) == 1:
            return NodeResult.failed("provider unavailable")
        return NodeResult.succeeded(
            AgentArtifact.text("<content>retry output</content><summary>retried</summary>")
        )


class _ReplayRunner:
    def __init__(self):
        self.calls = []

    def run(self, node, input_artifact):
        self.calls.append((node.node_id, input_artifact.content))
        if node.node_id == "first":
            content = "first output"
        elif node.node_id == "second" and len(self.calls) == 2:
            content = "old second output"
        elif node.node_id == "third":
            content = "<content>final output</content><summary>done</summary>"
        else:
            content = "replayed second output"
        return NodeResult.succeeded(AgentArtifact.text(content))


class _RetentionRunner:
    def __init__(self):
        self.count = 0

    def run(self, node, input_artifact):
        del node, input_artifact
        self.count += 1
        return NodeResult.succeeded(
            AgentArtifact.text(
                f"<content>retention output {self.count}</content><summary>done</summary>"
            )
        )


class _BlockingGraphRunner:
    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()

    def run(self, node, input_artifact):
        del node, input_artifact
        self.started.set()
        assert self.release.wait(timeout=5)
        return NodeResult.succeeded(AgentArtifact.text("<content>finished</content>"))


def test_graph_output_artifact_uses_existing_runtime_draft_commit_path(tmp_path):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    (card / "memory").mkdir(parents=True)
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    (card / ".card_data.json").write_text('{"name":"Test"}', encoding="utf-8")
    project = {"id": "project", "worldbook_ids": []}
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
        execution_plan_compiler=compiler,
        graph_runtime=GraphRuntime(_CommitRunner()),
        project_id="project",
        execution_graph_id="writing",
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
    project = {"id": "project", "worldbook_ids": []}
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
        execution_plan_compiler=compiler,
        graph_runtime=GraphRuntime(runner),
        project_id="project",
        execution_graph_id="writing",
        bootstrap_legacy_history=False,
    )

    result = runtime.submit("Enter", "graph-failure")

    assert result.status == "failed_terminal"
    assert result.commit_id is None
    assert runner.calls == ["first-node", "failed-node"]
    assert json.loads((card / "chat_log.json").read_text(encoding="utf-8")) == []


def test_graph_retry_recompiles_current_definitions_and_links_failed_run(tmp_path):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    (card / "memory").mkdir(parents=True)
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    (card / ".card_data.json").write_text('{"name":"Test"}', encoding="utf-8")
    project = {"id": "project", "worldbook_ids": []}
    graph = {
        "id": "writing",
        "nodes": [{"node_id": "writer-node", "agent_id": "writer"}],
        "output_node_id": "writer-node",
    }
    agent = _agent("writer")
    stores = {
        "project": _MemoryStore({"project": project}),
        "graph": _MemoryStore({"writing": graph}),
        "agent": _MemoryStore({"writer": agent}),
    }
    compiler = ExecutionPlanCompiler(
        project_store=stores["project"],
        graph_store=stores["graph"],
        agent_store=stores["agent"],
    )
    runner = _RetryRunner()
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
        execution_plan_compiler=compiler,
        graph_runtime=GraphRuntime(runner),
        project_id="project",
        execution_graph_id="writing",
        bootstrap_legacy_history=False,
    )

    failed = runtime.submit("Enter", "graph-failure")
    old_run = runtime.graph_runs_snapshot()["most_recent"]
    assert failed.status == "failed_terminal"
    assert old_run["status"] == "failed"

    stores["agent"].values["writer"]["instruction"] = "Write with the saved revision"
    stores["agent"].values["writer"]["model_id"] = "new-model"

    retried = runtime.retry_graph_run(old_run["graph_run_id"], "graph-retry")
    assert retried.status == "succeeded"
    new_run_id = runtime.graph_run_id_for_task(retried.task_id)
    new_run = runtime.graph_run_detail(new_run_id)
    assert new_run["graph_run_id"] != old_run["graph_run_id"]
    assert new_run["retry_of"] == old_run["graph_run_id"]
    assert new_run["plan_id"] != old_run["plan_id"]
    assert new_run["plan"]["graph"]["nodes"][0]["agent"]["model_id"] == "new-model"
    assert new_run["nodes"][0]["node_run_id"] != old_run["nodes"][0]["node_run_id"]
    assert runner.calls == [("writer-node", "Enter"), ("writer-node", "Enter")]


def test_debug_replay_runs_one_node_with_current_agent_and_frozen_input(tmp_path):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    (card / "memory").mkdir(parents=True)
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    (card / ".card_data.json").write_text('{"name":"Test"}', encoding="utf-8")
    project = {"id": "project", "worldbook_ids": []}
    graph = {
        "id": "writing",
        "nodes": [
            {"node_id": "first", "agent_id": "writer"},
            {"node_id": "second", "agent_id": "writer"},
            {"node_id": "third", "agent_id": "writer"},
        ],
        "output_node_id": "third",
    }
    agent = _agent("writer")
    stores = {
        "project": _MemoryStore({"project": project}),
        "graph": _MemoryStore({"writing": graph}),
        "agent": _MemoryStore({"writer": agent}),
    }
    compiler = ExecutionPlanCompiler(
        project_store=stores["project"],
        graph_store=stores["graph"],
        agent_store=stores["agent"],
    )
    runner = _ReplayRunner()
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
        execution_plan_compiler=compiler,
        graph_runtime=GraphRuntime(runner),
        project_id="project",
        execution_graph_id="writing",
        bootstrap_legacy_history=False,
    )

    result = runtime.submit("Enter", "replay-source")
    assert result.status == "succeeded"
    original = runtime.graph_runs_snapshot()["most_recent"]
    source = next(node for node in original["nodes"] if node["node_id"] == "second")
    chat_before = (card / "chat_log.json").read_bytes()
    revision_before = runtime.active_revision()

    stores["agent"].values["writer"]["instruction"] = "Current replay instruction"
    stores["agent"].values["writer"]["model_id"] = "current-model"
    stores["agent"].values["writer"]["prompt"] = [{"role": "system", "content": "Current prompt"}]

    replay = runtime.debug_replay(source["node_run_id"], idempotency_key="replay-default")

    assert replay["state"] == "succeeded"
    assert replay["source_node_run_id"] == source["node_run_id"]
    assert replay["old_output"] == "old second output"
    assert replay["new_output"] == "replayed second output"
    assert replay["old_effective_config"]["model_id"] == "model"
    assert replay["new_effective_config"]["model_id"] == "current-model"
    assert replay["effective_config_diff"]["model_id"]["old"] == "model"
    assert replay["effective_config_diff"]["model_id"]["new"] == "current-model"
    assert replay["input_artifact"]["content"] == "first output"
    assert replay["upstream_artifacts"][0]["content"] == "first output"
    assert runtime.active_revision() == revision_before
    assert (card / "chat_log.json").read_bytes() == chat_before
    assert runner.calls == [
        ("first", "Enter"),
        ("second", "first output"),
        ("third", "old second output"),
        ("second", "first output"),
    ]

    keyed = runtime.debug_replay(source["node_run_id"], idempotency_key="replay-once")
    repeated = runtime.debug_replay(source["node_run_id"], idempotency_key="replay-once")
    assert repeated["debug_replay_id"] == keyed["debug_replay_id"]
    assert len(runner.calls) == 5


def test_graph_trace_survives_restart_and_prunes_older_terminal_runs(tmp_path):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    (card / "memory").mkdir(parents=True)
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    (card / ".card_data.json").write_text('{"name":"Test"}', encoding="utf-8")
    stores = {
        "project": _MemoryStore({"project": {"id": "project"}}),
        "graph": _MemoryStore(
            {
                "writing": {
                    "id": "writing",
                    "nodes": [{"node_id": "writer-node", "agent_id": "writer"}],
                    "output_node_id": "writer-node",
                }
            }
        ),
        "agent": _MemoryStore({"writer": _agent("writer")}),
    }
    compiler = ExecutionPlanCompiler(
        project_store=stores["project"],
        graph_store=stores["graph"],
        agent_store=stores["agent"],
    )
    runner = _RetentionRunner()
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
        execution_plan_compiler=compiler,
        graph_runtime=GraphRuntime(runner),
        project_id="project",
        execution_graph_id="writing",
        bootstrap_legacy_history=False,
    )

    first = runtime.submit("one", "retention-1")
    first_id = runtime.graph_run_id_for_task(first.task_id)
    restarted = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
        execution_plan_compiler=compiler,
        graph_runtime=GraphRuntime(runner),
        project_id="project",
        execution_graph_id="writing",
        bootstrap_legacy_history=False,
    )
    assert restarted.graph_run_detail(first_id)["nodes"][0]["state"] == "succeeded"

    second = restarted.submit("two", "retention-2")
    second_id = restarted.graph_run_id_for_task(second.task_id)
    third = restarted.submit("three", "retention-3")
    third_id = restarted.graph_run_id_for_task(third.task_id)

    assert restarted.graph_run_detail(third_id)["status"] == "succeeded"
    assert restarted.graph_run_detail(second_id) is None
    assert restarted.graph_run_detail(first_id) is None


def test_restart_marks_in_flight_graph_run_interrupted(tmp_path):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    (card / "memory").mkdir(parents=True)
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    (card / ".card_data.json").write_text('{"name":"Test"}', encoding="utf-8")
    stores = {
        "project": _MemoryStore({"project": {"id": "project"}}),
        "graph": _MemoryStore(
            {
                "writing": {
                    "id": "writing",
                    "nodes": [{"node_id": "writer-node", "agent_id": "writer"}],
                    "output_node_id": "writer-node",
                }
            }
        ),
        "agent": _MemoryStore({"writer": _agent("writer")}),
    }
    compiler = ExecutionPlanCompiler(
        project_store=stores["project"],
        graph_store=stores["graph"],
        agent_store=stores["agent"],
    )
    runner = _BlockingGraphRunner()
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
        execution_plan_compiler=compiler,
        graph_runtime=GraphRuntime(runner),
        project_id="project",
        execution_graph_id="writing",
        bootstrap_legacy_history=False,
    )
    worker = threading.Thread(target=runtime.submit, args=("in flight", "restart-active"))
    worker.start()
    assert runner.started.wait(timeout=3)

    restarted = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
        execution_plan_compiler=compiler,
        graph_runtime=GraphRuntime(_RetentionRunner()),
        project_id="project",
        execution_graph_id="writing",
        bootstrap_legacy_history=False,
    )
    recovered = restarted.graph_runs_snapshot()["most_recent"]
    assert recovered["status"] == "interrupted"
    assert recovered["nodes"][0]["state"] == "failed"

    runner.release.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert restarted.graph_run_detail(recovered["graph_run_id"])["status"] == "interrupted"


class _BlockingStreamingProvider(FakeProvider):
    def __init__(self):
        super().__init__(
            [{"type": "final", "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}],
            model="observed-model",
            credentials={"api_key": "sk-node-secret"},
        )
        self.started = threading.Event()
        self.release = threading.Event()

    def stream(self, request, signal):
        self.requests.append(request)
        self.started.set()
        yield ProviderDelta(text="partial output")
        self.release.wait(timeout=5)
        if signal.cancelled:
            raise RuntimeError("unexpected cancellation")
        yield ProviderResult(
            usage=self._default_usage_for_test(),
            stop_reason="stop",
        )

    def _default_usage_for_test(self):
        from airp.engine.provider import UsageRecord

        return UsageRecord(prompt_tokens=3, completion_tokens=2, total_tokens=5)


def _wait_for_http_json(url, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        status, latest = _json_request("GET", url)
        if status == 200 and predicate(latest):
            return latest
        time.sleep(0.02)
    return latest


def test_session_graph_events_and_node_detail_are_live_and_persisted(tmp_path):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    (card / "memory").mkdir(parents=True)
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    (card / ".card_data.json").write_text('{"name":"Test"}', encoding="utf-8")
    provider = _BlockingStreamingProvider()
    compiler = ExecutionPlanCompiler(
        project_store=_MemoryStore({"project": {"id": "project", "worldbook_ids": []}}),
        graph_store=_MemoryStore(
            {
                "writing": {
                    "id": "writing",
                    "nodes": [{"node_id": "writer-node", "agent_id": "writer"}],
                    "output_node_id": "writer-node",
                }
            }
        ),
        agent_store=_MemoryStore(
            {
                "writer": {
                    **_agent("writer"),
                    "advanced": {"authorization": "Bearer sk-node-secret", "response_format": {"type": "text"}},
                }
            }
        ),
    )
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
        execution_plan_compiler=compiler,
        graph_runtime=GraphRuntime(ProviderNodeRunner(lambda _node: provider)),
        project_id="project",
        execution_graph_id="writing",
        bootstrap_legacy_history=False,
    )

    with SessionRuntimeServer(runtime, static_root=styles) as server:
        status, accepted = _json_request(
            "POST",
            f"{server.base_url}/v1/session/commands/submit",
            {"text": "Enter", "idempotency_key": "observed-submit"},
        )
        assert status in {200, 202}
        assert accepted["task_id"]
        assert provider.started.wait(timeout=3)

        _wait_for_http_json(
            f"{server.base_url}/v1/session/events?after=0",
            lambda payload: any(event["type"] == "graph.node.started" for event in payload.get("events", [])),
        )
        status, event_payload = _json_request(
            "GET", f"{server.base_url}/v1/session/events?after=0"
        )
        assert status == 200
        events = event_payload["events"]
        started = next(event for event in events if event["type"] == "graph.node.started")
        assert started["state"] == "running"
        assert started["node_run_id"]

        status, running_detail = _json_request(
            "GET", f"{server.base_url}/v1/studio/node-runs/{started['node_run_id']}"
        )
        assert status == 200
        assert running_detail["node_run"]["state"] == "running"
        assert running_detail["node_run"]["streamed_output"] == "partial output"
        assert "sk-node-secret" not in json.dumps(running_detail, ensure_ascii=False)

        provider.release.set()
        terminal = _wait_for_http_json(
            f"{server.base_url}/v1/session/events?after=0",
            lambda payload: any(event["type"] == "graph.node.finished" for event in payload.get("events", [])),
        )
        assert any(event["type"] == "graph.node.finished" and event["state"] == "succeeded" for event in terminal["events"])
        status, finished_detail = _json_request(
            "GET", f"{server.base_url}/v1/studio/node-runs/{started['node_run_id']}"
        )
        assert status == 200
        assert finished_detail["node_run"]["state"] == "succeeded"
        assert finished_detail["node_run"]["final_output"] == "partial output"
        assert finished_detail["node_run"]["artifact"]["content"] == "partial output"
        assert "sk-node-secret" not in json.dumps(finished_detail, ensure_ascii=False)


def test_studio_http_exposes_graph_retry_and_isolated_node_replay(tmp_path):
    styles = tmp_path / "styles"
    styles.mkdir()
    card = tmp_path / "card"
    (card / "memory").mkdir(parents=True)
    (card / ".initvar.json").write_text("{}", encoding="utf-8")
    (card / "chat_log.json").write_text("[]", encoding="utf-8")
    (card / ".card_data.json").write_text('{"name":"Test"}', encoding="utf-8")
    stores = {
        "project": _MemoryStore({"project": {"id": "project"}}),
        "graph": _MemoryStore(
            {
                "writing": {
                    "id": "writing",
                    "nodes": [{"node_id": "writer-node", "agent_id": "writer"}],
                    "output_node_id": "writer-node",
                }
            }
        ),
        "agent": _MemoryStore({"writer": _agent("writer")}),
    }
    compiler = ExecutionPlanCompiler(
        project_store=stores["project"],
        graph_store=stores["graph"],
        agent_store=stores["agent"],
    )
    runner = _RetryRunner()
    runtime = SessionTurnRuntime(
        database_path=tmp_path / "runtime.sqlite3",
        card_folder=card,
        projection_root=styles,
        execution_plan_compiler=compiler,
        graph_runtime=GraphRuntime(runner),
        project_id="project",
        execution_graph_id="writing",
        bootstrap_legacy_history=False,
    )

    with SessionRuntimeServer(runtime, static_root=styles) as server:
        status, accepted = _json_request(
            "POST",
            f"{server.base_url}/v1/session/commands/submit",
            {"text": "Enter", "idempotency_key": "http-graph-failure"},
        )
        assert status in {200, 202}
        assert accepted["task_id"]
        failed = _wait_for_http_json(
            f"{server.base_url}/v1/studio/graph-runs",
            lambda payload: (payload.get("most_recent") or {}).get("status") == "failed",
        )
        old_id = failed["most_recent"]["graph_run_id"]

        stores["agent"].values["writer"]["model_id"] = "http-current-model"
        status, retry = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/graph-runs/{old_id}/retry",
            {"idempotency_key": "http-graph-retry"},
        )
        assert status in {200, 202}
        latest = _wait_for_http_json(
            f"{server.base_url}/v1/studio/graph-runs",
            lambda payload: (payload.get("most_recent") or {}).get("status") == "succeeded",
        )
        retried = latest["most_recent"]
        assert retried["retry_of"] == old_id
        node_run_id = retried["nodes"][0]["node_run_id"]
        revision_before = runtime.active_revision()

        status, replay = _json_request(
            "POST",
            f"{server.base_url}/v1/studio/node-runs/{node_run_id}/replay",
            {"idempotency_key": "http-node-replay"},
        )
        assert status == 200
        assert replay["debug_replay"]["state"] == "succeeded"
        assert replay["debug_replay"]["new_effective_config"]["model_id"] == "http-current-model"
        assert runtime.active_revision() == revision_before
