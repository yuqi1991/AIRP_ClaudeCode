"""Host commit conversion for a successful content-neutral Graph Run."""

from __future__ import annotations

import json
from typing import Any

from airp.engine.agent_framework import AgentFrameworkExecutor
from airp.engine.graph_runtime import AgentArtifact, GraphExecutionError, NodeExecutionContext


def turn_draft_from_artifact(artifact: AgentArtifact, *, player_input: str = "") -> Any:
    """Create the host's minimal committed-turn shape without parsing content.

    Agent Regex Collections have already transformed the final Artifact.  The
    host intentionally treats that content as opaque text and does not infer
    tags, sections, prose format, or writing policy from it.
    """
    from airp.host.rp.session_runtime import TurnDraft

    content = artifact.content
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, sort_keys=True)
    return TurnDraft(content=content, polished_input=player_input)


class GraphTurnCommitExecutor:
    """Run a Graph then expose one host turn to the session commit loop."""

    def __init__(
        self,
        framework: AgentFrameworkExecutor,
        *,
        execution_context: NodeExecutionContext | None = None,
    ):
        self._framework = framework
        self._execution_context = execution_context

    def run(self, text: str, compiled_context: Any = None) -> Any:
        del compiled_context
        result = self._framework.run_graph(text, execution_context=self._execution_context)
        if not result.ok or result.output_artifact is None:
            raise GraphExecutionError(result)
        return turn_draft_from_artifact(result.output_artifact, player_input=text)
