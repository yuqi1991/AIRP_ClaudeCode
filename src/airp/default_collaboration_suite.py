"""Install the bundled editable collaboration recipe exactly once."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from contextlib import contextmanager
from importlib.resources import files
from uuid import uuid4
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

from airp.engine.active_graph import ActiveGraphSelectionError
from airp.engine.graph_definitions import GraphDefinitionError
from airp.engine.project_library import ProjectLibraryError

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None

try:
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - POSIX
    _msvcrt = None


_ID_KEYS = frozenset(
    {
        "provider_profile_id",
        "regex_collection_id",
        "writer_agent_id",
        "reviewer_agent_id",
        "graph_id",
    }
)
_VERSIONED_RECEIPT_KEYS = _ID_KEYS | {"version", "transaction_id", "recipe_digest"}
_SECRET_KEYS = frozenset(
    {"api_key", "apikey", "authorization", "secret", "secret_ref", "token"}
)


class DefaultCollaborationSuiteError(RuntimeError):
    """A fatal, redacted installation metadata or recovery failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ProjectInitializationWarning:
    code: str
    boundary: str
    project_id: str
    message: str
    action: str


@dataclass(frozen=True)
class ProjectInitializationResult:
    selected: bool
    warning: ProjectInitializationWarning | None = None


