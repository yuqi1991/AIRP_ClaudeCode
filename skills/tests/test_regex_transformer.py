from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest


SKILLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILLS))

from engine.regex_transformer import RegexTransformError, RegexTransformer  # noqa: E402
from engine.graph_runtime import AgentArtifact, ExecutionPlanCompiler  # noqa: E402
from engine.node_runner import ProviderNodeRunner  # noqa: E402
from engine.provider import FakeProvider  # noqa: E402


def test_regex_transformer_applies_ordered_javascript_rules_and_reports_diagnostics():
    result = RegexTransformer().transform(
        "<content>hello</content>",
        [
            {
                "id": "unwrap",
                "name": "Unwrap content",
                "target": "output",
                "pattern": r"^[\s\S]*?<content>([\s\S]*?)</content>[\s\S]*$",
                "replacement": "$1",
            },
            {
                "id": "capitalize",
                "name": "Capitalize",
                "target": "output",
                "pattern": "hello",
                "flags": "i",
                "replacement": "Hi",
            },
        ],
        target="output",
    )

    assert result.text == "Hi"
    assert [item.rule_id for item in result.diagnostics] == ["unwrap", "capitalize"]
    assert [item.changed for item in result.diagnostics] == [True, True]
    assert all(item.matched for item in result.diagnostics)


def test_regex_transformer_honors_input_output_and_both_targets_and_disabled_rules():
    rules = [
        {
            "id": "input-only",
            "target": "input",
            "pattern": "secret",
            "replacement": "public",
        },
        {
            "id": "both",
            "target": "both",
            "pattern": "public",
            "flags": "g",
            "replacement": "visible",
        },
        {
            "id": "output-only",
            "target": "output",
            "pattern": "visible",
            "replacement": "shown",
        },
        {
            "id": "disabled",
            "enabled": False,
            "target": "both",
            "pattern": "shown",
            "replacement": "BROKEN",
        },
    ]

    input_result = RegexTransformer().transform("secret", rules, target="input")
    output_result = RegexTransformer().transform("public public", rules, target="output")

    assert input_result.text == "visible"
    assert output_result.text == "shown visible"
    assert input_result.diagnostics[0].applied is True
    assert input_result.diagnostics[2].applied is False
    assert output_result.diagnostics[0].applied is False
    assert output_result.diagnostics[1].changed is True
    assert output_result.diagnostics[3].skipped is True


def test_regex_transformer_rejects_invalid_javascript_pattern_with_rule_context():
    with pytest.raises(RegexTransformError, match="broken") as caught:
        RegexTransformer().transform(
            "text",
            [
                {
                    "id": "broken",
                    "name": "Broken rule",
                    "target": "output",
                    "pattern": "[",
                    "replacement": "",
                }
            ],
            target="output",
        )

    assert caught.value.rule_id == "broken"
    assert caught.value.target == "output"


class _Observer:
    def __init__(self):
        self.deltas = []
        self.outputs = []

    def node_delta(self, node, text):
        del node
        self.deltas.append(text)

    def output_transformed(self, node, raw, result):
        del node
        self.outputs.append((raw, result.text))


def test_provider_node_runner_transforms_input_and_completed_output_but_keeps_raw_deltas():
    plan = ExecutionPlanCompiler().compile(
        project={"id": "project"},
        graph={
            "id": "writing",
            "nodes": [{"node_id": "writer-node", "agent_id": "writer"}],
            "output_node_id": "writer-node",
        },
        agents={
            "writer": {
                "agent_id": "writer",
                "name": "Writer",
                "instruction": "handoff={{handoff}} artifact={{input_artifact.content}}",
            }
        },
        player_input="raw",
    )
    provider = FakeProvider(
        [
            {"type": "text", "text": "<content>"},
            {"type": "text", "text": "done</content>"},
            {"type": "final"},
        ],
        model="writer-model",
    )
    transformer = RegexTransformer(
        [
            {
                "id": "normalize-input",
                "target": "input",
                "pattern": "raw",
                "replacement": "clean",
            },
            {
                "id": "unwrap",
                "target": "output",
                "pattern": r"^<content>([\s\S]*)</content>$",
                "replacement": "$1",
            },
        ]
    )
    observer = _Observer()
    node = plan.graph.nodes[0]
    node = replace(
        node,
        agent=replace(
            node.agent,
            prompt=({"role": "system", "content": "handoff={{handoff}} artifact={{input_artifact.content}}"},),
        ),
    )

    result = ProviderNodeRunner(
        lambda _node: provider,
        regex_transformer=transformer,
    ).run(node, AgentArtifact.input("raw"), observer=observer)

    assert result.ok
    assert result.primary_artifact.content == "done"
    assert provider.requests[0].messages[-1]["content"] == "clean"
    assert provider.requests[0].messages[0]["content"] == "handoff=clean artifact=clean"
    assert observer.deltas == ["<content>", "done</content>"]
    assert observer.outputs == [("<content>done</content>", "done")]


def test_provider_node_runner_fails_node_when_output_regex_is_invalid():
    plan = ExecutionPlanCompiler().compile(
        project={"id": "project"},
        graph={
            "id": "writing",
            "nodes": [{"node_id": "writer-node", "agent_id": "writer"}],
            "output_node_id": "writer-node",
        },
        agents={"writer": {"agent_id": "writer", "name": "Writer", "instruction": "Write"}},
        player_input="raw",
    )
    provider = FakeProvider([{"type": "text", "text": "output"}, {"type": "final"}], model="writer")
    result = ProviderNodeRunner(
        lambda _node: provider,
        regex_transformer=RegexTransformer(
            [{"id": "broken", "target": "output", "pattern": "[", "replacement": ""}]
        ),
    ).run(plan.graph.nodes[0], AgentArtifact.input("raw"))

    assert result.status == "failed"
    assert result.error["code"] == "invalid_regex"
    assert result.error["rule_id"] == "broken"
