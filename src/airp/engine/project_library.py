"""AIRP Project definitions for the Studio project editor.

Import-format fields are normalized at this boundary.  The runtime receives a
small AIRP project shape and never needs to know about SillyTavern card keys or
the source card's extension object.
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

from airp.engine.worldbook_library import WorldbookLibrary, WorldbookLibraryError


PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class ProjectLibraryError(ValueError):
    """A user-facing AIRP Project validation or persistence failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        references: list[dict[str, str]] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
        self.references = references or []

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"ok": False, "error": self.code, "message": str(self)}
        if self.references:
            payload["references"] = copy.deepcopy(self.references)
        return payload


class ProjectLibrary:
    """File-backed CRUD for normalized AIRP Project definitions."""

    def __init__(
        self,
        static_root: str | Path,
        *,
        project_root: str | Path | None = None,
        worldbooks: WorldbookLibrary | None = None,
        workspace=None,
    ) -> None:
        self.static_root = Path(static_root).resolve()
        workspace_projects_root = getattr(workspace, "projects_root", None)
        self.project_root = (
            Path(project_root).resolve()
            if project_root is not None
            else (Path(workspace_projects_root).resolve() if workspace_projects_root else self.static_root / "studio" / "projects")
        )
        self.worldbooks = worldbooks or WorldbookLibrary(self.static_root, workspace=workspace)
        self._lock = threading.RLock()

    def list_projects(self) -> list[dict[str, Any]]:
        with self._lock:
            projects = [self._read_project(path) for path in self._paths()]
        return sorted(projects, key=lambda item: (item["name"].casefold(), item["id"]))

    def get_project(self, project_id: str) -> dict[str, Any]:
        path = self._path_for(project_id)
        with self._lock:
            if not path.is_file():
                raise ProjectLibraryError(
                    "project_not_found",
                    f"Project {project_id!r} was not found",
                    status=404,
                )
            return self._read_project(path)

    def create_project(self, payload: Any) -> dict[str, Any]:
        self._require_object(payload, "Project")
        with self._lock:
            project_id = payload.get("id") or payload.get("project_id") or f"project-{uuid.uuid4().hex[:12]}"
            self._validate_id(project_id)
            if "id" in payload and "project_id" in payload and payload["id"] != payload["project_id"]:
                raise ProjectLibraryError("invalid_project", "id and project_id must agree")
            path = self._path_for(project_id)
            if path.exists():
                raise ProjectLibraryError(
                    "project_exists",
                    f"Project {project_id!r} already exists",
                    status=409,
                )
            project = self._normalize(payload, project_id=project_id)
            now = int(time.time())
            project["created_at"] = now
            project["updated_at"] = now
            self._write_project(path, project)
            return copy.deepcopy(project)

    def update_project(self, project_id: str, payload: Any) -> dict[str, Any]:
        self._require_object(payload, "Project")
        with self._lock:
            path = self._path_for(project_id)
            if not path.is_file():
                raise ProjectLibraryError(
                    "project_not_found",
                    f"Project {project_id!r} was not found",
                    status=404,
                )
            if "id" in payload and payload["id"] != project_id:
                raise ProjectLibraryError("project_id_immutable", "Project id cannot be changed")
            if "project_id" in payload and payload["project_id"] != project_id:
                raise ProjectLibraryError("project_id_immutable", "Project id cannot be changed")
            current = self._read_project(path)
            merged = {**current, **copy.deepcopy(payload), "id": project_id}
            merged["project_id"] = project_id
            project = self._normalize(merged, project_id=project_id)
            project["created_at"] = current.get("created_at", 0)
            project["updated_at"] = int(time.time())
            self._write_project(path, project)
            return copy.deepcopy(project)

    def copy_project(self, project_id: str, payload: Any = None) -> dict[str, Any]:
        if payload is not None and not isinstance(payload, dict):
            raise ProjectLibraryError("invalid_project", "copy payload must be an object")
        with self._lock:
            original = self.get_project(project_id)
            overrides = copy.deepcopy(payload or {})
            overrides.pop("id", None)
            overrides.pop("project_id", None)
            overrides.pop("created_at", None)
            overrides.pop("updated_at", None)
            new_id = overrides.pop("new_id", None) or f"project-{uuid.uuid4().hex[:12]}"
            self._validate_id(new_id)
            if self._path_for(new_id).exists():
                raise ProjectLibraryError("project_exists", f"Project {new_id!r} already exists", status=409)
            copied = {**original, **overrides, "id": new_id}
            copied["name"] = self._copy_name(original["name"])
            copied.pop("project_id", None)
            copied = self._normalize(copied, project_id=new_id)
            now = int(time.time())
            copied["created_at"] = now
            copied["updated_at"] = now
            self._write_project(self._path_for(new_id), copied)
            return copy.deepcopy(copied)

    def import_card(self, payload: Any) -> dict[str, Any]:
        self._require_object(payload, "Project import")
        document = payload.get("document", payload.get("card_data", payload.get("data")))
        if not isinstance(document, dict):
            raise ProjectLibraryError("invalid_project_import", "document must be a JSON object")
        overrides = {key: copy.deepcopy(value) for key, value in payload.items() if key not in {"document", "card_data", "data"}}
        overrides["card_data"] = document
        return self.create_project(overrides)

    def _normalize(self, payload: dict[str, Any], *, project_id: str) -> dict[str, Any]:
        self._validate_id(project_id)
        card_data = payload.get("card_data")
        if card_data is None:
            card_data = payload.get("source_card")
        if card_data is None and any(key in payload for key in ("data", "first_mes", "alternate_greetings")):
            card_data = payload
        facts = self._card_facts(card_data)
        # A normalized ``card`` object is accepted for API clients that do not
        # have a source card.  Raw import fields are never copied wholesale.
        normalized_card = payload.get("card")
        if isinstance(normalized_card, dict):
            facts = {**facts, **normalized_card}

        name = self._first_value(payload, facts, "name", default=project_id)
        if not isinstance(name, str) or not name.strip():
            raise ProjectLibraryError("invalid_project", "name must be a non-empty string")

        strings = {}
        for field in ("avatar", "description", "personality", "scenario"):
            value = self._first_value(payload, facts, field, default="")
            if not isinstance(value, str):
                raise ProjectLibraryError("invalid_project", f"{field} must be a string")
            strings[field] = value

        prompt = self._normalize_card_prompt(payload, facts)
        openings = self._normalize_openings(payload, facts)
        variables = self._first_value(payload, facts, "variables", default={})
        if not isinstance(variables, dict):
            raise ProjectLibraryError("invalid_project", "variables must be an object")
        assets = self._first_value(payload, facts, "assets", default=[])
        if not isinstance(assets, (dict, list)):
            raise ProjectLibraryError("invalid_project", "assets must be an object or array")
        graph_id = self._first_value(
            payload,
            facts,
            "graph_id",
            "selected_graph_id",
            "selected_graph",
            default=None,
        )
        if isinstance(graph_id, dict):
            graph_id = graph_id.get("id") or graph_id.get("graph_id")
        if graph_id is not None and (not isinstance(graph_id, str) or not graph_id.strip()):
            raise ProjectLibraryError("invalid_project", "graph_id must be a string or null")

        worldbook_ids = self._worldbook_ids(payload, facts)
        self._validate_worldbooks(worldbook_ids)
        turn_adapter = payload.get("turn_adapter", facts.get("turn_adapter"))
        if turn_adapter is None:
            turn_adapter = {
                "id": "rp",
                "config": {
                    "opening_instruction": "{{node_instruction}}",
                    "required_final_node_role": None,
                },
            }
        if not isinstance(turn_adapter, dict):
            raise ProjectLibraryError("invalid_project", "turn_adapter must be an object")
        adapter_id = turn_adapter.get("id") or "rp"
        adapter_config = turn_adapter.get("config") or {}
        if not isinstance(adapter_id, str) or not adapter_id.strip():
            raise ProjectLibraryError("invalid_project", "turn_adapter.id must be a string")
        if not isinstance(adapter_config, dict):
            raise ProjectLibraryError("invalid_project", "turn_adapter.config must be an object")
        opening_instruction = adapter_config.get("opening_instruction", "")
        required_role = adapter_config.get("required_final_node_role")
        if not isinstance(opening_instruction, str):
            raise ProjectLibraryError("invalid_project", "turn_adapter opening_instruction must be a string")
        if required_role is not None and not isinstance(required_role, str):
            raise ProjectLibraryError("invalid_project", "turn_adapter required_final_node_role must be a string or null")
        return {
            "id": project_id,
            "name": name.strip(),
            **strings,
            "card_prompt": prompt,
            "openings": openings,
            "variables": copy.deepcopy(variables),
            "assets": copy.deepcopy(assets),
            "graph_id": graph_id.strip() if isinstance(graph_id, str) else None,
            "worldbook_ids": worldbook_ids,
            "turn_adapter": {
                "id": adapter_id.strip(),
                "config": {
                    **copy.deepcopy(adapter_config),
                    "opening_instruction": opening_instruction,
                    "required_final_node_role": required_role,
                },
            },
            "created_at": self._timestamp(payload.get("created_at")),
            "updated_at": self._timestamp(payload.get("updated_at")),
        }

    def _normalize_card_prompt(self, payload: dict[str, Any], facts: dict[str, Any]) -> dict[str, str]:
        raw_prompt = payload.get("card_prompt")
        if raw_prompt is None:
            raw_prompt = facts.get("card_prompt")
        if raw_prompt is None:
            raw_prompt = {}
        if not isinstance(raw_prompt, dict):
            raise ProjectLibraryError("invalid_project", "card_prompt must be an object")
        system = self._first_value(
            raw_prompt,
            {},
            "system",
            "system_prompt",
            default=self._first_value(payload, facts, "system_prompt", default=""),
        )
        post_history = self._first_value(
            raw_prompt,
            {},
            "post_history",
            "post_history_instruction",
            "post_history_instructions",
            default=self._first_value(
                payload,
                facts,
                "post_history",
                "post_history_instruction",
                "post_history_instructions",
                default="",
            ),
        )
        if not isinstance(system, str) or not isinstance(post_history, str):
            raise ProjectLibraryError("invalid_project", "card prompt fields must be strings")
        return {"system": system, "post_history": post_history}

    def _normalize_openings(self, payload: dict[str, Any], facts: dict[str, Any]) -> list[dict[str, Any]]:
        raw_openings = self._first_value(payload, {}, "openings", default=None)
        if raw_openings is None:
            raw_openings = facts.get("openings")
        if raw_openings is None:
            raw_openings = self._source_openings(facts)
        if raw_openings is None:
            return []
        if not isinstance(raw_openings, list):
            raise ProjectLibraryError("invalid_project", "openings must be an array")

        openings: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_openings):
            if isinstance(raw, str):
                raw = {"content": raw}
            if not isinstance(raw, dict):
                raise ProjectLibraryError("invalid_project", "openings must contain objects or strings")
            opening_id = raw.get("id")
            if opening_id is None:
                opening_id = f"opening-{index + 1}"
            elif isinstance(opening_id, (int, float)) and not isinstance(opening_id, bool):
                opening_id = str(opening_id)
            if not isinstance(opening_id, str) or not opening_id.strip():
                raise ProjectLibraryError("invalid_project", "opening id must be a non-empty string")
            content = raw.get("content", raw.get("text", raw.get("message", "")))
            if not isinstance(content, str):
                raise ProjectLibraryError("invalid_project", "opening content must be a string")
            label = raw.get("label", raw.get("title", f"Opening {index + 1}"))
            if not isinstance(label, str):
                raise ProjectLibraryError("invalid_project", "opening label must be a string")
            is_default = raw.get("is_default", raw.get("default", raw.get("isDefault", False)))
            if not isinstance(is_default, bool):
                raise ProjectLibraryError("invalid_project", "opening is_default must be a boolean")
            variables = raw.get("variables")
            if variables is not None and not isinstance(variables, dict):
                raise ProjectLibraryError("invalid_project", "opening variables must be an object")
            normalized: dict[str, Any] = {
                "id": opening_id.strip(),
                "label": label.strip() or f"Opening {index + 1}",
                "content": content,
                "is_default": is_default,
            }
            if variables is not None:
                normalized["variables"] = copy.deepcopy(variables)
            openings.append(normalized)
        if openings and not any(opening["is_default"] for opening in openings):
            openings[0]["is_default"] = True
        elif openings:
            seen_default = False
            for opening in openings:
                if opening["is_default"] and seen_default:
                    opening["is_default"] = False
                elif opening["is_default"]:
                    seen_default = True
        return openings

    @staticmethod
    def _source_openings(facts: dict[str, Any]) -> list[dict[str, Any]] | None:
        first = facts.get("first_mes")
        alternate = facts.get("alternate_greetings")
        if first in (None, "") and not alternate:
            return None
        if first is not None and not isinstance(first, str):
            raise ProjectLibraryError("invalid_project", "first message must be a string")
        values: list[Any] = []
        if first not in (None, ""):
            values.append({"id": "opening-1", "label": first[:20], "content": first, "is_default": True})
        if alternate is not None:
            if not isinstance(alternate, list) or any(not isinstance(item, str) for item in alternate):
                raise ProjectLibraryError("invalid_project", "alternate greetings must be an array of strings")
            values.extend(
                {
                    "id": f"opening-{index + 2}",
                    "label": greeting[:20],
                    "content": greeting,
                    "is_default": False,
                }
                for index, greeting in enumerate(alternate)
            )
        return values

    def _worldbook_ids(self, payload: dict[str, Any], facts: dict[str, Any]) -> list[str]:
        raw_ids = self._first_value(payload, {}, "worldbook_ids", default=None)
        if raw_ids is None:
            raw_ids = self._first_value(payload, {}, "worldbook_bindings", "worldbooks", default=None)
        if raw_ids is None:
            raw_ids = facts.get("worldbook_ids", facts.get("worldbook_bindings", []))
        if not isinstance(raw_ids, list):
            raise ProjectLibraryError("invalid_project", "worldbook_ids must be an array")
        ids: list[str] = []
        for item in raw_ids:
            if isinstance(item, dict):
                item = item.get("id", item.get("worldbook_id"))
            if not isinstance(item, str) or not item.strip():
                raise ProjectLibraryError("invalid_project", "worldbook_ids must contain strings")
            item = item.strip()
            if item not in ids:
                ids.append(item)
        return ids

    def _validate_worldbooks(self, worldbook_ids: list[str]) -> None:
        for worldbook_id in worldbook_ids:
            try:
                self.worldbooks.get_worldbook(worldbook_id)
            except WorldbookLibraryError as exc:
                raise ProjectLibraryError(exc.code, str(exc), status=exc.status) from exc

    @staticmethod
    def _card_facts(card_data: Any) -> dict[str, Any]:
        if not isinstance(card_data, dict):
            return {}
        nested = card_data.get("data")
        if isinstance(nested, dict):
            return {**card_data, **nested}
        return dict(card_data)

    @staticmethod
    def _first_value(payload: dict[str, Any], facts: dict[str, Any], *keys: str, default: Any = None) -> Any:
        for key in keys:
            if key in payload:
                return payload[key]
        for key in keys:
            if key in facts:
                return facts[key]
        return default

    def _read_project(self, path: Path) -> dict[str, Any]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProjectLibraryError("invalid_project_data", f"cannot read Project {path.stem!r}") from exc
        if not isinstance(raw, dict):
            raise ProjectLibraryError("invalid_project_data", f"Project {path.stem!r} is not an object")
        return self._normalize(raw, project_id=path.stem) | {
            "created_at": self._timestamp(raw.get("created_at")),
            "updated_at": self._timestamp(raw.get("updated_at")),
        }

    def _copy_name(self, name: str) -> str:
        names = {project["name"].casefold() for project in self.list_projects()}
        base = f"{name}-copy"
        candidate = base
        index = 2
        while candidate.casefold() in names:
            candidate = f"{base}-{index}"
            index += 1
        return candidate

    def _paths(self) -> list[Path]:
        return sorted(self.project_root.glob("*.json")) if self.project_root.is_dir() else []

    def _path_for(self, project_id: str) -> Path:
        self._validate_id(project_id)
        self.project_root.mkdir(parents=True, exist_ok=True)
        path = (self.project_root / f"{project_id}.json").resolve()
        try:
            path.relative_to(self.project_root.resolve())
        except ValueError as exc:
            raise ProjectLibraryError("invalid_project", "invalid Project id") from exc
        return path

    def _write_project(self, path: Path, project: dict[str, Any]) -> None:
        self.project_root.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=self.project_root)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(project, handle, ensure_ascii=False, indent=2)
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
    def _validate_id(project_id: Any) -> None:
        if not isinstance(project_id, str) or not PROJECT_ID_RE.fullmatch(project_id):
            raise ProjectLibraryError("invalid_project", "id must be a safe Project identifier")

    @staticmethod
    def _require_object(value: Any, label: str) -> None:
        if not isinstance(value, dict):
            raise ProjectLibraryError("invalid_project", f"{label} must be an object")

    @staticmethod
    def _timestamp(value: Any) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0