@dataclass
class ProjectDeletionToken:
    _suite: "DefaultCollaborationSuite"
    _owner: object
    project_id: str
    lifecycle: str | None
    _finalized: bool = False

    def commit(self) -> None:
        self._suite._commit_project_deletion(self)

    def rollback(self) -> None:
        self._suite._rollback_project_deletion(self)


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
        self._journal_path = self._metadata_root / "pending.json"
        self._lock_path = self._metadata_root / "install.lock"
        self._initialized_projects_path = self._metadata_root / "initialized_projects.json"
        self._lock = self._lock_for_path(self._metadata_root.resolve())
        self._project_deletion_owner = object()

    @classmethod
    def _lock_for_path(cls, path: Path) -> threading.RLock:
        with cls._path_locks_guard:
            return cls._path_locks.setdefault(path, threading.RLock())

    @contextmanager
    def _installation_lock(self) -> Iterator[None]:
        with self._lock:
            self._metadata_root.mkdir(parents=True, exist_ok=True)
            with self._lock_path.open("a+", encoding="utf-8") as handle:
                self._acquire_file_lock(handle)
                try:
                    yield
                finally:
                    self._release_file_lock(handle)

    @staticmethod
    def _acquire_file_lock(handle) -> None:
        if _fcntl is not None:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_EX)
            return
        if _msvcrt is None:
            raise DefaultCollaborationSuiteError(
                "default_collaboration_suite_lock_unsupported",
                "当前平台不支持 Workspace 安装锁；启动已阻止。",
            )
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write("\0")
            handle.flush()
        handle.seek(0)
        _msvcrt.locking(handle.fileno(), _msvcrt.LK_LOCK, 1)

    @staticmethod
    def _release_file_lock(handle) -> None:
        if _fcntl is not None:
            _fcntl.flock(handle.fileno(), _fcntl.LOCK_UN)
            return
        handle.seek(0)
        _msvcrt.locking(handle.fileno(), _msvcrt.LK_UNLCK, 1)

    def install_once(self) -> dict[str, Any]:
        with self._installation_lock():
            receipt = self._read_receipt()
            journal = self._read_journal()
            if receipt is not None:
                if journal is not None:
                    self._validate_roll_forward(receipt, journal)
                    self._remove_journal()
                return {**self._public_receipt(receipt), "installed": False}
            if journal is not None:
                self._compensate_or_fail(journal)
                self._remove_journal()
                return self._failure_result("recovery")

            journal = self._plan_install(self._load_recipe())
            try:
                self._write_json(self._journal_path, journal)
            except Exception:
                persisted_journal = self._read_journal()
                if persisted_journal != journal:
                    raise DefaultCollaborationSuiteError(
                        "default_collaboration_suite_journal_write_failed",
                        "默认协作套件 journal 无法安全写入；启动已阻止。",
                    ) from None
                self._compensate_or_fail(journal)
                self._remove_journal()
                return self._failure_result("journal")
            resources = {item["kind"]: item for item in journal["resources"]}
            boundary = "provider"
            warnings: list[dict[str, str]] = []
            try:
                provider = resources["provider"]
                if not provider["reused"]:
                    self._providers.create_profile(provider["payload"])
                boundary = "regex_collection"
                self._regex.create_collection(resources["regex_collection"]["payload"])
                boundary = "writer_agent"
                self._agents.create_agent(resources["writer_agent"]["payload"])
                boundary = "reviewer_agent"
                self._agents.create_agent(resources["reviewer_agent"]["payload"])
                boundary = "graph"
                self._graphs.create_graph(resources["graph"]["payload"])

                boundary = "project_activation"
                for project in journal["projects"]:
                    try:
                        activation = self._activate_frozen_project(
                            project, resources["graph"]["id"]
                        )
                        if activation.warning is not None:
                            warnings.append(self._activation_warning(project["id"]))
                    except ActiveGraphSelectionError:
                        raise
                    except DefaultCollaborationSuiteError:
                        raise
                    except Exception:
                        warnings.append(self._activation_warning(project["id"]))

                boundary = "receipt"
                receipt = self._receipt_from_journal(journal)
                self._write_json(self._receipt_path, receipt)
            except DefaultCollaborationSuiteError:
                self._compensate_or_fail(journal)
                self._remove_journal()
                raise
            except ActiveGraphSelectionError:
                self._compensate_or_fail(journal)
                self._remove_journal()
                raise DefaultCollaborationSuiteError(
                    "default_collaboration_suite_active_graph_invalid",
                    "Active Graph selection 不安全；启动已阻止。",
                ) from None
            except Exception:
                if boundary == "receipt":
                    persisted_receipt = self._read_receipt()
                    if persisted_receipt is not None:
                        self._validate_roll_forward(persisted_receipt, journal)
                        self._remove_journal()
                        result = {
                            **self._public_receipt(persisted_receipt),
                            "installed": True,
                        }
                        if warnings:
                            result["status"] = "degraded"
                            result["diagnostics"] = warnings
                        return result
                self._compensate_or_fail(journal)
                self._remove_journal()
                return self._failure_result(boundary)

            self._remove_journal()
            result: dict[str, Any] = {**self._public_receipt(receipt), "installed": True}
            if warnings:
                result["status"] = "degraded"
                result["diagnostics"] = warnings
            return result

    def initialize_project(self, project_id: str) -> ProjectInitializationResult:
        with self._installation_lock():
            receipt = self._read_receipt()
            if receipt is None:
                return ProjectInitializationResult(selected=False)
            lifecycle = self._projects.project_instance_id(project_id)
            initialized = self._read_initialized_projects()
            if initialized.get(project_id) == lifecycle:
                return ProjectInitializationResult(selected=False)
            warning = None
            try:
                self._graphs.get_graph(receipt["graph_id"])
            except GraphDefinitionError as exc:
                if exc.code != "graph_not_found":
                    raise
                selected = False
            else:
                try:
                    claim = self._active_graphs.select_if_unset_claim(project_id, receipt["graph_id"])
                    selected = claim is not None
                except ActiveGraphSelectionError as exc:
                    if exc.code != "active_graph_selection_write_failed":
                        raise
                    selected = False
                    warning = self._project_initialization_warning(project_id)
            initialized[project_id] = lifecycle
            try:
                self._write_json(self._initialized_projects_path, initialized)
            except Exception:
                if selected:
                    try:
                        self._active_graphs.clear_claim(claim)
                    except Exception:
                        raise DefaultCollaborationSuiteError(
                            "default_collaboration_suite_compensation_failed",
                            "默认协作套件无法撤销 Project activation；启动已阻止。",
                        ) from None
                raise DefaultCollaborationSuiteError(
                    "default_collaboration_suite_project_activation_failed",
                    "默认协作套件无法记录 Project activation。",
                ) from None
            return ProjectInitializationResult(selected=selected, warning=warning)

    def begin_project_deletion(self, project_id: str) -> ProjectDeletionToken:
        """Remove one lifecycle and return its commit/rollback capability."""
        with self._installation_lock():
            initialized = self._read_initialized_projects()
            lifecycle = initialized.pop(project_id, None)
            if lifecycle is not None:
                self._write_json(self._initialized_projects_path, initialized)
            return ProjectDeletionToken(
                self, self._project_deletion_owner, project_id, lifecycle
            )

    def _commit_project_deletion(self, token: ProjectDeletionToken) -> None:
        self._validate_project_deletion(token)
        token._finalized = True

    def _rollback_project_deletion(self, token: ProjectDeletionToken) -> None:
        """Restore a removed lifecycle only while no replacement was recorded."""
        self._validate_project_deletion(token)
        if token.lifecycle is None:
            token._finalized = True
            return
        with self._installation_lock():
            initialized = self._read_initialized_projects()
            current = initialized.get(token.project_id)
            if current is not None and current != token.lifecycle:
                raise DefaultCollaborationSuiteError(
                    "default_collaboration_suite_project_metadata_conflict",
                    "默认协作套件 Project metadata 已被并发修改。",
                )
            if current is None:
                initialized[token.project_id] = token.lifecycle
                self._write_json(self._initialized_projects_path, initialized)
            token._finalized = True

    def _validate_project_deletion(self, token: ProjectDeletionToken) -> None:
        if (
            not isinstance(token, ProjectDeletionToken)
            or token._suite is not self
            or token._owner is not self._project_deletion_owner
            or token._finalized
        ):
            raise ValueError("invalid or finalized Project deletion token")

    def _plan_install(self, recipe: dict[str, Any]) -> dict[str, Any]:
        self._assert_secret_free(recipe)
        provider = self._resolve_provider()
        reused = provider is not None
        if provider is None:
            provider_payload = self._allocate(recipe["provider"], self._providers.list_profiles())
            provider_id = provider_payload["id"]
        else:
            provider_payload = None
            provider_id = provider["id"]

        regex_payload = self._allocate(
            recipe["regex_collection"], self._regex.list_collections()
        )
        existing_agents = self._agents.list_agents()
        writer_payload = self._allocate(recipe["agents"]["writer"], existing_agents)
        reviewer_payload = self._allocate(
            recipe["agents"]["reviewer"], [*existing_agents, writer_payload]
        )
        writer_payload.update(
            provider_profile_id=provider_id,
            model_id="deepseek-v4-flash",
            regex_collection_id=regex_payload["id"],
        )
        reviewer_payload.update(
            provider_profile_id=provider_id,
            model_id="deepseek-v4-flash",
        )
        graph_payload = self._allocate(recipe["graph"], self._graphs.list_graphs())
        agent_ids = {
            "writer": writer_payload["id"],
            "reviewer": reviewer_payload["id"],
        }
        for node in graph_payload["nodes"]:
            node["agent_id"] = agent_ids[node.pop("agent")]

        projects = []
        initialized = self._read_initialized_projects()
        for project in self._projects.list_projects():
            project_id = project["id"]
            projects.append(
                {
                    "id": project_id,
                    "instance_id": self._projects.project_instance_id(project_id),
                    "previous_selection": self._active_graphs.graph_id_for(project_id),
                    "previous_initialization": initialized.get(project_id),
                }
            )
        journal = {
            "version": 2,
            "transaction_id": uuid4().hex,
            "recipe_digest": "",
            "resources": [
                {
                    "kind": "provider",
                    "id": provider_id,
                    "payload": provider_payload,
                    "reused": reused,
                },
                {
                    "kind": "regex_collection",
                    "id": regex_payload["id"],
                    "payload": regex_payload,
                    "reused": False,
                },
                {
                    "kind": "writer_agent",
                    "id": writer_payload["id"],
                    "payload": writer_payload,
                    "reused": False,
                },
                {
                    "kind": "reviewer_agent",
                    "id": reviewer_payload["id"],
                    "payload": reviewer_payload,
                    "reused": False,
                },
                {
                    "kind": "graph",
                    "id": graph_payload["id"],
                    "payload": graph_payload,
                    "reused": False,
                },
            ],
            "projects": projects,
        }
        journal["recipe_digest"] = self._digest(
            {"resources": journal["resources"], "projects": journal["projects"]}
        )
        self._assert_secret_free(journal)
        return journal

    def _activate_frozen_project(
        self, project: dict[str, Any], graph_id: str
    ) -> ProjectInitializationResult:
        project_id = project["id"]
        try:
            lifecycle = self._projects.project_instance_id(project_id)
        except ProjectLibraryError as exc:
            if exc.code == "project_not_found":
                return ProjectInitializationResult(selected=False)
            raise DefaultCollaborationSuiteError(
                "default_collaboration_suite_project_lifecycle_invalid",
                "默认协作套件无法安全读取 Project lifecycle；启动已阻止。",
            ) from None
        except Exception:
            raise DefaultCollaborationSuiteError(
                "default_collaboration_suite_project_lifecycle_invalid",
                "默认协作套件无法安全读取 Project lifecycle；启动已阻止。",
            ) from None
        if lifecycle != project["instance_id"]:
            return ProjectInitializationResult(selected=False)
        initialized = self._read_initialized_projects()
        if initialized.get(project_id) == lifecycle:
            return ProjectInitializationResult(selected=False)
        warning = None
        try:
            claim = self._active_graphs.select_if_unset_claim(project_id, graph_id)
            selected = claim is not None
        except ActiveGraphSelectionError as exc:
            if exc.code != "active_graph_selection_write_failed":
                raise
            selected = False
            warning = self._project_initialization_warning(project_id)
        initialized[project_id] = lifecycle
        try:
            self._write_json(self._initialized_projects_path, initialized)
        except Exception:
            if selected:
                try:
                    self._active_graphs.clear_claim(claim)
                except Exception:
                    raise DefaultCollaborationSuiteError(
                        "default_collaboration_suite_compensation_failed",
                        "默认协作套件无法撤销 Project activation；启动已阻止。",
                    ) from None
            raise
        return ProjectInitializationResult(selected=selected, warning=warning)

    def _compensate_or_fail(self, journal: dict[str, Any]) -> None:
        try:
            graph_id = next(
                item["id"] for item in journal["resources"] if item["kind"] == "graph"
            )
            initialized = self._read_initialized_projects()
            ledger_changed = False
            for project in reversed(journal["projects"]):
                project_id = project["id"]
                previous_selection = project["previous_selection"]
                current_selection = self._active_graphs.graph_id_for(project_id)
                if previous_selection is None and current_selection == graph_id:
                    self._active_graphs.clear_if_equals(project_id, graph_id)
                elif current_selection != previous_selection:
                    raise DefaultCollaborationSuiteError(
                        "default_collaboration_suite_recovery_conflict",
                        "默认协作套件恢复发现未知的 Project Graph 冲突。",
                    )
                previous_initialization = project["previous_initialization"]
                current_initialization = initialized.get(project_id)
                if (
                    current_initialization == project["instance_id"]
                    and current_initialization != previous_initialization
                ):
                    if previous_initialization is None:
                        initialized.pop(project_id, None)
                    else:
                        initialized[project_id] = previous_initialization
                    ledger_changed = True
                elif current_initialization != previous_initialization:
                    raise DefaultCollaborationSuiteError(
                        "default_collaboration_suite_recovery_conflict",
                        "默认协作套件恢复发现未知的 Project initialization 冲突。",
                    )
            if ledger_changed:
                self._write_json(self._initialized_projects_path, initialized)

            for resource in reversed(journal["resources"]):
                if resource["reused"]:
                    continue
                current = self._get_optional(resource["kind"], resource["id"])
                if current is None:
                    continue
                if current.get("revision") != 1 or not self._contains_payload(
                    current, resource["payload"]
                ):
                    raise DefaultCollaborationSuiteError(
                        "default_collaboration_suite_recovery_conflict",
                        "默认协作套件恢复发现未知的 Library 对象冲突。",
                    )
                self._delete_resource(resource["kind"], resource["id"])
        except DefaultCollaborationSuiteError:
            raise
        except Exception as exc:
            raise DefaultCollaborationSuiteError(
                "default_collaboration_suite_compensation_failed",
                "默认协作套件无法安全完成补偿；启动已阻止。",
            ) from None

    def _get_optional(self, kind: str, object_id: str) -> dict[str, Any] | None:
        getters = {
            "provider": self._providers.get_profile,
            "regex_collection": self._regex.get_collection,
            "writer_agent": self._agents.get_agent,
            "reviewer_agent": self._agents.get_agent,
            "graph": self._graphs.get_graph,
        }
        try:
            return getters[kind](object_id)
        except Exception as exc:
            if getattr(exc, "code", "") in {
                "provider_profile_not_found",
                "regex_collection_not_found",
                "agent_not_found",
                "graph_not_found",
            }:
                return None
            raise

    def _delete_resource(self, kind: str, object_id: str) -> None:
        deleters = {
            "provider": self._providers.delete_profile,
            "regex_collection": self._regex.delete_collection,
            "writer_agent": self._agents.delete_agent,
            "reviewer_agent": self._agents.delete_agent,
            "graph": self._graphs.delete_graph,
        }
        deleters[kind](object_id)

    @classmethod
    def _contains_payload(cls, actual: Any, expected: Any) -> bool:
        if isinstance(expected, dict):
            return isinstance(actual, dict) and all(
                key in actual and cls._contains_payload(actual[key], value)
                for key, value in expected.items()
            )
        if isinstance(expected, list):
            return isinstance(actual, list) and len(actual) == len(expected) and all(
                cls._contains_payload(actual_item, expected_item)
                for actual_item, expected_item in zip(actual, expected)
            )
        return actual == expected

    def _read_initialized_projects(self) -> dict[str, str]:
        raw = self._read_json_optional(self._initialized_projects_path, "project_metadata")
        if raw is None:
            return {}
        if not all(
            isinstance(key, str) and isinstance(value, str) for key, value in raw.items()
        ):
            raise DefaultCollaborationSuiteError(
                "default_collaboration_suite_project_metadata_invalid",
                "默认协作套件 Project metadata 已损坏；启动已阻止。",
            )
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
        while f"{canonical_id}{suffix}" in ids or f"{canonical_name}{suffix}".casefold() in names:
            ordinal += 1
            suffix = "-copy" if ordinal == 2 else f"-copy-{ordinal - 1}"
        allocated["id"] = f"{canonical_id}{suffix}"
        allocated["name"] = f"{canonical_name}{suffix}"
        return allocated

    @staticmethod
    def _load_recipe() -> dict[str, Any]:
        resource = files("airp.resources").joinpath("default_collaboration_suite.json")
        try:
            raw = json.loads(resource.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DefaultCollaborationSuiteError(
                "default_collaboration_suite_recipe_invalid",
                "默认协作套件 recipe 无法读取；启动已阻止。",
            ) from None
        if not isinstance(raw, dict):
            raise DefaultCollaborationSuiteError(
                "default_collaboration_suite_recipe_invalid",
                "默认协作套件 recipe 无效；启动已阻止。",
            )
        return raw

    def _read_receipt(self) -> dict[str, Any] | None:
        raw = self._read_json_optional(self._receipt_path, "receipt")
        if raw is None:
            return None
        keys = set(raw)
        ids_valid = all(
            isinstance(raw.get(key), str) and bool(raw[key].strip()) for key in _ID_KEYS
        )
        legacy = keys == _ID_KEYS and ids_valid
        versioned = (
            keys == _VERSIONED_RECEIPT_KEYS
            and raw.get("version") == 2
            and ids_valid
            and isinstance(raw.get("transaction_id"), str)
            and bool(raw["transaction_id"])
            and isinstance(raw.get("recipe_digest"), str)
            and bool(raw["recipe_digest"])
        )
        if not legacy and not versioned:
            raise DefaultCollaborationSuiteError(
                "default_collaboration_suite_receipt_invalid",
                "默认协作套件 receipt 已损坏；启动已阻止。",
            )
        self._assert_secret_free(raw)
        return raw

    def _read_journal(self) -> dict[str, Any] | None:
        raw = self._read_json_optional(self._journal_path, "journal")
        if raw is None:
            return None
        try:
            if set(raw) != {"version", "transaction_id", "recipe_digest", "resources", "projects"}:
                raise ValueError
            if (
                raw["version"] != 2
                or not isinstance(raw["transaction_id"], str)
                or not bool(raw["transaction_id"])
                or not isinstance(raw["recipe_digest"], str)
            ):
                raise ValueError
            resources = raw["resources"]
            expected_kinds = [
                "provider",
                "regex_collection",
                "writer_agent",
                "reviewer_agent",
                "graph",
            ]
            if not isinstance(resources, list) or [
                item.get("kind") for item in resources
            ] != expected_kinds:
                raise ValueError
            for item in resources:
                if set(item) != {"kind", "id", "payload", "reused"}:
                    raise ValueError
                if not isinstance(item["id"], str) or not isinstance(item["reused"], bool):
                    raise ValueError
                if item["reused"] != (
                    item["kind"] == "provider" and item["payload"] is None
                ):
                    raise ValueError
                if not item["reused"] and not isinstance(item["payload"], dict):
                    raise ValueError
            if not isinstance(raw["projects"], list):
                raise ValueError
            for project in raw["projects"]:
                if set(project) != {
                    "id", "instance_id", "previous_selection", "previous_initialization"
                }:
                    raise ValueError
                if (
                    not isinstance(project["id"], str)
                    or not isinstance(project["instance_id"], str)
                    or (project["previous_selection"] is not None and not isinstance(project["previous_selection"], str))
                    or (project["previous_initialization"] is not None and not isinstance(project["previous_initialization"], str))
                ):
                    raise ValueError
            expected_digest = self._digest(
                {"resources": raw["resources"], "projects": raw["projects"]}
            )
            if raw["recipe_digest"] != expected_digest:
                raise ValueError
            self._assert_secret_free(raw)
        except (AttributeError, TypeError, ValueError) as exc:
            raise DefaultCollaborationSuiteError(
                "default_collaboration_suite_journal_invalid",
                "默认协作套件 pending journal 已损坏；启动已阻止。",
            ) from None
        return raw

    def _read_json_optional(self, path: Path, label: str) -> dict[str, Any] | None:
        try:
            serialized = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, UnicodeDecodeError) as exc:
            raise DefaultCollaborationSuiteError(
                f"default_collaboration_suite_{label}_invalid",
                f"默认协作套件 {label} 无法读取；启动已阻止。",
            ) from None
        try:
            raw = json.loads(serialized)
        except json.JSONDecodeError as exc:
            raise DefaultCollaborationSuiteError(
                f"default_collaboration_suite_{label}_invalid",
                f"默认协作套件 {label} 已损坏；启动已阻止。",
            ) from None
        if not isinstance(raw, dict):
            raise DefaultCollaborationSuiteError(
                f"default_collaboration_suite_{label}_invalid",
                f"默认协作套件 {label} 必须是对象；启动已阻止。",
            )
        return raw

    @staticmethod
    def _receipt_from_journal(journal: dict[str, Any]) -> dict[str, Any]:
        resources = {item["kind"]: item["id"] for item in journal["resources"]}
        return {
            "version": 2,
            "transaction_id": journal["transaction_id"],
            "recipe_digest": journal["recipe_digest"],
            "provider_profile_id": resources["provider"],
            "regex_collection_id": resources["regex_collection"],
            "writer_agent_id": resources["writer_agent"],
            "reviewer_agent_id": resources["reviewer_agent"],
            "graph_id": resources["graph"],
        }

    @staticmethod
    def _public_receipt(receipt: dict[str, Any]) -> dict[str, str]:
        return {key: receipt[key] for key in _ID_KEYS}

    def _validate_roll_forward(
        self, receipt: dict[str, Any], journal: dict[str, Any]
    ) -> None:
        expected = self._receipt_from_journal(journal)
        if set(receipt) != _VERSIONED_RECEIPT_KEYS or receipt != expected:
            raise DefaultCollaborationSuiteError(
                "default_collaboration_suite_recovery_conflict",
                "默认协作套件 receipt 与 pending journal 不一致；启动已阻止。",
            )

    @classmethod
    def _assert_secret_free(cls, payload: Any) -> None:
        if isinstance(payload, dict):
            for key, value in payload.items():
                normalized_key = str(key).casefold().replace("-", "_")
                if (
                    normalized_key in _SECRET_KEYS
                    or normalized_key.endswith(("_key", "_token"))
                    or any(
                        marker in normalized_key
                        for marker in ("authorization", "credential", "password", "secret")
                    )
                ):
                    raise DefaultCollaborationSuiteError(
                        "default_collaboration_suite_secret_metadata",
                        "默认协作套件 metadata 包含禁止的 Secret 字段；启动已阻止。",
                    )
                cls._assert_secret_free(value)
        elif isinstance(payload, list):
            for value in payload:
                cls._assert_secret_free(value)

    @staticmethod
    def _digest(payload: dict[str, Any]) -> str:
        serialized = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(serialized).hexdigest()

    @staticmethod
    def _failure_result(boundary: str) -> dict[str, Any]:
        return {
            "installed": False,
            "status": "degraded",
            "diagnostics": [
                {
                    "code": "default_collaboration_suite_install_failed",
                    "boundary": boundary,
                    "message": "默认协作套件安装失败；已撤销本次创建的对象，可在解决本地存储问题后重试。",
                    "action": "检查 Workspace 是否可写，然后重启 AIRP。",
                }
            ],
        }

    @staticmethod
    def _project_initialization_warning(project_id: str) -> ProjectInitializationWarning:
        return ProjectInitializationWarning(
            code="default_collaboration_suite_project_activation_failed",
            boundary="project_activation",
            project_id=project_id,
            message="默认协作套件已安装，但未能为一个 Project 自动选择 Graph。",
            action="在游戏 Monitor 的 Graph 下拉中手动选择 Graph。",
        )

    @staticmethod
    def _activation_warning(project_id: str) -> dict[str, str]:
        return {
            "code": "default_collaboration_suite_project_activation_failed",
            "boundary": "project_activation",
            "project_id": project_id,
            "message": "默认协作套件已安装，但未能为一个 Project 自动选择 Graph。",
            "action": "在游戏 Monitor 的 Graph 下拉中手动选择 Graph。",
        }

    def _remove_journal(self) -> None:
        try:
            self._journal_path.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            raise DefaultCollaborationSuiteError(
                "default_collaboration_suite_journal_cleanup_failed",
                "默认协作套件 journal 无法安全清理；启动已阻止。",
            ) from None
        self._fsync_directory(self._journal_path.parent)

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
            DefaultCollaborationSuite._fsync_directory(path.parent)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if _fcntl is None and _msvcrt is not None:
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
