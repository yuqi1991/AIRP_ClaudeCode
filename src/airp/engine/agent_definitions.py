"""Persistent Agent Definition library and prompt-preview compiler.

Agent Definitions are reusable Studio objects.  They own editable model and
prompt configuration, but never acquire execution-role semantics: Graphs own
order and runtime owns input, tools, streaming, tracing, and credentials.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping

from airp.engine.capabilities import provider_parameters
from airp.engine.macros import available_macro_roots, build_context, expand_template
from airp.engine.revisions import append_audit, conflict_payload, expected_revision, revision


AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
GENERATION_FIELDS = (
    "temperature",
    "max_output_tokens",
    "top_p",
    "stop",
    "reasoning_effort",
    "seed",
)

# These values are owned by the runtime/provider boundary.  Advanced JSON is
# intentionally otherwise free-form and is forwarded as top-level parameters.
RUNTIME_OWNED_FIELDS = frozenset(
    {
        "input",
        "messages",
        "prompt",
        "model",
        "model_id",
        "provider",
        "provider_id",
        "provider_profile_id",
        "tools",
        "tool_allowlist",
        "stream",
        "stream_options",
        "api_key",
        "apikey",
        "secret",
        "authorization",
        "auth",
        "authentication",
        "credentials",
        "headers",
        "base_url",
        "trace_id",
        "tracing",
        "metadata",
    }
)


class AgentDefinitionError(ValueError):
    """A user-facing Agent Definition validation or persistence failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        references: list[dict[str, str]] | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.references = references or []
        self.details = details or None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "ok": False,
            "error": self.code,
            "message": str(self),
        }
        if self.references:
            payload["references"] = copy.deepcopy(self.references)
        if self.details is not None:
            payload.update(copy.deepcopy(self.details))
        return payload


