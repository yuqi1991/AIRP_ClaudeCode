from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "skills"))

from airp.engine.agent_definitions import AgentDefinitionStore  # noqa: E402
from airp.engine.graph_runtime import ExecutionPlanCompiler  # noqa: E402
from airp.engine.node_runner import ProviderNodeRunner  # noqa: E402
from airp.engine.provider import FakeProvider  # noqa: E402
from airp.engine.graph_runtime import AgentArtifact  # noqa: E402


def test_agent_definition_persists_an_optional_regex_collection_binding(tmp_path: Path):
    store = AgentDefinitionStore(tmp_path)

    created = store.create_agent(
        {
            "agent_id": "writer",
            "name": "Writer",
            "instruction": "Write",
            "regex_collection_id": "extract-final-prose",
        }
    )

    assert created["regex_collection_id"] == "extract-final-prose"
    assert store.get_agent("writer")["regex_collection_id"] == "extract-final-prose"

    cleared = store.update_agent("writer", {"regex_collection_id": None})

    assert cleared["regex_collection_id"] is None


class _MemoryStore:
    def __init__(self, values):
        self.values = values

    def get_agent(self, key):
        return self.values[key]

    def get_collection(self, key):
        return self.values[key]


def test_execution_plan_freezes_the_bound_regex_collection_snapshot():
    agents = _MemoryStore(
        {
            "writer": {
                "agent_id": "writer",
                "name": "Writer",
                "instruction": "Write {{handoff}}",
                "regex_collection_id": "extract",
            }
        }
    )
    collections = _MemoryStore(
        {
            "extract": {
                "id": "extract",
                "name": "Extract",
                "rules": [
                    {
                        "id": "unwrap",
                        "name": "Unwrap",
                        "enabled": True,
                        "target": "output",
                        "pattern": "<content>([\\s\\S]*?)</content>",
                        "flags": "",
                        "replacement": "$1",
                    }
                ],
            }
        }
    )
    compiler = ExecutionPlanCompiler(
        agent_store=agents,
        regex_collection_store=collections,
    )

    plan = compiler.compile(
        project={"id": "project"},
        graph={
            "id": "story",
            "nodes": [{"node_id": "writer-node", "agent_id": "writer"}],
            "output_node_id": "writer-node",
        },
        player_input="hello",
    )

    collections.values["extract"]["rules"][0]["replacement"] = "BROKEN"

    frozen = plan.graph.nodes[0].agent.effective_config["regex_collection"]
    assert frozen["rules"][0]["replacement"] == "$1"
    assert plan.graph.nodes[0].agent.regex_collection_id == "extract"


def test_provider_node_runner_uses_the_frozen_agent_collection_without_project_config():
    agents = _MemoryStore(
        {
            "writer": {
                "agent_id": "writer",
                "name": "Writer",
                "instruction": "Write",
                "regex_collection_id": "extract",
            }
        }
    )
    collections = _MemoryStore(
        {
            "extract": {
                "id": "extract",
                "name": "Extract",
                "rules": [
                    {
                        "id": "unwrap",
                        "name": "Unwrap",
                        "enabled": True,
                        "target": "output",
                        "pattern": r"^<content>([\s\S]*)</content>$",
                        "flags": "",
                        "replacement": "$1",
                    }
                ],
            }
        }
    )
    plan = ExecutionPlanCompiler(
        agent_store=agents,
        regex_collection_store=collections,
    ).compile(
        project={"id": "project"},
        graph={
            "id": "story",
            "nodes": [{"node_id": "writer-node", "agent_id": "writer"}],
            "output_node_id": "writer-node",
        },
        player_input="hello",
    )
    provider = FakeProvider(
        [{"type": "text", "text": "<content>done</content>"}, {"type": "final"}],
        model="writer",
    )

    result = ProviderNodeRunner(lambda _node: provider).run(
        plan.graph.nodes[0], AgentArtifact.input("hello")
    )

    assert result.ok
    assert result.primary_artifact.content == "done"
