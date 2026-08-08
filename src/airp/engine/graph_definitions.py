"""Persistent linear Graph Definitions for the Agent Studio.

Graph Definitions are configuration objects.  They reference reusable Agent
Definitions and never contain execution state; a run gets a separate immutable
Execution Plan from :mod:`engine.graph_runtime`.
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
from typing import Any

from airp.engine.revisions import append_audit, conflict_payload, expected_revision, revision


GRAPH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
NODE_ID_RE = GRAPH_ID_RE


class GraphDefinitionError(ValueError):
    """A user-facing Graph Definition validation or persistence failure."""

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


class GraphDefinitionStore:
    """File-backed CRUD store for reusable linear Graph Definitions."""

    def __init__(
        self,
        static_root: str | Path,
        *,
        library_root: str | Path | None = None,
        agent_store=None,
        workspace=None,
    ) -> None:
        self.static_root = Path(static_root).resolve()
        workspace_graphs_root = getattr(workspace, "graphs_root", None)
        self.library_root = (
            Path(library_root).resolve()
            if library_root is not None
            else (Path(workspace_graphs_root).resolve() if workspace_graphs_root else self.static_root / "studio" / "graphs")
        )
        self.agent_store = agent_store
        self._lock = threading.RLock()

    def list_graphs(self) -> list[dict[str, Any]]:
        with self._lock:
            graphs = [self._read_graph(path) for path in self._paths()]
        return sorted(graphs, key=lambda item: (item["name"].casefold(), item["id"]))

    def get_graph(self, graph_id: str) -> dict[str, Any]:
        path = self._path_for(graph_id)
        with self._lock:
            if not path.is_file():
                raise GraphDefinitionError(
                    "graph_not_found",
                    f"Graph Definition {graph_id!r} was not found",
                    status=404,
                )
            return self._read_graph(path)

    def create_graph(self, payload: Any) -> dict[str, Any]:
        self._require_object(payload)
        with self._lock:
            graph_id = payload.get("id", payload.get("graph_id")) or f"graph-{uuid.uuid4().hex[:12]}"
            self._validate_id(graph_id)
            if "id" in payload and "graph_id" in payload and payload["id"] != payload["graph_id"]:
                raise GraphDefinitionError("invalid_graph", "id and graph_id must agree")
            path = self._path_for(graph_id)
            if path.exists():
                raise GraphDefinitionError(
                    "graph_exists",
                    f"Graph Definition {graph_id!r} already exists",
                    status=409,
                )
            graph = self._normalize(payload, graph_id=graph_id)
            now = int(time.time())
            graph["created_at"] = now
            graph["updated_at"] = now
            graph["revision"] = 1
            self._write_graph(path, graph)
            append_audit(
                self.library_root, object_type="graph", object_id=graph_id,
                parent_revision=0, new_revision=1, before=None, after=graph,
                source=payload.get("_source", "api"),
            )
            return copy.deepcopy(graph)

    def update_graph(self, graph_id: str, payload: Any) -> dict[str, Any]:
        self._require_object(payload)
        with self._lock:
            path = self._path_for(graph_id)
            if not path.is_file():
                raise GraphDefinitionError(
                    "graph_not_found",
                    f"Graph Definition {graph_id!r} was not found",
                    status=404,
                )
            if "id" in payload and payload["id"] != graph_id:
                raise GraphDefinitionError("graph_id_immutable", "graph id cannot be changed")
            if "graph_id" in payload and payload["graph_id"] != graph_id:
                raise GraphDefinitionError("graph_id_immutable", "graph_id cannot be changed")
            current = self._read_graph(path)
            expected = expected_revision(payload)
            if expected is not None and expected != current.get("revision", 0):
                raise GraphDefinitionError(
                    "revision_conflict", f"Graph Definition {graph_id!r} revision conflict", status=409,
                    details=conflict_payload("graph", graph_id, expected, current.get("revision", 0)),
                )
            merged = {**current, **copy.deepcopy(payload), "id": graph_id, "graph_id": graph_id}
            graph = self._normalize(merged, graph_id=graph_id)
            graph["created_at"] = current.get("created_at", 0)
            graph["updated_at"] = int(time.time())
            graph["revision"] = current.get("revision", 0) + 1
            self._write_graph(path, graph)
            append_audit(
                self.library_root, object_type="graph", object_id=graph_id,
                parent_revision=current.get("revision", 0), new_revision=graph["revision"],
                before=current, after=graph, source=payload.get("_source", "api"),
            )
            return copy.deepcopy(graph)

    def copy_graph(self, graph_id: str, payload: Any = None) -> dict[str, Any]:
        if payload is not None and not isinstance(payload, dict):
            raise GraphDefinitionError("invalid_graph", "copy payload must be an object")
        with self._lock:
            original = self.get_graph(graph_id)
            overrides = copy.deepcopy(payload or {})
            overrides.pop("id", None)
            overrides.pop("graph_id", None)
            overrides.pop("created_at", None)
            overrides.pop("updated_at", None)
            new_id = overrides.pop("new_id", None) or f"graph-{uuid.uuid4().hex[:12]}"
            self._validate_id(new_id)
            if self._path_for(new_id).exists():
                raise GraphDefinitionError("graph_exists", f"Graph Definition {new_id!r} already exists", status=409)
            copied = {**original, **overrides, "id": new_id, "graph_id": new_id}
            copied["name"] = self._copy_name(original["name"])
            copied.pop("created_at", None)
            copied.pop("updated_at", None)
            return self.create_graph(copied)

    def delete_graph(self, graph_id: str) -> None:
        with self._lock:
            path = self._path_for(graph_id)
            if not path.is_file():
                raise GraphDefinitionError(
                    "graph_not_found",
                    f"Graph Definition {graph_id!r} was not found",
                    status=404,
                )
            try:
                path.unlink()
            except OSError as exc:
                raise GraphDefinitionError(
                    "graph_delete_failed",
                    f"Graph Definition {graph_id!r} could not be deleted",
                    status=500,
                ) from exc

    def _normalize(self, payload: dict[str, Any], *, graph_id: str) -> dict[str, Any]:
        self._validate_id(graph_id)
        name = payload.get("name", graph_id)
        if not isinstance(name, str) or not name.strip():
            raise GraphDefinitionError("invalid_graph", "name must be a non-empty string")
        mode = payload.get("mode", "sequential")
        if mode != "sequential":
            raise GraphDefinitionError("invalid_graph", "only sequential graph mode is supported")
        raw_nodes = payload.get("nodes")
        if not isinstance(raw_nodes, list) or not raw_nodes:
            raise GraphDefinitionError("invalid_graph", "nodes must be a non-empty array")

        nodes: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw in enumerate(raw_nodes):
            node = self._normalize_node(raw, index)
            if node["node_id"] in seen:
                raise GraphDefinitionError("invalid_graph", f"duplicate graph node id: {node['node_id']}")
            seen.add(node["node_id"])
            self._validate_agent(node["agent_id"])
            nodes.append(node)
        nodes.sort(key=lambda item: (item["order"], item["declaration_index"]))
        for index, node in enumerate(nodes):
            node["order"] = index
            node.pop("declaration_index", None)

        enabled = [node for node in nodes if node["enabled"]]
        if not enabled:
            raise GraphDefinitionError("invalid_graph", "graph must have at least one enabled node")
        output_node_id = payload.get("output_node_id", payload.get("outputNodeId"))
        if output_node_id is None:
            output_node_id = enabled[-1]["node_id"]
        if not isinstance(output_node_id, str) or output_node_id not in {node["node_id"] for node in nodes}:
            raise GraphDefinitionError("invalid_graph", "output_node_id must reference a graph node")
        if output_node_id not in {node["node_id"] for node in enabled}:
            raise GraphDefinitionError("invalid_graph", "output_node_id must reference an enabled node")
        if enabled[-1]["node_id"] != output_node_id:
            raise GraphDefinitionError("invalid_graph", "output node must be the final enabled node")

        return {
            "id": graph_id,
            "graph_id": graph_id,
            "name": name.strip(),
            "version": str(payload.get("version") or "1"),
            "mode": "sequential",
            "nodes": nodes,
            "output_node_id": output_node_id,
            "created_at": self._timestamp(payload.get("created_at")),
            "updated_at": self._timestamp(payload.get("updated_at")),
        }

    def _normalize_node(self, raw: Any, index: int) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise GraphDefinitionError("invalid_graph", f"graph node {index} must be an object")
        node_id = raw.get("node_id", raw.get("id"))
        if "node_id" in raw and "id" in raw and raw["node_id"] != raw["id"]:
            raise GraphDefinitionError("invalid_graph", "node_id and id must agree")
        self._validate_node_id(node_id)

        agent_id = raw.get("agent_id", raw.get("agent_definition_id", raw.get("agentId")))
        if agent_id is None:
            agent = raw.get("agent") or raw.get("agent_definition")
            if isinstance(agent, str):
                agent_id = agent
            elif isinstance(agent, dict):
                agent_id = agent.get("agent_id", agent.get("id"))
        self._validate_agent_id(agent_id)

        label = raw.get("label")
        if label is not None and not isinstance(label, str):
            raise GraphDefinitionError("invalid_graph", "node label must be a string")
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise GraphDefinitionError("invalid_graph", "node enabled must be boolean")
        order = raw.get("order", index)
        if not isinstance(order, int) or isinstance(order, bool):
            raise GraphDefinitionError("invalid_graph", "node order must be an integer")

        normalized: dict[str, Any] = {
            "node_id": node_id,
            # ``id`` is a read-compatible alias for legacy clients.  Node
            # identity remains independent from the referenced Agent id.
            "id": node_id,
            "agent_id": agent_id,
            "label": label.strip() if isinstance(label, str) and label.strip() else None,
            "enabled": enabled,
            "order": order,
            "declaration_index": index,
        }
        for canonical, aliases in (
            ("provider_profile_id", ("provider_profile_id", "provider_id")),
            ("model_id", ("model_id", "model")),
        ):
            value = next((raw[key] for key in aliases if key in raw), None)
            if value is not None:
                if not isinstance(value, str) or not value.strip():
                    raise GraphDefinitionError("invalid_graph", f"{canonical} must be a non-empty string")
                normalized[canonical] = value.strip()
        for canonical, aliases in (
            ("generation", ("generation", "generation_override", "generation_config")),
            ("advanced", ("advanced", "advanced_override", "advanced_parameters")),
        ):
            value = next((raw[key] for key in aliases if key in raw), None)
            if value is not None:
                if not isinstance(value, dict):
                    raise GraphDefinitionError("invalid_graph", f"{canonical} must be an object")
                normalized[canonical] = copy.deepcopy(value)
        return normalized

    def _validate_agent(self, agent_id: str) -> None:
        if self.agent_store is None:
            return
        try:
            self.agent_store.get_agent(agent_id)
        except Exception as exc:
            code = getattr(exc, "code", "agent_not_found")
            status = getattr(exc, "status", 404)
            raise GraphDefinitionError(code, str(exc), status=status) from exc

    def _copy_name(self, name: str) -> str:
        names = {graph["name"].casefold() for graph in self.list_graphs()}
        base = f"{name}-copy"
        candidate = base
        suffix = 2
        while candidate.casefold() in names:
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    def _read_graph(self, path: Path) -> dict[str, Any]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GraphDefinitionError("invalid_graph_data", f"cannot read Graph Definition {path.stem!r}") from exc
        if not isinstance(raw, dict):
            raise GraphDefinitionError("invalid_graph_data", f"Graph Definition {path.stem!r} is not an object")
        graph_id = raw.get("id", raw.get("graph_id", path.stem))
        return self._normalize(raw, graph_id=graph_id) | {
            "created_at": self._timestamp(raw.get("created_at")),
            "updated_at": self._timestamp(raw.get("updated_at")),
            "revision": revision(raw.get("revision"), 0),
        }

    def _paths(self) -> list[Path]:
        return sorted(self.library_root.glob("*.json")) if self.library_root.is_dir() else []

    def _path_for(self, graph_id: str) -> Path:
        self._validate_id(graph_id)
        self.library_root.mkdir(parents=True, exist_ok=True)
        path = (self.library_root / f"{graph_id}.json").resolve()
        try:
            path.relative_to(self.library_root.resolve())
        except ValueError as exc:
            raise GraphDefinitionError("invalid_graph", "invalid Graph Definition id") from exc
        return path

    def _write_graph(self, path: Path, graph: dict[str, Any]) -> None:
        self.library_root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=self.library_root)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(graph, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    @staticmethod
    def _validate_id(value: Any) -> None:
        if not isinstance(value, str) or not GRAPH_ID_RE.fullmatch(value):
            raise GraphDefinitionError("invalid_graph", "id must be a safe Graph identifier")

    @staticmethod
    def _validate_node_id(value: Any) -> None:
        if not isinstance(value, str) or not NODE_ID_RE.fullmatch(value):
            raise GraphDefinitionError("invalid_graph", "node_id must be a safe graph node identifier")

    @staticmethod
    def _validate_agent_id(value: Any) -> None:
        if not isinstance(value, str) or not NODE_ID_RE.fullmatch(value):
            raise GraphDefinitionError("invalid_graph", "agent_id must be a safe Agent identifier")

    @staticmethod
    def _require_object(value: Any) -> None:
        if not isinstance(value, dict):
            raise GraphDefinitionError("invalid_graph", "Graph Definition must be an object")

    @staticmethod
    def _timestamp(value: Any) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


class GraphDefinitionService:
    """Thin public facade used by the Runtime Studio HTTP adapter."""

    def __init__(self, store: GraphDefinitionStore) -> None:
        self._store = store

    def list_graphs(self) -> list[dict[str, Any]]:
        return self._store.list_graphs()

    def get_graph(self, graph_id: str) -> dict[str, Any]:
        return self._store.get_graph(graph_id)

    def create_graph(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"graph": self._store.create_graph(payload)}

    def update_graph(self, graph_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        return {"graph": self._store.update_graph(graph_id, payload)}

    def copy_graph(self, graph_id: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"graph": self._store.copy_graph(graph_id, payload)}

    def delete_graph(self, graph_id: str) -> None:
        self._store.delete_graph(graph_id)
