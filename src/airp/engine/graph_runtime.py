"""Execution plans and provider-independent linear Graph Runtime.

The runtime in this module only knows ordered nodes, immutable Artifacts and
Node Results.  Provider/model execution belongs behind the ``NodeRunner``
boundary supplied by callers.
"""

from __future__ import annotations

import copy
import hashlib
import inspect
import json
import time
import uuid
from dataclasses import dataclass, field, replace
from collections.abc import Callable
from typing import Any, Mapping, Protocol

from airp.engine.macros import build_context, expand_template


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


def _notify(observer: Any, method: str, *args: Any) -> None:
    """Notify an optional observer without coupling Graph Runtime to storage."""
    callback = getattr(observer, method, None) if observer is not None else None
    if not callable(callback):
        return
    try:
        callback(*args)
    except Exception:
        # Observability must never change graph execution semantics.
        return


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
        kind: str = "text",
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
            kind=str(payload.get("kind") or "text"),
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
class NodeExecutionContext:
    """Host capabilities supplied for one Graph Run.

    Graph Runtime does not know what a tool means or where it is persisted.
    The host may provide a node-aware dispatcher so each Agent Definition's
    allowlist is enforced without making the provider runner depend on RP
    Session or Worldbook modules.
    """

    tool_handler: Callable[["GraphNodePlan", str, dict[str, Any]], Any] | None = None
    tool_registry: Any | None = None
    skill_catalog: Mapping[str, Any] | None = None
    macro_context: Mapping[str, Any] | None = None
    abort_signal: Any | None = None
    # A Graph Run is one ephemeral collaboration. Executors may use this key
    # for private in-memory Agent transcripts, but must discard it on close.
    execution_id: str | None = None


@dataclass(frozen=True)
class ResolvedAgent:
    """Definition data needed by a single planned node."""

    agent_id: str
    name: str
    instruction: str
    provider_profile_id: str | None
    model_id: str | None
    generation: Mapping[str, Any]
    advanced: Mapping[str, Any]
    tool_allowlist: tuple[str, ...]
    prompt: tuple[Mapping[str, Any], ...]
    prompt_provenance: tuple[Mapping[str, Any], ...]
    effective_config: Mapping[str, Any]
    regex_collection_id: str | None = None

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
        else:
            effective = _copy(dict(effective))
        regex_collection = definition.get("regex_collection")
        if isinstance(regex_collection, Mapping):
            effective["regex_collection"] = _copy(dict(regex_collection))
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
            provider_profile_id=definition.get("provider_profile_id"),
            model_id=definition.get("model_id"),
            generation=_copy(definition.get("generation") or {}),
            advanced=_redact_secrets(definition.get("advanced") or {}),
            tool_allowlist=tuple(str(item) for item in tool_allowlist),
            prompt=tuple(_redact_secrets(item) for item in raw_prompt if isinstance(item, Mapping)),
            prompt_provenance=tuple(_redact_secrets(item) for item in raw_provenance if isinstance(item, Mapping)),
            effective_config=_redact_secrets(dict(effective)),
            regex_collection_id=definition.get("regex_collection_id"),
        )

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ResolvedAgent":
        return cls(
            agent_id=str(payload.get("agent_id") or ""),
            name=str(payload.get("name") or payload.get("agent_id") or "Agent"),
            instruction=str(payload.get("instruction") or ""),
            provider_profile_id=payload.get("provider_profile_id"),
            model_id=payload.get("model_id"),
            generation=_copy(payload.get("generation") or {}),
            advanced=_redact_secrets(payload.get("advanced") or {}),
            tool_allowlist=tuple(str(item) for item in payload.get("tool_allowlist") or []),
            prompt=tuple(_redact_secrets(item) for item in payload.get("prompt") or []),
            prompt_provenance=tuple(_redact_secrets(item) for item in payload.get("prompt_provenance") or []),
            effective_config=_redact_secrets(payload.get("effective_config") or {}),
            regex_collection_id=payload.get("regex_collection_id"),
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "agent_id": self.agent_id,
            "name": self.name,
            "instruction": self.instruction,
            "provider_profile_id": self.provider_profile_id,
            "model_id": self.model_id,
            "generation": _copy(dict(self.generation)),
            "advanced": _copy(dict(self.advanced)),
            "tool_allowlist": list(self.tool_allowlist),
            "prompt": [_copy(item) for item in self.prompt],
            "prompt_provenance": [_copy(item) for item in self.prompt_provenance],
            "effective_config": _copy(dict(self.effective_config)),
        }
        if self.regex_collection_id is not None:
            payload["regex_collection_id"] = self.regex_collection_id
        return payload


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
    handoff_prompt: str = ""
    source_node_id: str | None = None
    loop_id: str | None = None
    loop_iteration: int | None = None

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
            handoff_prompt=str(payload.get("handoff_prompt") or ""),
            source_node_id=payload.get("source_node_id"),
            loop_id=payload.get("loop_id"),
            loop_iteration=payload.get("loop_iteration"),
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
            "handoff_prompt": self.handoff_prompt,
            "source_node_id": self.source_node_id or self.node_id,
            "loop_id": self.loop_id,
            "loop_iteration": self.loop_iteration,
        }


