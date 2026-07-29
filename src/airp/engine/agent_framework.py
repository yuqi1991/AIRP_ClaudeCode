"""Content-neutral execution adapters for the Agent Framework.

The framework returns GraphRunResult and opaque AgentArtifacts.  Interpretation
of a successful artifact belongs to a selected turn adapter, never to Graph
Runtime or Node Runner.
"""

from __future__ import annotations

from typing import Any, Protocol

from airp.engine.graph_runtime import AgentArtifact, ExecutionPlan, GraphRunResult, GraphRuntime


class TurnAdapter(Protocol):
    """Interpret one successful framework Artifact for a host application."""

    adapter_id: str

    def interpret(
        self,
        artifact: AgentArtifact,
        *,
        player_input: str = "",
        context: Any = None,
    ) -> Any:
        ...


class AgentFrameworkExecutor:
    """Run a frozen plan without interpreting its output content."""

    def __init__(self, graph_runtime: GraphRuntime, plan: ExecutionPlan, *, observer: Any = None):
        self.graph_runtime = graph_runtime
        self.plan = plan
        self.observer = observer

    def run_graph(self, text: str) -> GraphRunResult:
        return self.graph_runtime.run(
            self.plan,
            AgentArtifact.input(text),
            observer=self.observer,
        )

    def run(self, text: str) -> GraphRunResult:
        """Return the framework result, including an opaque output Artifact."""
        return self.run_graph(text)


class AdaptedGraphExecutor:
    """Apply a host-selected TurnAdapter after a framework Graph Run succeeds."""

    def __init__(self, framework: AgentFrameworkExecutor, adapter: TurnAdapter):
        if adapter is None or not callable(getattr(adapter, "interpret", None)):
            raise TypeError("AdaptedGraphExecutor requires a TurnAdapter")
        self.framework = framework
        self.adapter = adapter

    def run(self, text: str, compiled_context: Any = None) -> Any:
        del compiled_context
        result = self.framework.run_graph(text)
        if not result.ok or result.output_artifact is None:
            from airp.engine.graph_runtime import GraphExecutionError

            raise GraphExecutionError(result)
        return self.adapter.interpret(
            result.output_artifact,
            player_input=text,
            context={"plan_id": result.plan_id},
        )
