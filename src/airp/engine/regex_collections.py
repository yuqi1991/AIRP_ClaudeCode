"""Persistent, Agent-owned JavaScript Regex Collection definitions.

This module owns the user-visible collection schema and persistence boundary.
It deliberately does not execute regular expressions; the Runtime transformer
will consume the normalized rule list in a later slice.
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
from typing import Any, Callable

from airp.engine.revisions import append_audit, conflict_payload, expected_revision, revision


REGEX_COLLECTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
REGEX_RULE_ID_RE = REGEX_COLLECTION_ID_RE
VALID_TARGETS = frozenset({"input", "output", "both"})


class RegexCollectionError(ValueError):
    """A user-facing Regex Collection validation or persistence failure."""

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


class RegexCollectionLibrary:
    """File-backed CRUD library for reusable Regex Collections.

    ``reference_callback`` keeps deletion dependency checks independent from
    the Agent Definition store. It receives an id and returns reference
    dictionaries, for example ``{"type": "agent", "id": "writer"}``.
    """

    def __init__(
        self,
        static_root: str | Path,
        *,
        library_root: str | Path | None = None,
        workspace=None,
        reference_callback: Callable[[str], list[dict[str, str]]] | None = None,
    ) -> None:
        self.static_root = Path(static_root).resolve()
        workspace_root = getattr(workspace, "regex_collections_root", None)
        self.library_root = (
            Path(library_root).resolve()
            if library_root is not None
            else (
                Path(workspace_root).resolve()
                if workspace_root is not None
                else self.static_root / "studio" / "regex_collections"
            )
        )
        self.reference_callback = reference_callback
        self._lock = threading.RLock()

    def list_collections(self) -> list[dict[str, Any]]:
        with self._lock:
            collections = [self._read_collection(path) for path in self._paths()]
        return sorted(collections, key=lambda item: (item["name"].casefold(), item["id"]))

    def get_collection(self, collection_id: str) -> dict[str, Any]:
        path = self._path_for(collection_id)
        with self._lock:
            if not path.is_file():
                raise RegexCollectionError(
                    "regex_collection_not_found",
                    f"Regex Collection {collection_id!r} was not found",
                    status=404,
                )
            return self._read_collection(path)

    def create_collection(self, payload: Any) -> dict[str, Any]:
        self._require_object(payload, "Regex Collection")
        with self._lock:
            collection_id = payload.get("id") or f"regex-{uuid.uuid4().hex[:12]}"
            self._validate_id(collection_id, "Regex Collection")
            path = self._path_for(collection_id)
            if path.exists():
                raise RegexCollectionError(
                    "regex_collection_exists",
                    f"Regex Collection {collection_id!r} already exists",
                    status=409,
                )
            collection = self._normalize_for_write(payload, collection_id=collection_id)
            collection["name"] = self._allocate_name(collection["name"])
            now = int(time.time())
            collection["created_at"] = now
            collection["updated_at"] = now
            collection["revision"] = 1
            self._atomic_write(path, collection)
            append_audit(
                self.library_root, object_type="regex_collection", object_id=collection_id,
                parent_revision=0, new_revision=1, before=None, after=collection,
                source=payload.get("_source", "api"),
            )
            return copy.deepcopy(collection)

    def update_collection(self, collection_id: str, payload: Any) -> dict[str, Any]:
        self._require_object(payload, "Regex Collection")
        with self._lock:
            path = self._path_for(collection_id)
            if not path.is_file():
                raise RegexCollectionError(
                    "regex_collection_not_found",
                    f"Regex Collection {collection_id!r} was not found",
                    status=404,
                )
            if "id" in payload and payload["id"] != collection_id:
                raise RegexCollectionError("regex_collection_id_immutable", "collection id cannot be changed")
            current = self._read_collection(path)
            expected = expected_revision(payload)
            if expected is not None and expected != current.get("revision", 0):
                raise RegexCollectionError(
                    "revision_conflict", f"Regex Collection {collection_id!r} revision conflict", status=409,
                    details=conflict_payload("regex_collection", collection_id, expected, current.get("revision", 0)),
                )
            merged = {**current, **copy.deepcopy(payload), "id": collection_id}
            collection = self._normalize_for_write(merged, collection_id=collection_id)
            collection["name"] = self._allocate_name(collection["name"], exclude_id=collection_id)
            collection["created_at"] = current.get("created_at", 0)
            collection["updated_at"] = int(time.time())
            collection["revision"] = current.get("revision", 0) + 1
            self._atomic_write(path, collection)
            append_audit(
                self.library_root, object_type="regex_collection", object_id=collection_id,
                parent_revision=current.get("revision", 0), new_revision=collection["revision"],
                before=current, after=collection, source=payload.get("_source", "api"),
            )
            return copy.deepcopy(collection)

    def copy_collection(self, collection_id: str, payload: Any = None) -> dict[str, Any]:
        if payload is not None and not isinstance(payload, dict):
            raise RegexCollectionError("invalid_regex_collection", "copy payload must be an object")
        with self._lock:
            original = self.get_collection(collection_id)
            overrides = copy.deepcopy(payload or {})
            overrides.pop("id", None)
            overrides.pop("created_at", None)
            overrides.pop("updated_at", None)
            new_id = overrides.pop("new_id", None) or f"regex-{uuid.uuid4().hex[:12]}"
            self._validate_id(new_id, "Regex Collection")
            if self._path_for(new_id).exists():
                raise RegexCollectionError(
                    "regex_collection_exists",
                    f"Regex Collection {new_id!r} already exists",
                    status=409,
                )
            rules = []
            for rule in original["rules"]:
                copied_rule = {**rule, "id": f"rule-{uuid.uuid4().hex[:12]}"}
                rules.append(copied_rule)
            copied = {**original, **overrides, "id": new_id, "rules": rules}
            copied["name"] = self._allocate_name(original["name"])
            return self.create_collection(copied)

    def delete_collection(self, collection_id: str) -> None:
        with self._lock:
            path = self._path_for(collection_id)
            if not path.is_file():
                raise RegexCollectionError(
                    "regex_collection_not_found",
                    f"Regex Collection {collection_id!r} was not found",
                    status=404,
                )
            references = self._references_for(collection_id)
            if references:
                raise RegexCollectionError(
                    "regex_collection_in_use",
                    f"Regex Collection {collection_id!r} is still referenced",
                    status=409,
                    references=references,
                )
            path.unlink()

    def test_collection(
        self,
        collection_id: str,
        payload: Any = None,
        *,
        text: str = "",
        target: str = "output",
    ) -> dict[str, Any]:
        """Run a collection against sample text through the JS transformer."""
        if payload is None:
            collection = self.get_collection(collection_id)
        else:
            self._require_object(payload, "Regex Collection test")
            collection = self._normalize_for_write(payload, collection_id=collection_id)
        if isinstance(payload, dict):
            text = payload.get("text", text)
            target = payload.get("target", target)
        if not isinstance(text, str):
            raise RegexCollectionError("invalid_regex_test", "test text must be a string")
        if target not in {"input", "output"}:
            raise RegexCollectionError("invalid_regex_test", "test target must be input or output")
        try:
            from airp.engine.regex_transformer import RegexTransformer

            result = RegexTransformer(collection["rules"]).transform(text, target=target)
        except Exception as exc:
            details = exc.to_dict() if hasattr(exc, "to_dict") else {"message": str(exc)}
            raise RegexCollectionError(
                details.get("code", "regex_transform_failed"),
                details.get("message", str(exc)),
            ) from exc
        return result.to_dict()

    def _normalize_for_write(self, payload: dict[str, Any], *, collection_id: str) -> dict[str, Any]:
        self._validate_id(collection_id, "Regex Collection")
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            raise RegexCollectionError("invalid_regex_collection", "name must be a non-empty string")
        raw_rules = payload.get("rules", [])
        if not isinstance(raw_rules, list):
            raise RegexCollectionError("invalid_regex_collection", "rules must be an array")
        rules: list[dict[str, Any]] = []
        rule_ids: set[str] = set()
        for index, raw_rule in enumerate(raw_rules):
            rule = self._normalize_rule(raw_rule, index)
            while rule["id"] in rule_ids:
                rule["id"] = f"rule-{uuid.uuid4().hex[:12]}"
            rule_ids.add(rule["id"])
            rules.append(rule)
        return {
            "id": collection_id,
            "name": name.strip(),
            "rules": rules,
            "created_at": self._timestamp(payload.get("created_at")),
            "updated_at": self._timestamp(payload.get("updated_at")),
        }

    def _normalize_rule(self, raw: Any, index: int) -> dict[str, Any]:
        self._require_object(raw, "Regex rule")
        rule_id = raw.get("id") or f"rule-{uuid.uuid4().hex[:12]}"
        self._validate_id(rule_id, "Regex rule")
        name = raw.get("name", f"Rule {index + 1}")
        if not isinstance(name, str) or not name.strip():
            raise RegexCollectionError("invalid_regex_collection", "rule name must be a non-empty string")
        enabled = raw.get("enabled", True)
        if not isinstance(enabled, bool):
            raise RegexCollectionError("invalid_regex_collection", "rule enabled must be a boolean")
        target = raw.get("target", "both")
        if target not in VALID_TARGETS:
            raise RegexCollectionError(
                "invalid_regex_collection",
                "target must be input, output, or both",
            )
        pattern = raw.get("pattern", "")
        flags = raw.get("flags", "")
        replacement = raw.get("replacement", "")
        if not isinstance(pattern, str):
            raise RegexCollectionError("invalid_regex_collection", "rule pattern must be a string")
        if not isinstance(flags, str):
            raise RegexCollectionError("invalid_regex_collection", "rule flags must be a string")
        if not isinstance(replacement, str):
            raise RegexCollectionError("invalid_regex_collection", "rule replacement must be a string")
        return {
            "id": rule_id,
            "name": name.strip(),
            "enabled": enabled,
            "target": target,
            "pattern": pattern,
            "flags": flags,
            "replacement": replacement,
        }

    def _references_for(self, collection_id: str) -> list[dict[str, str]]:
        if self.reference_callback is None:
            return []
        references = self.reference_callback(collection_id)
        if not isinstance(references, list):
            raise TypeError("Regex Collection reference callback must return a list")
        return copy.deepcopy(references)

    def _allocate_name(self, name: str, *, exclude_id: str | None = None) -> str:
        names = {
            item["name"].casefold()
            for item in self.list_collections()
            if item["id"] != exclude_id
        }
        if name.casefold() not in names:
            return name
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

    def _path_for(self, collection_id: str) -> Path:
        self._validate_id(collection_id, "Regex Collection")
        self.library_root.mkdir(parents=True, exist_ok=True)
        path = (self.library_root / f"{collection_id}.json").resolve()
        try:
            path.relative_to(self.library_root)
        except ValueError as exc:
            raise RegexCollectionError("invalid_regex_collection", "invalid Regex Collection id") from exc
        return path

    def _read_collection(self, path: Path) -> dict[str, Any]:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RegexCollectionError(
                "invalid_regex_collection",
                f"cannot read Regex Collection {path.stem!r}",
            ) from exc
        if not isinstance(raw, dict):
            raise RegexCollectionError(
                "invalid_regex_collection",
                f"Regex Collection {path.stem!r} is not an object",
            )
        collection_id = raw.get("id", path.stem)
        return self._normalize_for_write(raw, collection_id=collection_id) | {
            "created_at": self._timestamp(raw.get("created_at")),
            "updated_at": self._timestamp(raw.get("updated_at")),
            "revision": revision(raw.get("revision"), 0),
        }

    @staticmethod
    def _require_object(value: Any, label: str) -> None:
        if not isinstance(value, dict):
            raise RegexCollectionError("invalid_regex_collection", f"{label} must be an object")

    @staticmethod
    def _validate_id(value: Any, label: str) -> None:
        if not isinstance(value, str) or not REGEX_COLLECTION_ID_RE.fullmatch(value):
            raise RegexCollectionError(
                "invalid_regex_collection",
                f"{label} id must be a safe library identifier",
            )

    @staticmethod
    def _timestamp(value: Any) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    def _atomic_write(self, path: Path, payload: dict[str, Any]) -> None:
        self.library_root.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=self.library_root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