@dataclass(frozen=True)
class GraphPlan:
    graph_id: str
    name: str
    nodes: tuple[GraphNodePlan, ...]
    output_node_id: str
    revision: int = 0
    mode: str = "sequential"

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "GraphPlan":
        return cls(
            graph_id=str(payload.get("graph_id") or payload.get("id") or ""),
            name=str(payload.get("name") or payload.get("graph_id") or "Graph"),
            nodes=tuple(GraphNodePlan.from_dict(item) for item in payload.get("nodes") or []),
            output_node_id=str(payload.get("output_node_id") or ""),
            revision=int(payload.get("revision") or 0),
            mode=str(payload.get("mode") or "sequential"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "graph_id": self.graph_id,
            "id": self.graph_id,
            "name": self.name,
            "mode": self.mode,
            "nodes": [node.to_dict() for node in self.nodes],
            "output_node_id": self.output_node_id,
            "revision": self.revision,
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
    provenance: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "project", _copy(dict(self.project)))
        object.__setattr__(self, "worldbooks", tuple(_copy(dict(book)) for book in self.worldbooks))
        object.__setattr__(self, "provenance", _copy(dict(self.provenance)))

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ExecutionPlan":
        return cls(
            plan_id=str(payload.get("plan_id") or ""),
            player_input=str(payload.get("player_input") or ""),
            project=payload.get("project") or {},
            worldbooks=tuple(payload.get("worldbooks") or []),
            graph=GraphPlan.from_dict(payload.get("graph") or {}),
            created_at=int(payload.get("created_at") or 0),
            provenance=payload.get("provenance") or {},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "player_input": self.player_input,
            "project": _copy(dict(self.project)),
            "worldbooks": [_copy(dict(book)) for book in self.worldbooks],
            "graph": self.graph.to_dict(),
            "created_at": self.created_at,
            "provenance": _copy(dict(self.provenance)),
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
        regex_collection_store=None,
        provider_profile_store=None,
    ) -> None:
        self.agent_store = agent_store
        self.graph_store = graph_store
        self.project_store = project_store
        self.worldbook_store = worldbook_store
        self.regex_collection_store = regex_collection_store
        self.provider_profile_store = provider_profile_store

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
        context: Mapping[str, Any] | None = None,
    ) -> ExecutionPlan:
        project = self._resolve_project(project, project_id)
        resolved_project_id = str(project.get("id") or project.get("project_id") or project_id or "project")
        # Graph activation is runtime state, not Project content. Callers must
        # explicitly provide the selected Graph (or its id) for every run.
        graph = self._resolve_graph(graph, graph_id)
        resolved_graph_id = str(graph.get("id") or graph.get("graph_id") or graph_id or "graph")
        agents_by_id = self._resolve_agents(agents)
        books = self._resolve_worldbooks(worldbooks, project)

        raw_nodes = graph.get("nodes")
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise ValueError("Execution Plan requires a non-empty graph")
        planned_nodes: list[GraphNodePlan] = []
        seen: set[str] = set()
        agent_sources: dict[str, dict[str, Any]] = {}
        provider_ids: set[str] = set()
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
            definition = self._freeze_regex_collection(definition)
            safe_definition = _redact_secrets(definition)
            agent_sources[agent_id] = {
                "id": agent_id,
                "revision": int(definition.get("revision") or 0),
                "content_hash": _hash(safe_definition),
            }
            preview = self._preview_agent(definition, player_input, context=context)
            resolved_agent = self._apply_node_overrides(
                ResolvedAgent.from_definition(definition, preview),
                raw_node,
            )
            if resolved_agent.provider_profile_id:
                provider_ids.add(str(resolved_agent.provider_profile_id))
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
                    handoff_prompt=self._handoff_prompt(raw_node),
                    source_node_id=node_id,
                )
            )
        planned_nodes.sort(key=lambda node: node.order)
        planned_nodes = self._reindex_nodes(planned_nodes)
        output_source_node_id = graph.get("output_node_id") or graph.get("outputNodeId") or planned_nodes[-1].node_id
        if output_source_node_id != planned_nodes[-1].node_id:
            raise ValueError("output node must be the final enabled node")
        mode = str(graph.get("mode") or "sequential")
        if mode not in {"sequential", "handoff"}:
            raise ValueError("unsupported graph mode")
        if mode == "handoff":
            planned_nodes = self._expand_handoff_loops(planned_nodes, graph.get("loops") or [])
        planned_nodes = self._reindex_nodes(planned_nodes)
        output_node_id = next(
            (
                node.node_id
                for node in reversed(planned_nodes)
                if (node.source_node_id or node.node_id) == output_source_node_id
            ),
            "",
        )
        if not output_node_id:
            raise ValueError("output node was not expanded")
        graph_plan = GraphPlan(
            resolved_graph_id,
            str(graph.get("name") or resolved_graph_id),
            tuple(planned_nodes),
            output_node_id,
            revision=int(graph.get("revision") or 0),
            mode=mode,
        )
        provider_sources = []
        if self.provider_profile_store is not None:
            for provider_id in sorted(provider_ids):
                try:
                    profile = self.provider_profile_store.get_profile(provider_id)
                except Exception:
                    continue
                safe_profile = _redact_secrets(profile)
                provider_sources.append({
                    "id": provider_id,
                    "revision": int(profile.get("revision") or 0),
                    "content_hash": _hash(safe_profile),
                })
        worldbook_sources = [
            {
                "id": str(book.get("id") or ""),
                "revision": int(book.get("revision") or 0),
                "content_hash": _hash(_redact_secrets(book)),
            }
            for book in books
            if isinstance(book, Mapping)
        ]
        provenance = {
            "schema": {"id": "airp.config-provenance", "version": 1},
            "project": {
                "id": resolved_project_id,
                "revision": int(project.get("revision") or 0),
                "content_hash": _hash(_redact_secrets(project)),
            },
            "graph": {
                "id": resolved_graph_id,
                "revision": int(graph.get("revision") or 0),
                "content_hash": _hash(_redact_secrets(graph)),
            },
            "agents": sorted(agent_sources.values(), key=lambda item: item["id"]),
            "providers": provider_sources,
            "worldbooks": worldbook_sources,
        }
        plan_data = {
            "player_input": player_input,
            "project": _redact_secrets({**project, "id": resolved_project_id}),
            "worldbooks": _redact_secrets(books),
            "graph": _redact_secrets(graph_plan.to_dict()),
            "provenance": provenance,
        }
        plan_id = _hash(plan_data)
        return ExecutionPlan(
            plan_id=plan_id,
            player_input=player_input,
            project=plan_data["project"],
            worldbooks=tuple(plan_data["worldbooks"]),
            graph=graph_plan,
            created_at=int(time.time()),
            provenance=provenance,
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
            provider_profile_id=provider_profile_id,
            model_id=model_id,
            generation=generation,
            advanced=advanced,
            tool_allowlist=agent.tool_allowlist,
            prompt=agent.prompt,
            prompt_provenance=agent.prompt_provenance,
            effective_config=effective,
            regex_collection_id=agent.regex_collection_id,
        )

    @staticmethod
    def _handoff_prompt(node: Mapping[str, Any]) -> str:
        value = node.get("handoff_prompt", node.get("handoff"))
        if value is None:
            return ""
        if not isinstance(value, str):
            raise ValueError("handoff_prompt must be text")
        return value

    @staticmethod
    def _reindex_nodes(nodes: list[GraphNodePlan]) -> list[GraphNodePlan]:
        return [replace(node, order=index) for index, node in enumerate(nodes)]

    @staticmethod
    def _expand_handoff_loops(
        nodes: list[GraphNodePlan],
        raw_loops: Any,
    ) -> list[GraphNodePlan]:
        if not isinstance(raw_loops, list):
            raise ValueError("handoff loops must be an array")
        if not raw_loops:
            return list(nodes)

        node_index = {node.node_id: index for index, node in enumerate(nodes)}
        loops_by_start: dict[int, tuple[str, int, int, str | None]] = {}
        covered: set[int] = set()
        for ordinal, raw_loop in enumerate(raw_loops, start=1):
            if not isinstance(raw_loop, Mapping):
                raise ValueError("handoff loop must be an object")
            loop_id = raw_loop.get("id") or raw_loop.get("loop_id") or f"loop-{ordinal}"
            if not isinstance(loop_id, str) or not loop_id:
                raise ValueError("handoff loop id must be text")
            if raw_loop.get("mode", "fixed") != "fixed":
                raise ValueError("only fixed handoff loops are supported")
            iterations = raw_loop.get("iterations", raw_loop.get("count"))
            if not isinstance(iterations, int) or isinstance(iterations, bool) or not 1 <= iterations <= 100:
                raise ValueError("handoff loop iterations must be between 1 and 100")
            start_id = raw_loop.get("start_node_id", raw_loop.get("start"))
            end_id = raw_loop.get("end_node_id", raw_loop.get("end"))
            if start_id not in node_index or end_id not in node_index:
                raise ValueError("handoff loop must reference graph nodes")
            start = node_index[start_id]
            end = node_index[end_id]
            if start > end or end >= len(nodes) - 1:
                raise ValueError("handoff loop requires a contiguous segment and an exit node")
            if any(index in covered for index in range(start, end + 1)):
                raise ValueError("handoff loops cannot overlap")
            covered.update(range(start, end + 1))
            exit_prompt = raw_loop.get("exit_handoff_prompt")
            if exit_prompt is not None and not isinstance(exit_prompt, str):
                raise ValueError("exit_handoff_prompt must be text")
            loops_by_start[start] = (loop_id, end, iterations, exit_prompt)

        expanded: list[GraphNodePlan] = []
        generated_ids: set[str] = set()
        index = 0
        while index < len(nodes):
            loop = loops_by_start.get(index)
            if loop is None:
                expanded.append(nodes[index])
                index += 1
                continue
            loop_id, end, iterations, exit_prompt = loop
            segment = nodes[index : end + 1]
            for iteration in range(1, iterations + 1):
                for segment_index, source in enumerate(segment):
                    handoff_prompt = source.handoff_prompt
                    if iteration == iterations and segment_index == len(segment) - 1 and exit_prompt is not None:
                        handoff_prompt = exit_prompt
                    generated_id = f"{source.node_id}.loop{iteration}"
                    if generated_id in node_index or generated_id in generated_ids:
                        raise ValueError(
                            f"handoff loop generated node id {generated_id!r} conflicts with a source node id"
                        )
                    generated_ids.add(generated_id)
                    expanded.append(
                        replace(
                            source,
                            node_id=generated_id,
                            handoff_prompt=handoff_prompt,
                            source_node_id=source.source_node_id or source.node_id,
                            loop_id=loop_id,
                            loop_iteration=iteration,
                        )
                    )
            index = end + 1
        return expanded

    def _freeze_regex_collection(self, definition: Mapping[str, Any]) -> dict[str, Any]:
        snapshot = _copy(dict(definition))
        collection_id = snapshot.get("regex_collection_id")
        if not collection_id:
            return snapshot
        if self.regex_collection_store is None or not hasattr(self.regex_collection_store, "get_collection"):
            raise ValueError(
                f"Regex Collection store is required for Agent Definition {snapshot.get('agent_id') or snapshot.get('id')!r}"
            )
        try:
            collection = self.regex_collection_store.get_collection(collection_id)
        except Exception as exc:
            raise ValueError(
                f"Regex Collection {collection_id!r} for Agent Definition "
                f"{snapshot.get('agent_id') or snapshot.get('id')!r} was not found"
            ) from exc
        if not isinstance(collection, Mapping):
            raise ValueError(f"Regex Collection {collection_id!r} must be an object")
        snapshot["regex_collection"] = _copy(dict(collection))
        return snapshot

    def _resolve_project(self, project, project_id):
        if isinstance(project, Mapping):
            return _copy(dict(project))
        resolved_id = project if isinstance(project, str) else project_id
        if self.project_store is None or not resolved_id:
            return {"id": str(resolved_id or "project"), "worldbook_ids": []}
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

    def _preview_agent(self, definition, player_input, *, context=None):
        if self.agent_store is not None and hasattr(self.agent_store, "preview_agent"):
            agent_id = definition.get("agent_id", definition.get("id"))
            try:
                preview = self.agent_store.preview_agent(
                    agent_id,
                    {
                        "project_input": player_input,
                        "context": _copy(context or {}),
                },
                )
                if isinstance(preview, Mapping) and isinstance(preview.get("preview"), Mapping):
                    preview = preview["preview"]
                if isinstance(preview, Mapping):
                    return _copy(dict(preview))
            except Exception:
                # A definition snapshot remains usable when preview expansion
                # is unavailable; the frozen fallback still records its
                # instruction, configuration, and tools for the run.
                pass
        return {}