class AgentDefinitionStore:
    """File-backed CRUD store with graph-safe deletion and preview support."""

    def __init__(
        self,
        static_root: str | Path,
        *,
        library_root: str | Path | None = None,
        graph_root: str | Path | None = None,
        workspace=None,
        tool_schema_provider: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> None:
        self.static_root = Path(static_root).resolve()
        workspace_agents_root = getattr(workspace, "agents_root", None)
        workspace_graphs_root = getattr(workspace, "graphs_root", None)
        self.library_root = (
            Path(library_root).resolve()
            if library_root is not None
            else (Path(workspace_agents_root).resolve() if workspace_agents_root else self.static_root / "studio" / "agents")
        )
        graph_roots = [
            Path(graph_root).resolve() if graph_root is not None else None,
            Path(workspace_graphs_root).resolve() if workspace_graphs_root else None,
            self.static_root / "studio" / "graphs",
            self.static_root / "graphs",
        ]
        self._graph_roots = tuple(dict.fromkeys(root for root in graph_roots if root is not None))
        # Tool schemas are supplied by the host composition root. The engine
        # stores only the allowlist and falls back to a generic preview.
        self._tool_schema_provider = {
            str(name): copy.deepcopy(dict(schema))
            for name, schema in (tool_schema_provider or {}).items()
            if isinstance(name, str) and isinstance(schema, Mapping)
        }
        self._lock = threading.RLock()

    # ── Definition lifecycle ──────────────────────────────────────────

    def list_agents(self) -> list[dict[str, Any]]:
        with self._lock:
            agents = [self._read_agent(path) for path in self._paths()]
            return sorted(agents, key=lambda item: (item["name"].casefold(), item["agent_id"]))

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        path = self._path_for(agent_id)
        with self._lock:
            if not path.is_file():
                raise AgentDefinitionError(
                    "agent_not_found",
                    f"Agent Definition {agent_id!r} was not found",
                    status=404,
                )
            return self._read_agent(path)

    def create_agent(self, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise AgentDefinitionError("invalid_agent_definition", "Agent Definition must be an object")
        with self._lock:
            agent_id = payload.get("agent_id", payload.get("id")) or f"agent-{uuid.uuid4().hex[:12]}"
            self._validate_id(agent_id)
            if "agent_id" in payload and "id" in payload and payload["agent_id"] != payload["id"]:
                raise AgentDefinitionError("invalid_agent_definition", "agent_id and id must agree")
            path = self._path_for(agent_id)
            if path.exists():
                raise AgentDefinitionError(
                    "agent_exists",
                    f"Agent Definition {agent_id!r} already exists",
                    status=409,
                )
            agent = self._normalize(payload, agent_id=agent_id)
            now = int(time.time())
            agent["created_at"] = now
            agent["updated_at"] = now
            agent["revision"] = 1
            self._write_agent(path, agent)
            append_audit(
                self.library_root, object_type="agent", object_id=agent_id,
                parent_revision=0, new_revision=1, before=None, after=agent,
                source=payload.get("_source", "api"),
            )
            return copy.deepcopy(agent)

    def update_agent(self, agent_id: str, payload: Any) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise AgentDefinitionError("invalid_agent_definition", "Agent Definition must be an object")
        with self._lock:
            path = self._path_for(agent_id)
            if not path.is_file():
                raise AgentDefinitionError(
                    "agent_not_found",
                    f"Agent Definition {agent_id!r} was not found",
                    status=404,
                )
            if "agent_id" in payload and payload["agent_id"] != agent_id:
                raise AgentDefinitionError("agent_id_immutable", "agent_id cannot be changed")
            if "id" in payload and payload["id"] != agent_id:
                raise AgentDefinitionError("agent_id_immutable", "agent_id cannot be changed")
            current = self._read_agent(path)
            expected = expected_revision(payload)
            if expected is not None and expected != current.get("revision", 0):
                raise AgentDefinitionError(
                    "revision_conflict", f"Agent Definition {agent_id!r} revision conflict", status=409,
                    details=conflict_payload("agent", agent_id, expected, current.get("revision", 0), current),
                )
            merged = {**current, **copy.deepcopy(payload)}
            merged["agent_id"] = agent_id
            merged["id"] = agent_id
            # Partial updates to the alias form should still update canonical
            # fields rather than leaving a stale value in the stored object.
            self._apply_aliases(merged, payload)
            agent = self._normalize(merged, agent_id=agent_id)
            agent["created_at"] = current.get("created_at", 0)
            agent["updated_at"] = int(time.time())
            agent["revision"] = current.get("revision", 0) + 1
            self._write_agent(path, agent)
            append_audit(
                self.library_root, object_type="agent", object_id=agent_id,
                parent_revision=current.get("revision", 0), new_revision=agent["revision"],
                before=current, after=agent, source=payload.get("_source", "api"),
            )
            return copy.deepcopy(agent)

    def delete_agent(self, agent_id: str) -> None:
        with self._lock:
            path = self._path_for(agent_id)
            if not path.is_file():
                raise AgentDefinitionError(
                    "agent_not_found",
                    f"Agent Definition {agent_id!r} was not found",
                    status=404,
                )
            references = self._references_for(agent_id)
            if references:
                raise AgentDefinitionError(
                    "agent_in_use",
                    f"Agent Definition {agent_id!r} is still referenced",
                    status=409,
                    references=references,
                )
            path.unlink()

    def copy_agent(self, agent_id: str, payload: Any = None) -> dict[str, Any]:
        if payload is not None and not isinstance(payload, dict):
            raise AgentDefinitionError("invalid_agent_definition", "copy payload must be an object")
        with self._lock:
            original = self.get_agent(agent_id)
            overrides = copy.deepcopy(payload or {})
            overrides.pop("id", None)
            overrides.pop("agent_id", None)
            overrides.pop("created_at", None)
            overrides.pop("updated_at", None)
            overrides["name"] = self._copy_name(original["name"])
            copied = {**original, **overrides}
            copied.pop("id", None)
            copied.pop("agent_id", None)
            copied.pop("created_at", None)
            copied.pop("updated_at", None)
            return self.create_agent(copied)

    def preview_agent(self, agent_id: str, payload: Any = None) -> dict[str, Any]:
        if payload is not None and not isinstance(payload, dict):
            raise AgentDefinitionError("invalid_agent_preview", "preview payload must be an object")
        agent = self.get_agent(agent_id)
        return self._compile_preview(agent, payload or {})

    # ── Normalization ─────────────────────────────────────────────────

    def _normalize(self, payload: dict[str, Any], *, agent_id: str) -> dict[str, Any]:
        self._validate_id(agent_id)
        if "role" in payload or "agent_role" in payload:
            raise AgentDefinitionError(
                "agent_role_removed",
                "Agent Definitions do not have a role; Graphs own execution order",
            )

        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            raise AgentDefinitionError("invalid_agent_definition", "name must be a non-empty string")

        instruction = payload.get("instruction", "")
        if not isinstance(instruction, str):
            raise AgentDefinitionError("invalid_agent_definition", "instruction must be a string")

        provider_payload = payload
        default_provider = payload.get("default_provider")
        if isinstance(default_provider, dict):
            provider_payload = {**default_provider, **payload}
        provider_profile_id = self._first_string(
            provider_payload,
            "provider_profile_id",
            "default_provider_profile_id",
            "default_provider_id",
            "provider_id",
        )
        model_id = self._first_string(
            provider_payload,
            "model_id",
            "default_model_id",
            "default_model",
            "model",
        )
        regex_collection_id = self._first_string(
            payload,
            "regex_collection_id",
            "regex_collection",
        )

        generation = payload.get("generation")
        if generation is None:
            generation = payload.get("generation_config", payload.get("generation_settings", {}))
        if not isinstance(generation, dict):
            raise AgentDefinitionError("invalid_agent_definition", "generation must be an object")
        generation = self._normalize_generation(generation)
        # Accept form/API clients that submit the basic controls at the top
        # level while keeping one canonical nested representation on disk.
        for field in GENERATION_FIELDS:
            if field in payload:
                generation[field] = self._normalize_generation({field: payload[field]})[field]
        if "max_tokens" in payload and "max_output_tokens" not in generation:
            generation["max_output_tokens"] = self._normalize_generation(
                {"max_output_tokens": payload["max_tokens"]}
            )["max_output_tokens"]

        advanced = payload.get("advanced")
        if advanced is None:
            advanced = payload.get(
                "advanced_parameters",
                payload.get("advanced_params", payload.get("advanced_json", {})),
            )
        if not isinstance(advanced, dict):
            raise AgentDefinitionError("invalid_agent_definition", "advanced must be a JSON object")

        tool_allowlist = payload.get("tool_allowlist")
        if tool_allowlist is None:
            tool_allowlist = payload.get(
                "allowed_tools",
                payload.get("tools_allowlist", payload.get("tools", [])),
            )
        if not isinstance(tool_allowlist, list):
            raise AgentDefinitionError("invalid_agent_definition", "tool_allowlist must be an array")
        normalized_tools: list[str] = []
        for tool in tool_allowlist:
            if not isinstance(tool, str) or not tool.strip():
                raise AgentDefinitionError(
                    "invalid_agent_definition",
                    "tool_allowlist must contain non-empty strings",
                )
            tool = tool.strip()
            if tool not in normalized_tools:
                normalized_tools.append(tool)

        normalized = {
            # ``id`` is retained as a read-compatible alias for existing
            # provider/graph scanners; ``agent_id`` is the Studio contract.
            "id": agent_id,
            "agent_id": agent_id,
            "name": name.strip(),
            "instruction": instruction,
            "provider_profile_id": provider_profile_id,
            "model_id": model_id,
            "regex_collection_id": regex_collection_id,
            "generation": copy.deepcopy(generation),
            "advanced": copy.deepcopy(advanced),
            "tool_allowlist": normalized_tools,
            "created_at": self._timestamp(payload.get("created_at")),
            "updated_at": self._timestamp(payload.get("updated_at")),
        }
        return normalized

    @staticmethod
    def _apply_aliases(merged: dict[str, Any], payload: dict[str, Any]) -> None:
        aliases = (
            ("provider_profile_id", "default_provider_profile_id"),
            ("provider_profile_id", "default_provider_id"),
            ("provider_profile_id", "provider_id"),
            ("model_id", "default_model_id"),
            ("model_id", "default_model"),
            ("model_id", "model"),
            ("generation", "generation_config"),
            ("advanced", "advanced_parameters"),
            ("advanced", "advanced_params"),
            ("advanced", "advanced_json"),
            ("tool_allowlist", "allowed_tools"),
            ("tool_allowlist", "tools_allowlist"),
            ("tool_allowlist", "tools"),
        )
        for canonical, alias in aliases:
            if canonical not in payload and alias in payload:
                merged[canonical] = copy.deepcopy(payload[alias])

    @staticmethod
    def _normalize_generation(raw: dict[str, Any]) -> dict[str, Any]:
        normalized: dict[str, Any] = {}
        for key, value in raw.items():
            if value is None or value == "":
                continue
            if key == "temperature" or key == "top_p":
                if not isinstance(value, (int, float)) or isinstance(value, bool):
                    raise AgentDefinitionError("invalid_agent_definition", f"{key} must be a number")
            elif key == "max_output_tokens":
                if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                    raise AgentDefinitionError(
                        "invalid_agent_definition",
                        "max_output_tokens must be a positive integer",
                    )
            elif key == "stop":
                if not isinstance(value, (str, list)):
                    raise AgentDefinitionError(
                        "invalid_agent_definition",
                        "stop must be a string or array",
                    )
                if isinstance(value, list) and any(not isinstance(item, str) for item in value):
                    raise AgentDefinitionError(
                        "invalid_agent_definition",
                        "stop array must contain strings",
                    )
            elif key == "reasoning_effort":
                if not isinstance(value, str):
                    raise AgentDefinitionError(
                        "invalid_agent_definition",
                        "reasoning_effort must be a string",
                    )
            elif key == "seed":
                if not isinstance(value, int) or isinstance(value, bool):
                    raise AgentDefinitionError("invalid_agent_definition", "seed must be an integer")
            normalized[key] = copy.deepcopy(value)
        return normalized

    @staticmethod
    def _first_string(payload: dict[str, Any], *keys: str) -> str | None:
        for key in keys:
            if key not in payload:
                continue
            value = payload[key]
            if value in (None, ""):
                return None
            if not isinstance(value, str) or not value.strip():
                raise AgentDefinitionError("invalid_agent_definition", f"{key} must be a string")
            return value.strip()
        return None

    @staticmethod
    def _timestamp(value: Any) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    @staticmethod
    def _validate_id(agent_id: Any) -> None:
        if not isinstance(agent_id, str) or not AGENT_ID_RE.fullmatch(agent_id):
            raise AgentDefinitionError("invalid_agent_definition", "agent_id must be a safe library identifier")

    # ── Prompt preview helpers ─────────────────────────────────────────

    def _compile_preview(
        self,
        agent: dict[str, Any],
        request: dict[str, Any],
    ) -> dict[str, Any]:
        project_input = request.get(
            "project_input",
            request.get("runtime_input", request.get("player_input", request.get("input"))),
        )
        handoff = request.get("handoff", request.get("upstream_artifact"))
        output_contract = request.get("output_contract")

        tool_protocol = self._tool_protocol(agent["tool_allowlist"])
        runtime_context = request.get("context") if isinstance(request.get("context"), dict) else {}
        macro_context = build_context(runtime_context, request=request)
        instruction = expand_template(agent["instruction"], macro_context, preserve_unknown=True)
        source_specs = [
            ("instruction", "Agent instruction", "system", instruction, bool(instruction)),
            ("project_input", "Project input", "user", project_input, project_input is not None),
            ("handoff", "Agent handoff", "user", handoff, handoff is not None),
            ("tool_protocol", "Tool protocol", "system", tool_protocol, True),
        ]
        if output_contract is not None:
            source_specs.append(
                ("output_contract", "Output contract", "system", output_contract, True)
            )
        provenance: list[dict[str, Any]] = []
        messages: list[dict[str, Any]] = []
        for order, (kind, label, role, content, included) in enumerate(source_specs, start=1):
            record = {
                "order": order,
                "kind": kind,
                "source": kind,
                "label": label,
                "included": included,
                "content": copy.deepcopy(content),
            }
            if kind == "instruction":
                record["template"] = agent["instruction"]
            provenance.append(record)
            if not included:
                continue
            messages.append(
                {
                    "role": role,
                    "content": self._message_content(content),
                    "source": kind,
                    "provenance": copy.deepcopy(record),
                }
            )

        effective_generation = copy.deepcopy(agent["generation"])
        overridden_basic_fields: list[str] = []
        ignored_advanced_fields: list[str] = []
        for key, value in agent["advanced"].items():
            if isinstance(key, str) and key.casefold() in RUNTIME_OWNED_FIELDS:
                ignored_advanced_fields.append(key)
                continue
            if key in effective_generation:
                overridden_basic_fields.append(key)
            effective_generation[key] = copy.deepcopy(value)

        effective_config: dict[str, Any] = {
            "agent_id": agent["agent_id"],
            "provider_profile_id": agent["provider_profile_id"],
            "model_id": agent["model_id"],
            "model": agent["model_id"],
            "generation": copy.deepcopy(effective_generation),
            "parameters": copy.deepcopy(effective_generation),
            "advanced": copy.deepcopy(agent["advanced"]),
            "tool_allowlist": list(agent["tool_allowlist"]),
            "tools": copy.deepcopy(tool_protocol),
            "stream": True,
            "input": copy.deepcopy(project_input),
            "runtime_owned_fields": sorted(RUNTIME_OWNED_FIELDS),
        }
        for runtime_field in ("base_url", "provider", "trace_id"):
            if runtime_field in request:
                effective_config[runtime_field] = copy.deepcopy(request[runtime_field])
        # Provider requests forward advanced JSON at their top level. Exposing
        # those values alongside the nested view makes precedence inspectable.
        for key, value in effective_generation.items():
            effective_config.setdefault(key, copy.deepcopy(value))

        return {
            "agent": copy.deepcopy(agent),
            "messages": messages,
            "compiled_prompt": copy.deepcopy(messages),
            "prompt": copy.deepcopy(messages),
            "provenance": provenance,
            "prompt_provenance": copy.deepcopy(provenance),
            "source_order": [item["kind"] for item in provenance],
            "effective_config": effective_config,
            "overridden_basic_fields": overridden_basic_fields,
            "ignored_advanced_fields": ignored_advanced_fields,
            "tool_protocol": copy.deepcopy(tool_protocol),
            "instruction_template": agent["instruction"],
            "instruction": instruction,
            "available_macros": available_macro_roots(macro_context),
        }

    @staticmethod
    def _message_content(value: Any) -> str:
        if isinstance(value, str):
            return value
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def _tool_protocol(self, tool_names: list[str]) -> list[dict[str, Any]]:
        protocol: list[dict[str, Any]] = []
        for name in tool_names:
            schema = self._tool_schema_provider.get(name)
            if schema is None:
                protocol.append(
                    {
                        "name": name,
                        "description": "Definition-allowlisted tool (schema supplied by the runtime)",
                        "parameters": {"required": [], "optional": [], "types": {}},
                    }
                )
                continue
            protocol.append(
                {
                    "name": name,
                    "description": schema["description"],
                    "parameters": provider_parameters(schema),
                }
            )
        return protocol

    # ── Persistence and references ────────────────────────────────────

    def _references_for(self, agent_id: str) -> list[dict[str, str]]:
        references: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        paths: list[Path] = []
        for root in self._graph_roots:
            if root.is_dir():
                paths.extend(root.rglob("*.json"))
        paths.extend(
            path
            for path in (
                self.static_root / "studio" / "graphs.json",
                self.static_root / "graphs.json",
            )
            if path.is_file()
        )
        for path in sorted(set(paths)):
            try:
                raw = self._read_json(path)
            except AgentDefinitionError:
                continue
            candidates = raw.get("graphs") if isinstance(raw, dict) and isinstance(raw.get("graphs"), list) else raw
            if isinstance(candidates, dict):
                candidates = [candidates]
            if not isinstance(candidates, list):
                candidates = [raw]
            for graph in candidates:
                if not isinstance(graph, dict):
                    continue
                graph_id = graph.get("graph_id") or graph.get("id") or path.stem
                if not isinstance(graph_id, str):
                    graph_id = path.stem
                name = graph.get("name") if isinstance(graph.get("name"), str) else graph_id
                nodes = graph.get("nodes") if isinstance(graph.get("nodes"), list) else [graph]
                if not any(self._node_references_agent(node, agent_id) for node in nodes):
                    continue
                key = (graph_id, str(path))
                if key in seen:
                    continue
                seen.add(key)
                references.append({"type": "graph", "id": graph_id, "name": name})
        return references

    @staticmethod
    def _node_references_agent(node: Any, agent_id: str) -> bool:
        if not isinstance(node, dict):
            return False
        for key in ("agent_id", "agent_definition_id", "agentId", "agentDefinitionId"):
            if node.get(key) == agent_id:
                return True
        for key in ("agent", "agent_definition"):
            value = node.get(key)
            if isinstance(value, str) and value == agent_id:
                return True
            if isinstance(value, dict) and (value.get("agent_id") == agent_id or value.get("id") == agent_id):
                return True
        values = node.get("agent_ids")
        return isinstance(values, list) and agent_id in values

    def _copy_name(self, name: str) -> str:
        names = {agent["name"].casefold() for agent in self.list_agents()}
        base = f"{name}-copy"
        candidate = base
        index = 2
        while candidate.casefold() in names:
            candidate = f"{base}-{index}"
            index += 1
        return candidate

    def _paths(self) -> list[Path]:
        if not self.library_root.is_dir():
            return []
        return [path for path in self.library_root.glob("*.json") if path.is_file()]

    def _path_for(self, agent_id: str) -> Path:
        self._validate_id(agent_id)
        self.library_root.mkdir(parents=True, exist_ok=True)
        path = (self.library_root / f"{agent_id}.json").resolve()
        try:
            path.relative_to(self.library_root)
        except ValueError as exc:
            raise AgentDefinitionError("invalid_agent_definition", "invalid Agent Definition id") from exc
        return path

    def _read_agent(self, path: Path) -> dict[str, Any]:
        raw = self._read_json(path)
        if not isinstance(raw, dict):
            raise AgentDefinitionError(
                "invalid_agent_definition",
                f"Agent Definition {path.stem!r} is not an object",
            )
        agent_id = raw.get("agent_id", raw.get("id", path.stem))
        return self._normalize(raw, agent_id=agent_id) | {"revision": revision(raw.get("revision"), 0)}

    @staticmethod
    def _read_json(path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AgentDefinitionError(
                "invalid_agent_definition",
                f"cannot read JSON object {path.stem!r}",
            ) from exc

    def _write_agent(self, path: Path, agent: dict[str, Any]) -> None:
        self.library_root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.stem}.", suffix=".tmp", dir=self.library_root
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(agent, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

class AgentDefinitionService:
    """Thin public service facade used by the Runtime Studio HTTP adapter."""

    def __init__(self, store: AgentDefinitionStore) -> None:
        self._store = store

    def list_agents(self) -> list[dict[str, Any]]:
        return self._store.list_agents()

    def get_agent(self, agent_id: str) -> dict[str, Any]:
        return self._store.get_agent(agent_id)

    def create_agent(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"agent": self._store.create_agent(payload)}

    def update_agent(self, agent_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"agent": self._store.update_agent(agent_id, payload)}

    def copy_agent(self, agent_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"agent": self._store.copy_agent(agent_id, payload)}

    def delete_agent(self, agent_id: str) -> None:
        self._store.delete_agent(agent_id)

    def preview_agent(self, agent_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"preview": self._store.preview_agent(agent_id, payload)}
