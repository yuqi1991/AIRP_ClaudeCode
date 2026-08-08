"""Install the bundled editable collaboration recipe exactly once."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from airp.engine.graph_definitions import GraphDefinitionError


class DefaultCollaborationSuite:
    """Materialize the recipe through ordinary Studio Library interfaces."""

    _path_locks_guard = threading.Lock()
    _path_locks: dict[Path, threading.RLock] = {}

    def __init__(
        self,
        *,
        workspace,
        provider_store,
        secret_store,
        regex_collections,
        agent_store,
        graph_store,
        projects,
        active_graphs,
    ) -> None:
        self._providers = provider_store
        self._secrets = secret_store
        self._regex = regex_collections
        self._agents = agent_store
        self._graphs = graph_store
        self._projects = projects
        self._active_graphs = active_graphs
        self._metadata_root = workspace.runtime_root / "default_collaboration_suite"
        self._receipt_path = self._metadata_root / "receipt.json"
        self._initialized_projects_path = self._metadata_root / "initialized_projects.json"
        self._lock = self._lock_for_path(self._metadata_root.resolve())

    @classmethod
    def _lock_for_path(cls, path: Path) -> threading.RLock:
        with cls._path_locks_guard:
            return cls._path_locks.setdefault(path, threading.RLock())

    def install_once(self) -> dict[str, Any]:
        with self._lock:
            receipt = self._read_receipt()
            if receipt is not None:
                return {**receipt, "installed": False}

            recipe = self._load_recipe()
            provider = self._resolve_provider()
            if provider is None:
                provider_payload = self._allocate(
                    recipe["provider"], self._providers.list_profiles()
                )
                provider = self._providers.create_profile(provider_payload)
            regex_payload = self._allocate(
                recipe["regex_collection"], self._regex.list_collections()
            )
            regex = self._regex.create_collection(regex_payload)

            writer_payload = self._allocate(
                recipe["agents"]["writer"], self._agents.list_agents()
            )
            writer_payload.update(
                provider_profile_id=provider["id"],
                model_id="deepseek-v4-flash",
                regex_collection_id=regex["id"],
            )
            writer = self._agents.create_agent(writer_payload)

            reviewer_payload = self._allocate(
                recipe["agents"]["reviewer"], self._agents.list_agents()
            )
            reviewer_payload.update(
                provider_profile_id=provider["id"],
                model_id="deepseek-v4-flash",
            )
            reviewer = self._agents.create_agent(reviewer_payload)

            graph_payload = self._allocate(recipe["graph"], self._graphs.list_graphs())
            agent_ids = {"writer": writer["agent_id"], "reviewer": reviewer["agent_id"]}
            for node in graph_payload["nodes"]:
                node["agent_id"] = agent_ids[node.pop("agent")]
            graph = self._graphs.create_graph(graph_payload)

            receipt = {
                "provider_profile_id": provider["id"],
                "regex_collection_id": regex["id"],
                "writer_agent_id": writer["agent_id"],
                "reviewer_agent_id": reviewer["agent_id"],
                "graph_id": graph["id"],
            }
            self._write_json(self._receipt_path, receipt)
            for project in self._projects.list_projects():
                self.initialize_project(project["id"])
            return {**receipt, "installed": True}

    def initialize_project(self, project_id: str) -> bool:
        with self._lock:
            receipt = self._read_receipt()
            if receipt is None:
                return False
            lifecycle = self._projects.project_instance_id(project_id)
            initialized = self._read_initialized_projects()
            if initialized.get(project_id) == lifecycle:
                return False
            try:
                self._graphs.get_graph(receipt["graph_id"])
            except GraphDefinitionError as exc:
                if exc.code != "graph_not_found":
                    raise
                selected = False
            else:
                selected = self._active_graphs.select_if_unset(project_id, receipt["graph_id"])
            initialized[project_id] = lifecycle
            self._write_json(self._initialized_projects_path, initialized)
            return selected

    def _read_initialized_projects(self) -> dict[str, str]:
        try:
            raw = json.loads(self._initialized_projects_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        if not isinstance(raw, dict) or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in raw.items()
        ):
            raise ValueError("default collaboration suite project metadata must be a string map")
        return raw

    def _resolve_provider(self) -> dict[str, Any] | None:
        candidates = []
        for profile in self._providers.list_profiles():
            hostname = (urlparse(profile.get("base_url", "")).hostname or "").lower()
            if not profile.get("enabled"):
                continue
            if hostname != "deepseek.com" and not hostname.endswith(".deepseek.com"):
                continue
            if profile.get("api_format") not in {"chat_completions", "responses"}:
                continue
            candidates.append(profile)
        if not candidates:
            return None
        return min(
            candidates,
            key=lambda profile: (
                not self._secrets.has(profile["id"]),
                profile["id"] != "legacy-deepseek",
                profile["id"],
            ),
        )

    @staticmethod
    def _allocate(payload: dict[str, Any], existing: list[dict[str, Any]]) -> dict[str, Any]:
        allocated = copy.deepcopy(payload)
        canonical_id = allocated["id"]
        canonical_name = allocated["name"]
        ids = {item.get("id", item.get("agent_id")) for item in existing}
        names = {
            item["name"].casefold()
            for item in existing
            if isinstance(item.get("name"), str)
        }
        suffix = ""
        ordinal = 1
        while (
            f"{canonical_id}{suffix}" in ids
            or f"{canonical_name}{suffix}".casefold() in names
        ):
            ordinal += 1
            suffix = "-copy" if ordinal == 2 else f"-copy-{ordinal - 1}"
        allocated["id"] = f"{canonical_id}{suffix}"
        allocated["name"] = f"{canonical_name}{suffix}"
        return allocated

    @staticmethod
    def _load_recipe() -> dict[str, Any]:
        resource = files("airp.resources").joinpath("default_collaboration_suite.json")
        return json.loads(resource.read_text(encoding="utf-8"))

    def _read_receipt(self) -> dict[str, Any] | None:
        try:
            raw = json.loads(self._receipt_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        if not isinstance(raw, dict):
            raise ValueError("default collaboration suite receipt must be an object")
        return raw

    @staticmethod
    def _write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