class AgentExecutor(Protocol):
    """Provider-independent execution seam for one resolved Agent node.

    Executors may retain state only for a supplied ``execution_id``. Graph
    Runtime owns that lifetime and calls ``close_execution`` when a Graph Run
    completes, fails, or is cancelled.
    """

    def run(
        self,
        node: GraphNodePlan,
        input_artifact: AgentArtifact,
        *,
        observer: Any = None,
        execution_context: NodeExecutionContext | None = None,
    ) -> NodeResult:
        ...


# ``NodeRunner`` remains the public compatibility name for existing adapters
# and tests. New production executors use the more accurate AgentExecutor
# language because a node can now own a temporary multi-turn Agent session.
NodeRunner = AgentExecutor


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

    @property
    def error(self) -> Any:
        """Return the failed node's structured error, if one exists.

        Keeping this lookup on the provider-independent result lets host
        runtimes classify retryable provider failures without reaching into a
        concrete NodeRunner or losing the original error at the graph seam.
        """
        if self.ok or not self.failed_node_id:
            return None
        for outcome in reversed(self.node_results):
            if outcome.node_id == self.failed_node_id:
                return _copy(outcome.result.error)
        return None

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
            "error": self.error,
        }


class GraphRuntime:
    """Schedule enabled nodes and pass only typed Artifacts between them."""

    def __init__(self, node_runner: AgentExecutor):
        if node_runner is None or not callable(getattr(node_runner, "run", None)):
            raise TypeError("GraphRuntime requires a NodeRunner")
        self.node_runner = node_runner

    def run(
        self,
        plan: ExecutionPlan,
        initial_artifact: AgentArtifact | None = None,
        *,
        observer: Any = None,
        execution_context: NodeExecutionContext | None = None,
    ) -> GraphRunResult:
        context = execution_context or NodeExecutionContext()
        if not context.execution_id:
            context = replace(context, execution_id=str(uuid.uuid4()))
        current = initial_artifact or AgentArtifact.input(plan.player_input)
        outcomes: list[NodeRunOutcome] = []
        _notify(observer, "graph_started", plan)
        try:
            for node in plan.graph.nodes:
                if not node.enabled:
                    continue
                _notify(observer, "node_started", node, current)
                try:
                    result = self._run_node(node, current, observer, context)
                except Exception as exc:  # Runner failures are Graph failures.
                    result = NodeResult.failed(str(exc))
                if isinstance(result, AgentArtifact):
                    result = NodeResult.succeeded(result)
                if not isinstance(result, NodeResult):
                    result = NodeResult.failed("Node Runner returned an invalid Node Result")
                artifact = result.primary_artifact
                if result.ok and artifact is None:
                    result = NodeResult.failed("Node Runner succeeded without a primary Artifact")
                outcomes.append(NodeRunOutcome(node.node_id, result))
                _notify(observer, "node_finished", node, current, result)
                if not result.ok:
                    graph_result = GraphRunResult("failed", plan.plan_id, None, tuple(outcomes), node.node_id)
                    _notify(observer, "graph_finished", graph_result)
                    return graph_result
                assert artifact is not None
                next_node = next(
                    (
                        candidate
                        for candidate in plan.graph.nodes[node.order + 1 :]
                        if candidate.enabled
                    ),
                    None,
                )
                current = self._handoff_artifact(
                    plan,
                    node,
                    next_node,
                    artifact,
                    context,
                    observer,
                )
            output = next(
                (
                    outcome.result.primary_artifact
                    for outcome in outcomes
                    if outcome.node_id == plan.graph.output_node_id and outcome.result.ok
                ),
                None,
            )
            if output is None:
                graph_result = GraphRunResult("failed", plan.plan_id, None, tuple(outcomes), plan.graph.output_node_id)
                _notify(observer, "graph_finished", graph_result)
                return graph_result
            graph_result = GraphRunResult("succeeded", plan.plan_id, output, tuple(outcomes))
            _notify(observer, "graph_finished", graph_result)
            return graph_result
        finally:
            self._close_execution(context.execution_id)

    def _run_node(
        self,
        node: GraphNodePlan,
        input_artifact: AgentArtifact,
        observer: Any,
        execution_context: NodeExecutionContext | None,
    ) -> NodeResult:
        runner = self.node_runner.run
        try:
            parameters = inspect.signature(runner).parameters
        except (TypeError, ValueError):
            parameters = {}
        kwargs = {}
        if "observer" in parameters:
            kwargs["observer"] = observer
        if "execution_context" in parameters:
            kwargs["execution_context"] = execution_context
        return runner(node, input_artifact, **kwargs)

    def _close_execution(self, execution_id: str | None) -> None:
        if not execution_id:
            return
        close = getattr(self.node_runner, "close_execution", None)
        if callable(close):
            try:
                close(execution_id)
            except Exception:
                # Cleanup cannot overwrite the result that was already produced.
                return

    @staticmethod
    def _handoff_artifact(
        plan: ExecutionPlan,
        source: GraphNodePlan,
        target: GraphNodePlan | None,
        artifact: AgentArtifact,
        execution_context: NodeExecutionContext | None,
        observer: Any,
    ) -> AgentArtifact:
        if target is None:
            return artifact
        content = artifact.content if isinstance(artifact.content, str) else _canonical(artifact.content)
        template = source.handoff_prompt
        # Legacy sequential graphs preserve raw Artifacts when no prompt was
        # configured. A handoff graph always materialises the connection so
        # Monitor can show a direct handoff even with an empty prompt.
        if not template and plan.graph.mode != "handoff":
            return artifact
        macro_context = build_context(
            execution_context.macro_context if execution_context is not None else None,
            {
                "project": plan.project,
                "project_id": plan.project.get("id") if isinstance(plan.project, Mapping) else "",
                "player_input": plan.player_input,
                "project_input": plan.player_input,
                "worldbooks": list(plan.worldbooks),
                "graph": plan.graph.to_dict(),
                "node": source.to_dict(),
                "next_node": target.to_dict(),
                "handoff": content,
                "node_input": content,
            },
        )
        prompt = expand_template(template, macro_context, preserve_unknown=True) if template else ""
        handoff_content = f"{prompt}\n\n{content}" if prompt else content
        handed_off = AgentArtifact.text(
            handoff_content,
            kind="handoff",
            metadata={
                "source_node_id": source.source_node_id or source.node_id,
                "source_run_node_id": source.node_id,
                "target_node_id": target.source_node_id or target.node_id,
                "loop_id": source.loop_id,
                "loop_iteration": source.loop_iteration,
            },
        )
        _notify(observer, "handoff_prepared", source, target, artifact, handed_off)
        return handed_off


class GraphExecutionError(RuntimeError):
    """Raised when a Graph Run cannot produce its output Artifact."""

    def __init__(self, result: GraphRunResult):
        self.result = result
        detail = result.failed_node_id or "unknown node"
        super().__init__(f"Graph Run failed at {detail}")
