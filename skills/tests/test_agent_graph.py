from __future__ import annotations

import sys
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILLS))

from engine.agent_graph import SequentialAgentGraph, SequentialGraphNode  # noqa: E402
from engine.context_compiler import CompiledContext  # noqa: E402
from engine.director import NarrativeDirector  # noqa: E402


class _Director(NarrativeDirector):
    def __init__(self, result):
        self.result = result
        self.payloads = []

    def direct(self, handle, compiled):
        self.payloads.append(list(compiled.payload))
        handle.set_final_text(self.result)


class _Handle:
    def __init__(self):
        self.final = None
        self.commit_feedback = None
        self.task_id = "task"
        self._task_text = ""

    @property
    def task_text(self):
        return self._task_text

    def set_final_text(self, text):
        self.final = text

    def take_final_text(self):
        value = self.final
        self.final = None
        return value

    def call_tool(self, name, args):
        raise AssertionError("not used")

    def tool_schemas(self):
        return []

    def set_commit_feedback(self, error, details=None):
        self.commit_feedback = {"error": error, "details": details}

    def emit_preview(self, text):
        pass

    aborted = False
    signal = None

    def compile_follow_up(self):
        raise AssertionError("not used")

    def compile_sequential_handoff(self, compiled, source_node, target_node, text):
        payload = list(compiled.payload)
        payload.append(
            {
                "role": "user",
                "content": f"[sequential graph handoff from {source_node.id}/{source_node.role}]\n{text}",
            }
        )
        return CompiledContext(payload, {}, "handoff", "stable")

    def report_model_call_started(self, meta):
        pass

    def report_model_call_finished(self, meta):
        pass


def test_sequential_graph_hands_prior_output_to_next_node_and_commits_last():
    planner = _Director("plan")
    writer = _Director("final turn")
    graph = SequentialAgentGraph(
        [
            SequentialGraphNode("planner", "story_planner", planner),
            SequentialGraphNode("director", "narrative_director", writer),
        ]
    )
    handle = _Handle()

    compiled = CompiledContext(
        [{"role": "user", "content": "base"}],
        {},
        "payload",
        "stable",
    )
    graph.direct(handle, compiled)

    assert handle.final == "final turn"
    assert writer.payloads[0][-1]["role"] == "user"
    assert "plan" in writer.payloads[0][-1]["content"]
