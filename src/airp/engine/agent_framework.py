"""Content-neutral execution facade for the Agent Framework."""

from __future__ import annotations

from typing import Any

from airp.engine.graph_runtime import (
    AgentArtifact,
    ExecutionPlan,
    GraphRunResult,
    GraphRuntime,
    NodeExecutionContext,
)


class AgentFrameworkExecutor:
    """Run a frozen plan without interpreting its output content."""

    def __init__(self, graph_runtime: GraphRuntime, plan: ExecutionPlan, *, observer: Any = None):
        self.graph_runtime = graph_runtime
        self.plan = plan
        self.observer = observer

    def run_graph(
        self,
        text: str,
        *,
        execution_context: NodeExecutionContext | None = None,
    ) -> GraphRunResult:
        return self.graph_runtime.run(
            self.plan,
            AgentArtifact.input(text),
            observer=self.observer,
            execution_context=execution_context,
        )

    def run(
        self,
        text: str,
        *,
        execution_context: NodeExecutionContext | None = None,
    ) -> GraphRunResult:
        """Return the framework result, including an opaque output Artifact."""
        return self.run_graph(text, execution_context=execution_context)
