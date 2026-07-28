"""Execution plans and provider-independent linear Graph Runtime.

The runtime in this module only knows ordered nodes, immutable Artifacts and
Node Results.  Provider/model execution belongs behind the ``NodeRunner``
boundary supplied by callers.
"""

from __future__ import annotations

import copy
import hashlib
import json
import time
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _copy(value: Any) -> Any:
    return copy.deepcopy(value)


def _redact_secrets(value: Any) -> Any:
    """Remove secret-shaped fields before a definition enters an Execution Plan."""
    secret_keys = {
        "api_key",
        "apikey",
        "secret",
        "secret_ref",
        "authorization",
        "credentials",
    }
    if isinstance(value, dict):
        return {
            key: _redact_secrets(item)
            for key, item in value.items()
            if str(key).casefold() not in secret_keys
        }
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    return _copy(value)


@dataclass(frozen=True)
class AgentArtifact:
    """Immutable value handed between Graph nodes."""

    kind: str
    content_type: str
    content: Any
    content_hash: str
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, str) or not self.kind.strip():
            raise ValueError("Artifact kind must be a non-empty string")
        if not isinstance(self.content_type, str) or not self.content_type.strip():
            raise ValueError("Artifact content_type must be a non-empty string")
        object.__setattr__(self, "content", _copy(self.content))
        expected = _hash(self.content)
        if self.content_hash != expected:
            raise ValueError("Artifact content_hash does not match content")
        object.__setattr__(self, "metadata", _copy(dict(self.metadata)))

    @classmethod
    def text(
        cls,
        content: str,
        *,
        kind: str = "narrative_draft",
        content_type: str = "text/plain",
        metadata: Mapping[str, Any] | None = None,
    ) -> "AgentArtifact":
        if not isinstance(content, str):
            raise ValueError("text Artifact content must be a string")
        return cls(kind, content_type, content, _hash(content), metadata or {})

    @classmethod
    def input(cls, content: str) -> "AgentArtifact":
        return cls.text(content, kind="player_input")

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "AgentArtifact":
        content = _copy(payload.get("content"))
        return cls(
            kind=str(payload.get("kind") or "narrative_draft"),
            content_type=str(payload.get("content_type") or "text/plain"),
            content=content,
            content_hash=str(payload.get("content_hash") or _hash(content)),
            metadata=payload.get("metadata") or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "content_type": self.content_type,
            "content": _copy(self.content),
            "content_hash": self.content_hash,
            "metadata": _copy(dict(self.metadata)),
        }


