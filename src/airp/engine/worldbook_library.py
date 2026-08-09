"""Global Worldbook Definitions and Project-scoped bindings.

Import formats end at this boundary. Callers receive one AIRP shape and the
runtime consumes a frozen skill-mode snapshot without knowing library files.
"""

from __future__ import annotations

import hashlib
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


LIBRARY_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
AIRP_FORMAT = "airp-worldbook"
AIRP_VERSION = 1


class WorldbookLibraryError(ValueError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        references: list[dict[str, str]] | None = None,
        details: dict[str, Any] | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.status = status
        self.references = references or []
        self.details = details or None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"ok": False, "error": self.code, "message": str(self)}
        if self.references:
            result["references"] = self.references
        if self.details is not None:
            result.update(self.details)
        return result


class WorldbookLibrary:
    """Own reusable Worldbook Definitions and Project binding records."""

    def __init__(self, static_root: str | Path, *, library_root: str | Path | None = None, workspace=None):
        self.static_root = Path(static_root).resolve()
        studio_root = self.static_root / "studio"
        workspace_worldbooks_root = getattr(workspace, "worldbooks_root", None)
        workspace_projects_root = getattr(workspace, "projects_root", None)
        self.library_root = (
            Path(library_root).resolve()
            if library_root
            else (Path(workspace_worldbooks_root).resolve() if workspace_worldbooks_root else studio_root / "worldbooks")
        )
        self.project_root = Path(workspace_projects_root).resolve() if workspace_projects_root else studio_root / "projects"
        self._lock = threading.RLock()

    def list_worldbooks(self) -> list[dict[str, Any]]:
        with self._lock:
            books = [self._read_worldbook(path) for path in self._worldbook_paths()]
        return sorted(books, key=lambda item: (item["name"].casefold(), item["id"]))

    def get_worldbook(self, worldbook_id: str) -> dict[str, Any]:
        path = self._worldbook_path(worldbook_id)
        with self._lock:
            if not path.is_file():
                raise WorldbookLibraryError(
                    "worldbook_not_found", f"Worldbook {worldbook_id!r} was not found", status=404
                )
            return self._read_worldbook(path)

    def create_worldbook(self, payload: Any) -> tuple[dict[str, Any], list[dict[str, str]]]:
        self._require_object(payload, "Worldbook")
        with self._lock:
            worldbook_id = payload.get("id") or f"worldbook-{uuid.uuid4().hex[:12]}"
            self._validate_id(worldbook_id, "Worldbook")
            path = self._worldbook_path(worldbook_id)
            if path.exists():
                raise WorldbookLibraryError(
                    "worldbook_exists", f"Worldbook {worldbook_id!r} already exists", status=409
                )
            worldbook, renamed = self._normalize_for_write(payload, worldbook_id=worldbook_id)
            now = int(time.time())
            worldbook.update(created_at=now, updated_at=now)
            worldbook["revision"] = 1
            self._atomic_write(path, worldbook)
            append_audit(
                self.library_root, object_type="worldbook", object_id=worldbook_id,
                parent_revision=0, new_revision=1, before=None, after=worldbook,
                source=payload.get("_source", "api"),
            )
            return worldbook, renamed

    def update_worldbook(
        self, worldbook_id: str, payload: Any
    ) -> tuple[dict[str, Any], list[dict[str, str]]]:
        self._require_object(payload, "Worldbook")
        with self._lock:
            path = self._worldbook_path(worldbook_id)
            if not path.is_file():
                raise WorldbookLibraryError(
                    "worldbook_not_found", f"Worldbook {worldbook_id!r} was not found", status=404
                )
            current = self._read_worldbook(path)
            expected = expected_revision(payload)
            if expected is not None and expected != current.get("revision", 0):
                raise WorldbookLibraryError(
                    "revision_conflict", f"Worldbook {worldbook_id!r} revision conflict", status=409,
                    details=conflict_payload("worldbook", worldbook_id, expected, current.get("revision", 0), current),
                )
            merged = {**current, **payload, "id": worldbook_id}
            worldbook, renamed = self._normalize_for_write(
                merged, worldbook_id=worldbook_id, exclude_worldbook_id=worldbook_id
            )
            worldbook["created_at"] = current["created_at"]
            worldbook["updated_at"] = int(time.time())
            worldbook["revision"] = current.get("revision", 0) + 1
            self._atomic_write(path, worldbook)
            append_audit(
                self.library_root, object_type="worldbook", object_id=worldbook_id,
                parent_revision=current.get("revision", 0), new_revision=worldbook["revision"],
                before=current, after=worldbook, source=payload.get("_source", "api"),
            )
            return worldbook, renamed

    def copy_worldbook(self, worldbook_id: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
        with self._lock:
            original = self.get_worldbook(worldbook_id)
            payload = {
                "name": self._copy_name(original["name"]),
                "entries": [{**entry, "id": f"entry-{uuid.uuid4().hex[:12]}"} for entry in original["entries"]],
            }
            return self.create_worldbook(payload)

    def delete_worldbook(self, worldbook_id: str) -> None:
        with self._lock:
            path = self._worldbook_path(worldbook_id)
            if not path.is_file():
                raise WorldbookLibraryError(
                    "worldbook_not_found", f"Worldbook {worldbook_id!r} was not found", status=404
                )
            references = self._references_for(worldbook_id)
            if references:
                raise WorldbookLibraryError(
                    "worldbook_in_use",
                    f"Worldbook {worldbook_id!r} is still referenced",
                    status=409,
                    references=references,
                )
            path.unlink()

    def export_worldbook(self, worldbook_id: str) -> dict[str, Any]:
        worldbook = self.get_worldbook(worldbook_id)
        return {
            "format": AIRP_FORMAT,
            "version": AIRP_VERSION,
            "worldbook": {
                "id": worldbook["id"],
                "name": worldbook["name"],
                "entries": worldbook["entries"],
            },
        }

    def import_worldbook(self, payload: Any) -> tuple[dict[str, Any], str, list[dict[str, str]]]:
        self._require_object(payload, "Worldbook import")
        document = payload.get("document", payload.get("data", payload.get("worldbook")))
        if not isinstance(document, dict):
            raise WorldbookLibraryError("invalid_worldbook_import", "document must be a JSON object")

        if document.get("format") == AIRP_FORMAT:
            if document.get("version") != AIRP_VERSION or not isinstance(document.get("worldbook"), dict):
                raise WorldbookLibraryError("invalid_worldbook_import", "unsupported AIRP Worldbook version")
            source_format = AIRP_FORMAT
            source = document["worldbook"]
            raw_entries = source.get("entries")
            default_name = source.get("name")
        else:
            card_data = document.get("data") if isinstance(document.get("data"), dict) else {}
            embedded = card_data.get("character_book", document.get("character_book"))
            if isinstance(embedded, dict):
                source_format = "sillytavern-character-book"
                raw_entries = embedded.get("entries")
                default_name = embedded.get("name") or card_data.get("name") or document.get("name")
            elif isinstance(document.get("entries"), (dict, list)):
                source_format = "sillytavern-world-info"
                raw_entries = document["entries"]
                default_name = document.get("name")
            else:
                raise WorldbookLibraryError(
                    "invalid_worldbook_import",
                    "JSON is not an embedded character_book, SillyTavern World Info, or AIRP Worldbook",
                )

        entries = self._import_entries(raw_entries)
        name = payload.get("name") or default_name or "Imported Worldbook"
        worldbook, renamed = self.create_worldbook({"name": name, "entries": entries})
        return worldbook, source_format, renamed

    def list_projects(self) -> list[dict[str, Any]]:
        with self._lock:
            projects = [self._read_project(path) for path in self._project_paths()]
        return sorted(projects, key=lambda item: (item["name"].casefold(), item["id"]))

    def get_project_bindings(self, project_id: str) -> dict[str, Any]:
        path = self._project_path(project_id)
        with self._lock:
            if not path.is_file():
                return {"id": project_id, "name": project_id, "worldbook_ids": []}
            return self._read_project(path)

    def set_project_bindings(self, project_id: str, payload: Any) -> dict[str, Any]:
        self._require_object(payload, "Project binding")
        raw_ids = payload.get("worldbook_ids")
        if not isinstance(raw_ids, list):
            raise WorldbookLibraryError("invalid_project_binding", "worldbook_ids must be an array")
        with self._lock:
            path = self._project_path(project_id)
            current = self._read_project(path) if path.is_file() else {
                "id": project_id,
                "name": project_id,
                "worldbook_ids": [],
                "revision": 0,
            }
            expected = expected_revision(payload)
            if expected is not None and expected != current.get("revision", 0):
                raise WorldbookLibraryError(
                    "revision_conflict", f"Project binding {project_id!r} revision conflict", status=409,
                    details=conflict_payload("project_worldbooks", project_id, expected, current.get("revision", 0), current),
                )
            worldbook_ids: list[str] = []
            for worldbook_id in raw_ids:
                self._validate_id(worldbook_id, "Worldbook")
                if not self._worldbook_path(worldbook_id).is_file():
                    raise WorldbookLibraryError(
                        "worldbook_not_found", f"Worldbook {worldbook_id!r} was not found", status=404
                    )
                if worldbook_id not in worldbook_ids:
                    worldbook_ids.append(worldbook_id)
            name = payload.get("name", current.get("name", project_id))
            if not isinstance(name, str) or not name.strip():
                raise WorldbookLibraryError("invalid_project_binding", "name must be a non-empty string")
            project = {
                **current,
                "id": project_id,
                "name": name.strip(),
                "worldbook_ids": worldbook_ids,
                "revision": current.get("revision", 0) + 1,
            }
            self._atomic_write(path, project)
            append_audit(
                self.project_root, object_type="project_worldbooks", object_id=project_id,
                parent_revision=current.get("revision", 0), new_revision=project["revision"],
                before=current, after=project, source=payload.get("_source", "api"),
            )
            return project

    def effective_worldbooks(self, project_id: str) -> list[dict[str, Any]]:
        project = self.get_project_bindings(project_id)
        return [self.get_worldbook(worldbook_id) for worldbook_id in project["worldbook_ids"]]

    def snapshot_for_project(self, project_id: str) -> dict[str, Any] | None:
        if not self._project_path(project_id).is_file():
            return None
        books = self.effective_worldbooks(project_id)
        ordered: list[tuple[int, int, dict[str, Any]]] = []
        sequence = 0
        for book in books:
            for entry in book["entries"]:
                if entry["enabled"]:
                    ordered.append((entry["order"], sequence, entry))
                    sequence += 1
        entries = [item[2] for item in sorted(ordered, key=lambda item: (item[0], item[1]))]
        catalog = [{"title": entry["title"], "section": f"## {entry['title']}", "usage": entry["usage"]} for entry in entries]
        reference = self._markdown(entry for entry in entries if "{{user}}" not in entry["title"])
        user = self._markdown(entry for entry in entries if "{{user}}" in entry["title"])
        canonical = json.dumps(books, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return {
            "worldbook_catalog": catalog,
            "worldbook_reference": reference,
            "worldbook_user": user,
            "source": {
                "id": f"studio-project:{project_id}:worldbooks",
                "version": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            },
        }

    def _normalize_for_write(
        self,
        payload: dict[str, Any],
        *,
        worldbook_id: str,
        exclude_worldbook_id: str | None = None,
    ) -> tuple[dict[str, Any], list[dict[str, str]]]:
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            raise WorldbookLibraryError("invalid_worldbook", "name must be a non-empty string")
        raw_entries = payload.get("entries", [])
        if not isinstance(raw_entries, list):
            raise WorldbookLibraryError("invalid_worldbook", "entries must be an array")

        reserved = self._entry_titles(exclude_worldbook_id=exclude_worldbook_id)
        entry_ids: set[str] = set()
        entries: list[dict[str, Any]] = []
        renamed: list[dict[str, str]] = []
        for index, raw in enumerate(raw_entries):
            entry = self._normalize_entry(raw, index)
            while entry["id"] in entry_ids:
                entry["id"] = f"entry-{uuid.uuid4().hex[:12]}"
            entry_ids.add(entry["id"])
            allocated = self._allocate_copy(entry["title"], reserved)
            if allocated != entry["title"]:
                renamed.append({"from": entry["title"], "to": allocated})
                entry["title"] = allocated
            reserved.add(allocated)
            entries.append(entry)
        return {
            "id": worldbook_id,
            "name": name.strip(),
            "entries": entries,
            "created_at": self._timestamp(payload.get("created_at")),
            "updated_at": self._timestamp(payload.get("updated_at")),
        }, renamed

    def _normalize_entry(self, raw: Any, index: int) -> dict[str, Any]:
        self._require_object(raw, "Worldbook entry")
        title = raw.get("title")
        usage = raw.get("usage", "")
        content = raw.get("content")
        enabled = raw.get("enabled", True)
        order = raw.get("order", index)
        tags = raw.get("tags", [])
        if not isinstance(title, str) or not title.strip():
            raise WorldbookLibraryError("invalid_worldbook", "entry title must be a non-empty string")
        if not isinstance(usage, str):
            raise WorldbookLibraryError("invalid_worldbook", "entry usage must be a string")
        if not isinstance(content, str):
            raise WorldbookLibraryError("invalid_worldbook", "entry content must be a string")
        if not isinstance(enabled, bool):
            raise WorldbookLibraryError("invalid_worldbook", "entry enabled must be a boolean")
        if not isinstance(order, int) or isinstance(order, bool):
            raise WorldbookLibraryError("invalid_worldbook", "entry order must be an integer")
        if not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags):
            raise WorldbookLibraryError("invalid_worldbook", "entry tags must be an array of strings")
        entry_id = raw.get("id")
        if isinstance(entry_id, int) and not isinstance(entry_id, bool):
            entry_id = str(entry_id)
        if entry_id is None:
            entry_id = f"entry-{uuid.uuid4().hex[:12]}"
        self._validate_id(entry_id, "Worldbook entry")
        clean_tags: list[str] = []
        for tag in tags:
            tag = tag.strip()
            if tag and tag not in clean_tags:
                clean_tags.append(tag)
        return {
            "id": entry_id,
            "title": title.strip(),
            "usage": usage.strip(),
            "content": content,
            "enabled": enabled,
            "order": order,
            "tags": clean_tags,
        }

    def _import_entries(self, raw_entries: Any) -> list[dict[str, Any]]:
        if isinstance(raw_entries, dict):
            values = list(raw_entries.values())
        elif isinstance(raw_entries, list):
            values = raw_entries
        else:
            raise WorldbookLibraryError("invalid_worldbook_import", "entries must be an array or object")
        result = []
        for index, raw in enumerate(values):
            if not isinstance(raw, dict):
                raise WorldbookLibraryError("invalid_worldbook_import", "entries must contain objects")
            extensions = raw.get("extensions") if isinstance(raw.get("extensions"), dict) else {}
            airp = extensions.get("airp") if isinstance(extensions.get("airp"), dict) else {}
            result.append(
                {
                    "id": raw.get("id", raw.get("uid")),
                    "title": raw.get("title") or raw.get("comment") or raw.get("name") or f"Entry {index + 1}",
                    "usage": raw.get("usage", airp.get("usage", "")),
                    "content": raw.get("content", ""),
                    "enabled": raw.get("enabled") if isinstance(raw.get("enabled"), bool) else not bool(raw.get("disable", False)),
                    "order": raw.get("order", raw.get("insertion_order", index)),
                    "tags": raw.get("tags", []),
                }
            )
        return result

    def _entry_titles(self, *, exclude_worldbook_id: str | None = None) -> set[str]:
        titles: set[str] = set()
        for path in self._worldbook_paths():
            if path.stem == exclude_worldbook_id:
                continue
            titles.update(entry["title"] for entry in self._read_worldbook(path)["entries"])
        return titles

    def _copy_name(self, name: str) -> str:
        return self._allocate_copy(name, {book["name"] for book in self.list_worldbooks()})

    @staticmethod
    def _allocate_copy(title: str, reserved: set[str]) -> str:
        if title not in reserved:
            return title
        candidate = f"{title}-copy"
        suffix = 2
        while candidate in reserved:
            candidate = f"{title}-copy-{suffix}"
            suffix += 1
        return candidate

    @staticmethod
    def _markdown(entries) -> str:
        return "\n\n".join(f"## {entry['title']}\n{entry['content']}" for entry in entries)

    def _references_for(self, worldbook_id: str) -> list[dict[str, str]]:
        references = []
        for project in self.list_projects():
            if worldbook_id in project["worldbook_ids"]:
                references.append({"type": "project", "id": project["id"], "name": project["name"]})
        return references

    def _read_worldbook(self, path: Path) -> dict[str, Any]:
        raw = self._read_json(path, "Worldbook")
        worldbook, _ = self._normalize_stored(raw, worldbook_id=path.stem)
        return worldbook

    def _normalize_stored(self, payload: dict[str, Any], *, worldbook_id: str):
        name = payload.get("name")
        entries = payload.get("entries")
        if not isinstance(name, str) or not name.strip() or not isinstance(entries, list):
            raise WorldbookLibraryError("invalid_worldbook", f"cannot read Worldbook {worldbook_id!r}")
        normalized = [self._normalize_entry(entry, index) for index, entry in enumerate(entries)]
        return ({
            "id": worldbook_id,
            "name": name.strip(),
            "entries": normalized,
            "created_at": self._timestamp(payload.get("created_at")),
            "updated_at": self._timestamp(payload.get("updated_at")),
            "revision": revision(payload.get("revision"), 1),
        }, [])

    def _read_project(self, path: Path) -> dict[str, Any]:
        raw = self._read_json(path, "Project binding")
        ids = raw.get("worldbook_ids")
        name = raw.get("name", path.stem)
        if not isinstance(ids, list) or any(not isinstance(item, str) for item in ids):
            raise WorldbookLibraryError("invalid_project_binding", f"cannot read Project {path.stem!r}")
        return {
            **raw,
            "id": path.stem,
            "name": name if isinstance(name, str) else path.stem,
            "worldbook_ids": ids,
            "revision": revision(raw.get("revision"), 0),
        }

    @staticmethod
    def _read_json(path: Path, label: str) -> dict[str, Any]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorldbookLibraryError("invalid_library_data", f"cannot read {label} {path.stem!r}") from exc
        if not isinstance(raw, dict):
            raise WorldbookLibraryError("invalid_library_data", f"{label} {path.stem!r} is not an object")
        return raw

    def _worldbook_paths(self) -> list[Path]:
        return sorted(self.library_root.glob("*.json")) if self.library_root.is_dir() else []

    def _project_paths(self) -> list[Path]:
        return sorted(self.project_root.glob("*.json")) if self.project_root.is_dir() else []

    def _worldbook_path(self, value: Any) -> Path:
        return self._safe_path(self.library_root, value, "Worldbook")

    def _project_path(self, value: Any) -> Path:
        return self._safe_path(self.project_root, value, "Project")

    def _safe_path(self, root: Path, value: Any, label: str) -> Path:
        self._validate_id(value, label)
        root.mkdir(parents=True, exist_ok=True)
        path = (root / f"{value}.json").resolve()
        try:
            path.relative_to(root.resolve())
        except ValueError as exc:
            raise WorldbookLibraryError("invalid_library_id", f"invalid {label} id") from exc
        return path

    @staticmethod
    def _validate_id(value: Any, label: str) -> None:
        if not isinstance(value, str) or not LIBRARY_ID_RE.fullmatch(value):
            raise WorldbookLibraryError("invalid_library_id", f"{label} id must be a safe library identifier")

    @staticmethod
    def _require_object(value: Any, label: str) -> None:
        if not isinstance(value, dict):
            raise WorldbookLibraryError("invalid_worldbook", f"{label} must be an object")

    @staticmethod
    def _timestamp(value: Any) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    @staticmethod
    def _atomic_write(path: Path, value: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