@dataclass(frozen=True)
class NodeResult:
    """Stable result boundary returned by one Node Runner invocation."""

    status: str
    primary_artifact: AgentArtifact | None = None
    diagnostics_ref: str | None = None
    error: Any = None

    def __post_init__(self) -> None:
        if self.status not in {"succeeded", "failed"}:
            raise ValueError("Node Result status must be succeeded or failed")
        if self.status == "succeeded" and self.primary_artifact is None:
            raise ValueError("a successful Node Result requires an Artifact")

    @property
    def artifact(self) -> AgentArtifact | None:
        return self.primary_artifact

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"

    @classmethod
    def succeeded(cls, artifact: AgentArtifact, *, diagnostics_ref: str | None = None) -> "NodeResult":
        return cls("succeeded", artifact, diagnostics_ref=diagnostics_ref)

    @classmethod
    def failed(
        cls,
        error: Any,
        *,
        diagnostics_ref: str | None = None,
    ) -> "NodeResult":
        return cls("failed", None, diagnostics_ref=diagnostics_ref, error=_copy(error))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "NodeResult":
        artifact = payload.get("primary_artifact") or payload.get("artifact")
        return cls(
            status=str(payload.get("status") or "failed"),
            primary_artifact=AgentArtifact.from_dict(artifact) if isinstance(artifact, Mapping) else None,
            diagnostics_ref=payload.get("diagnostics_ref"),
            error=_copy(payload.get("error")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "primary_artifact": self.primary_artifact.to_dict() if self.primary_artifact else None,
            "diagnostics_ref": self.diagnostics_ref,
            "error": _copy(self.error),
        }


@dataclass(frozen=True)
class ResolvedAgent:
    """Definition data needed by a single planned node."""

    agent_id: str
    name: str
    instruction: str
    prompt_preset_id: str | None
    provider_profile_id: str | None
    model_id: str | None
    generation: Mapping[str, Any]
    advanced: Mapping[str, Any]
    tool_allowlist: tuple[str, ...]
    prompt: tuple[Mapping[str, Any], ...]
    prompt_provenance: tuple[Mapping[str, Any], ...]
    effective_config: Mapping[str, Any]

    @classmethod
    def from_definition(cls, definition: Mapping[str, Any], preview: Mapping[str, Any] | None = None) -> "ResolvedAgent":
        preview = preview or {}
        effective = preview.get("effective_config") if isinstance(preview, Mapping) else None
        if not isinstance(effective, Mapping):
            effective = {
                "agent_id": definition.get("agent_id", definition.get("id")),
                "provider_profile_id": definition.get("provider_profile_id"),
                "model_id": definition.get("model_id"),
                "generation": _copy(definition.get("generation") or {}),
                "tool_allowlist": list(definition.get("tool_allowlist") or []),
                "stream": True,
            }
        raw_prompt = preview.get("messages") if isinstance(preview, Mapping) else None
        if not isinstance(raw_prompt, list):
            raw_prompt = definition.get("prompt", definition.get("compiled_prompt", definition.get("messages", [])))
        if not isinstance(raw_prompt, list):
            raw_prompt = []
        raw_provenance = preview.get("provenance") if isinstance(preview, Mapping) else None
        if not isinstance(raw_provenance, list):
            raw_provenance = definition.get("prompt_provenance", [])
        if not isinstance(raw_provenance, list):
            raw_provenance = []
        tool_allowlist = definition.get("tool_allowlist") or definition.get("allowed_tools") or []
        if not isinstance(tool_allowlist, list):
            tool_allowlist = []
        return cls(
            agent_id=str(definition.get("agent_id", definition.get("id")) or ""),
            name=str(definition.get("name") or definition.get("agent_id") or "Agent"),
            instruction=str(definition.get("instruction") or ""),
            prompt_preset_id=definition.get("prompt_preset_id"),
            provider_profile_id=definition.get("provider_profile_id"),
            model_id=definition.get("model_id"),
            generation=_copy(definition.get("generation") or {}),
            advanced=_copy(definition.get("advanced") or {}),
            tool_allowlist=tuple(str(item) for item in tool_allowlist),
            prompt=tuple(_copy(item) for item in raw_prompt if isinstance(item, Mapping)),
            prompt_provenance=tuple(_copy(item) for item in raw_provenance if isinstance(item, Mapping)),
            effective_config=_copy(dict(effective)),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ResolvedAgent":
        return cls(
            agent_id=str(payload.get("agent_id") or ""),
            name=str(payload.get("name") or payload.get("agent_id") or "Agent"),
            instruction=str(payload.get("instruction") or ""),
            prompt_preset_id=payload.get("prompt_preset_id"),
            provider_profile_id=payload.get("provider_profile_id"),
            model_id=payload.get("model_id"),
            generation=_copy(payload.get("generation") or {}),
            advanced=_copy(payload.get("advanced") or {}),
            tool_allowlist=tuple(str(item) for item in payload.get("tool_allowlist") or []),
            prompt=tuple(_copy(item) for item in payload.get("prompt") or []),
            prompt_provenance=tuple(_copy(item) for item in payload.get("prompt_provenance") or []),
            effective_config=_copy(payload.get("effective_config") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "name": self.name,
            "instruction": self.instruction,
            "prompt_preset_id": self.prompt_preset_id,
            "provider_profile_id": self.provider_profile_id,
            "model_id": self.model_id,
            "generation": _copy(dict(self.generation)),
            "advanced": _copy(dict(self.advanced)),
            "tool_allowlist": list(self.tool_allowlist),
            "prompt": [_copy(item) for item in self.prompt],
            "prompt_provenance": [_copy(item) for item in self.prompt_provenance],
            "effective_config": _copy(dict(self.effective_config)),
        }


@dataclass(frozen=True)
class GraphNodePlan:
    node_id: str
    agent_id: str
    label: str | None
    order: int
    enabled: bool
    agent: ResolvedAgent
    generation: Mapping[str, Any] = field(default_factory=dict)
    provider_profile_id: str | None = None
    model_id: str | None = None
    advanced: Mapping[str, Any] = field(default_factory=dict)

    @property
    def prompt(self) -> tuple[Mapping[str, Any], ...]:
        """Convenience view of the prompt frozen for this node."""
        return self.agent.prompt

    @property
    def tool_allowlist(self) -> tuple[str, ...]:
        return self.agent.tool_allowlist

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "GraphNodePlan":
        agent_payload = payload.get("agent") or {}
        return cls(
            node_id=str(payload.get("node_id") or payload.get("id") or ""),
            agent_id=str(payload.get("agent_id") or ""),
            label=payload.get("label"),
            order=int(payload.get("order", 0)),
            enabled=bool(payload.get("enabled", True)),
            agent=ResolvedAgent.from_dict(agent_payload),
            generation=_copy(payload.get("generation") or {}),
            provider_profile_id=payload.get("provider_profile_id"),
            model_id=payload.get("model_id"),
            advanced=_copy(payload.get("advanced") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "id": self.node_id,
            "agent_id": self.agent_id,
            "label": self.label,
            "order": self.order,
            "enabled": self.enabled,
            "agent": self.agent.to_dict(),
            "generation": _copy(dict(self.generation)),
            "provider_profile_id": self.provider_profile_id,
            "model_id": self.model_id,
            "advanced": _copy(dict(self.advanced)),
        }


@dataclass(frozen=True)
class GraphPlan:
    graph_id: str
    name: str
    nodes: tuple[GraphNodePlan, ...]
    output_node_id: str

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "GraphPlan":
        return cls(
            graph_id=str(payload.get("graph_id") or payload.get("id") or ""),
            name=str(payload.get("name") or payload.get("graph_id") or "Graph"),
            nodes=tuple(GraphNodePlan.from_dict(item) for item in payload.get("nodes") or []),
            output_node_id=str(payload.get("output_node_id") or ""),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            "id": self.graph_id,
            "name": self.name,
            "mode": "sequential",
            "nodes": [node.to_dict() for node in self.nodes],
            "output_node_id": self.output_node_id,
        }


@dataclass(frozen=True)
class ExecutionPlan:
    """Task-level frozen definition/configuration snapshot."""

    plan_id: str
    player_input: str
    project: Mapping[str, Any]
    worldbooks: tuple[Mapping[str, Any], ...]
    graph: GraphPlan
    created_at: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "project", _copy(dict(self.project)))
        object.__setattr__(self, "worldbooks", tuple(_copy(dict(book)) for book in self.worldbooks))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExecutionPlan":
        return cls(
            plan_id=str(payload.get("plan_id") or ""),
            player_input=str(payload.get("player_input") or ""),
            project=payload.get("project") or {},
            worldbooks=tuple(payload.get("worldbooks") or []),
            graph=GraphPlan.from_dict(payload.get("graph") or {}),
            created_at=int(payload.get("created_at") or 0),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "player_input": self.player_input,
            "project": _copy(dict(self.project)),
            "worldbooks": [_copy(dict(book)) for book in self.worldbooks],
            "graph": self.graph.to_dict(),
            "created_at": self.created_at,
        }


class ExecutionPlanCompiler:
    """Compile current Studio definitions into one task-level snapshot."""

    def __init__(
        self,
        *,
        agent_store=None,
        graph_store=None,
        project_store=None,
        worldbook_store=None,
    ) -> None:
        self.agent_store = agent_store
        self.graph_store = graph_store
        self.project_store = project_store
        self.worldbook_store = worldbook_store

    def compile(
        self,
        *,
        project: Mapping[str, Any] | str | None = None,
        graph: Mapping[str, Any] | str | None = None,
        agents: Mapping[str, Mapping[str, Any]] | list[Mapping[str, Any]] | None = None,
        worldbooks: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] | None = None,
        player_input: str = "",
        project_id: str | None = None,
        graph_id: str | None = None,
    ) -> ExecutionPlan:
        project = self._resolve_project(project, project_id)
        resolved_project_id = str(project.get("id") or project.get("project_id") or project_id or "project")
        graph = self._resolve_graph(graph, graph_id or project.get("graph_id"))
        resolved_graph_id = str(graph.get("id") or graph.get("graph_id") or graph_id or "graph")
        agents_by_id = self._resolve_agents(agents)
        books = self._resolve_worldbooks(worldbooks, project)

        raw_nodes = graph.get("nodes")
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise ValueError("Execution Plan requires a non-empty graph")
        planned_nodes: list[GraphNodePlan] = []
        seen: set[str] = set()
        for index, raw_node in enumerate(raw_nodes):
            if not isinstance(raw_node, Mapping):
                raise ValueError("graph nodes must be objects")
            if raw_node.get("enabled", True) is False:
                continue
            node_id = str(raw_node.get("node_id") or raw_node.get("id") or "")
            agent_id = str(raw_node.get("agent_id") or raw_node.get("agent_definition_id") or "")
            if not node_id or not agent_id:
                raise ValueError("graph nodes require node_id and agent_id")
            if node_id in seen:
                raise ValueError(f"duplicate graph node id: {node_id}")
            seen.add(node_id)
            definition = agents_by_id.get(agent_id)
            if definition is None and self.agent_store is not None and hasattr(self.agent_store, "get_agent"):
                definition = _copy(self.agent_store.get_agent(agent_id))
            if definition is None:
                raise ValueError(f"Agent Definition {agent_id!r} was not found")
            preview = self._preview_agent(definition, player_input)
            resolved_agent = self._apply_node_overrides(
                ResolvedAgent.from_definition(definition, preview),
                raw_node,
            )
            planned_nodes.append(
                GraphNodePlan(
                    node_id=node_id,
                    agent_id=agent_id,
                    label=raw_node.get("label"),
                    order=int(raw_node.get("order", index)),
                    enabled=True,
                    agent=resolved_agent,
                    generation=_copy(raw_node.get("generation") or raw_node.get("generation_override") or {}),
                    provider_profile_id=raw_node.get("provider_profile_id"),
                    model_id=raw_node.get("model_id") or raw_node.get("model"),
                    advanced=_copy(raw_node.get("advanced") or raw_node.get("advanced_override") or {}),
                )
            )
        planned_nodes.sort(key=lambda node: node.order)
        planned_nodes = [
            GraphNodePlan(
                node_id=node.node_id,
                agent_id=node.agent_id,
                label=node.label,
                order=index,
                enabled=True,
                agent=node.agent,
                generation=node.generation,
                provider_profile_id=node.provider_profile_id,
                model_id=node.model_id,
                advanced=node.advanced,
            )
            for index, node in enumerate(planned_nodes)
        ]
        output_node_id = graph.get("output_node_id") or graph.get("outputNodeId") or planned_nodes[-1].node_id
        if output_node_id != planned_nodes[-1].node_id:
            raise ValueError("output node must be the final enabled node")
        graph_plan = GraphPlan(resolved_graph_id, str(graph.get("name") or resolved_graph_id), tuple(planned_nodes), output_node_id)
        plan_data = {
            "player_input": player_input,
            "project": _redact_secrets({**project, "id": resolved_project_id}),
            "worldbooks": _redact_secrets(books),
            "graph": _redact_secrets(graph_plan.to_dict()),
        }
        plan_id = _hash(plan_data)
        return ExecutionPlan(
            plan_id=plan_id,
            player_input=player_input,
            project=plan_data["project"],
            worldbooks=tuple(plan_data["worldbooks"]),
            graph=graph_plan,
            created_at=int(time.time()),
        )

    @staticmethod
    def _apply_node_overrides(agent: ResolvedAgent, node: Mapping[str, Any]) -> ResolvedAgent:
        generation = _copy(dict(agent.generation))
        node_generation = node.get("generation") or node.get("generation_override") or {}
        if isinstance(node_generation, Mapping):
            generation.update(_copy(dict(node_generation)))
        advanced = _copy(dict(agent.advanced))
        node_advanced = node.get("advanced") or node.get("advanced_override") or {}
        if isinstance(node_advanced, Mapping):
            advanced.update(_copy(dict(node_advanced)))
        provider_profile_id = node.get("provider_profile_id") or agent.provider_profile_id
        model_id = node.get("model_id") or node.get("model") or agent.model_id
        effective = _copy(dict(agent.effective_config))
        effective["provider_profile_id"] = provider_profile_id
        effective["model_id"] = model_id
        effective["model"] = model_id
        effective["generation"] = _copy(generation)
        effective["parameters"] = _copy(generation)
        effective["advanced"] = _copy(advanced)
        for key, value in generation.items():
            effective[key] = _copy(value)
        for key, value in advanced.items():
            effective[key] = _copy(value)
        return ResolvedAgent(
            agent_id=agent.agent_id,
            name=agent.name,
            instruction=agent.instruction,
            prompt_preset_id=agent.prompt_preset_id,
            provider_profile_id=provider_profile_id,
            model_id=model_id,
            generation=generation,
            advanced=advanced,
            tool_allowlist=agent.tool_allowlist,
            prompt=agent.prompt,
            prompt_provenance=agent.prompt_provenance,
            effective_config=effective,
        )

    def _resolve_project(self, project, project_id):
        if isinstance(project, Mapping):
            return _copy(dict(project))
        resolved_id = project if isinstance(project, str) else project_id
        if self.project_store is None or not resolved_id:
            return {"id": str(resolved_id or "project"), "graph_id": None, "worldbook_ids": []}
        return _copy(self.project_store.get_project(resolved_id))

    def _resolve_graph(self, graph, graph_id):
        if isinstance(graph, Mapping):
            return _copy(dict(graph))
        resolved_id = graph if isinstance(graph, str) else graph_id
        if self.graph_store is None or not resolved_id:
            raise ValueError("Graph Definition is required")
        return _copy(self.graph_store.get_graph(resolved_id))

    def _resolve_agents(self, agents):
        if agents is None:
            return {}
        if isinstance(agents, Mapping):
            return {str(key): _copy(dict(value)) for key, value in agents.items()}
        return {
            str(item.get("agent_id") or item.get("id")): _copy(dict(item))
            for item in agents
            if isinstance(item, Mapping) and (item.get("agent_id") or item.get("id"))
        }

    def _resolve_worldbooks(self, worldbooks, project):
        if worldbooks is not None:
            return _copy(list(worldbooks))
        if self.worldbook_store is None:
            return []
        project_id = project.get("id") or project.get("project_id")
        if hasattr(self.worldbook_store, "effective_worldbooks") and project_id:
            return _copy(self.worldbook_store.effective_worldbooks(project_id))
        ids = project.get("worldbook_ids") or project.get("worldbook_bindings") or []
        return [_copy(self.worldbook_store.get_worldbook(item)) for item in ids]

    def _preview_agent(self, definition, player_input):
        if self.agent_store is not None and hasattr(self.agent_store, "preview_agent"):
            agent_id = definition.get("agent_id", definition.get("id"))
            try:
                preview = self.agent_store.preview_agent(
                    agent_id,
                    {
                        "project_input": player_input,
                        "output_contract": {"kind": "narrative_draft", "content_type": "text/plain"},
                    },
                )
                if isinstance(preview, Mapping) and isinstance(preview.get("preview"), Mapping):
                    preview = preview["preview"]
                if isinstance(preview, Mapping):
                    return _copy(dict(preview))
            except Exception:
                # A definition snapshot remains usable even when its optional
                # preset source is unavailable; the frozen fallback still records
                # instruction/configuration/tools for the run.
                pass
        return {}


class NodeRunner(Protocol):
    """Provider-independent execution seam for one resolved node."""

    def run(self, node: GraphNodePlan, input_artifact: AgentArtifact) -> NodeResult:
        ...


@dataclass(frozen=True)
class NodeRunOutcome:
    node_id: str
    result: NodeResult


@dataclass(frozen=True)
class GraphRunResult:
    status: str
    plan_id: str
    output_artifact: AgentArtifact | None
    node_results: tuple[NodeRunOutcome, ...]
    failed_node_id: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "plan_id": self.plan_id,
            "output_artifact": self.output_artifact.to_dict() if self.output_artifact else None,
            "node_results": [
                {"node_id": item.node_id, "result": item.result.to_dict()}
                for item in self.node_results
            ],
            "failed_node_id": self.failed_node_id,
        }


class GraphRuntime:
    """Schedule enabled nodes and pass only typed Artifacts between them."""

    def __init__(self, node_runner: NodeRunner):
        if node_runner is None or not callable(getattr(node_runner, "run", None)):
            raise TypeError("GraphRuntime requires a NodeRunner")
        self.node_runner = node_runner

    def run(
        self,
        plan: ExecutionPlan,
        initial_artifact: AgentArtifact | None = None,
    ) -> GraphRunResult:
        current = initial_artifact or AgentArtifact.input(plan.player_input)
        outcomes: list[NodeRunOutcome] = []
        for node in plan.graph.nodes:
            if not node.enabled:
                continue
            try:
                result = self.node_runner.run(node, current)
            except Exception as exc:  # Runner failures are Graph failures.
                result = NodeResult.failed(str(exc))
            if isinstance(result, AgentArtifact):
                result = NodeResult.succeeded(result)
            if not isinstance(result, NodeResult):
                result = NodeResult.failed("Node Runner returned an invalid Node Result")
            outcomes.append(NodeRunOutcome(node.node_id, result))
            if not result.ok:
                return GraphRunResult("failed", plan.plan_id, None, tuple(outcomes), node.node_id)
            current = result.primary_artifact
        output = next(
            (
                outcome.result.primary_artifact
                for outcome in outcomes
                if outcome.node_id == plan.graph.output_node_id and outcome.result.ok
            ),
            None,
        )
        if output is None:
            return GraphRunResult("failed", plan.plan_id, None, tuple(outcomes), plan.graph.output_node_id)
        return GraphRunResult("succeeded", plan.plan_id, output, tuple(outcomes))


class GraphExecutionError(RuntimeError):
    """Raised when a Graph Run cannot produce its output Artifact."""

    def __init__(self, result: GraphRunResult):
        self.result = result
        detail = result.failed_node_id or "unknown node"
        super().__init__(f"Graph Run failed at {detail}")


class GraphRuntimeExecutor:
    """Adapter that feeds a Graph output Artifact into the legacy TurnDraft seam."""

    def __init__(self, graph_runtime: GraphRuntime, plan: ExecutionPlan):
        self.graph_runtime = graph_runtime
        self.plan = plan

    def run(self, text: str, compiled_context=None):
        del compiled_context
        result = self.graph_runtime.run(self.plan, AgentArtifact.input(text))
        if not result.ok or result.output_artifact is None:
            raise GraphExecutionError(result)
        content = result.output_artifact.content
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)
        from engine.turn_parser import parse_turn_text

        return parse_turn_text(content, fallback_input=text)
