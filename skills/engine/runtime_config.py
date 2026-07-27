from __future__ import annotations

import copy
import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from engine.context_compiler import ManifestSlot, PromptPreset, macro_context


CONFIG_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_TEMPLATE_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}")
_ALLOWED_ROLES = frozenset({"system", "user", "assistant"})
_ALLOWED_PLACEMENTS = frozenset({"relative", "in_chat"})
_ALLOWED_PROVIDERS = frozenset({"deepseek"})


class RuntimeConfigError(ValueError):
    pass


@dataclass(frozen=True)
class FrozenRuntimeConfig:
    data: dict

    @property
    def preset_id(self) -> str:
        return self.data["preset_id"]

    @property
    def graph_id(self) -> str:
        return self.data["graph_id"]


class RuntimeConfigStore:
    """File-backed runtime configuration with per-task immutable snapshots."""

    def __init__(self, styles_root, *, preset_root=None, graph_root=None):
        self.styles_root = Path(styles_root).resolve()
        self.preset_root = Path(preset_root).resolve() if preset_root else self.styles_root / "presets"
        self.graph_root = Path(graph_root).resolve() if graph_root else self.styles_root / "graphs"
        self.settings_path = self.styles_root / "settings.json"

    def selection(self) -> dict[str, str]:
        settings = self._read_json(self.settings_path)
        runtime = settings.get("runtime") if isinstance(settings, dict) else {}
        if not isinstance(runtime, dict):
            runtime = {}
        preset_id = runtime.get("preset_id") or runtime.get("presetId") or "default"
        graph_id = runtime.get("graph_id") or runtime.get("graphId") or "default"
        self._validate_id(preset_id, "preset")
        self._validate_id(graph_id, "graph")
        return {"preset_id": preset_id, "graph_id": graph_id}

    def freeze(self) -> FrozenRuntimeConfig:
        selected = self.selection()
        preset_path = self.preset_root / f"{selected['preset_id']}.json"
        graph_path = self.graph_root / f"{selected['graph_id']}.json"
        preset = self._normalize_preset(self._read_json(preset_path), selected["preset_id"], preset_path)
        graph = self._normalize_graph(self._read_json(graph_path), selected["graph_id"], graph_path)
        data = {
            **selected,
            "settings": copy.deepcopy(self._read_json(self.settings_path)),
            "preset": preset,
            "graph": graph,
            "sources": {
                "settings": self._source(self.settings_path),
                "preset": self._source(preset_path),
                "graph": self._source(graph_path),
            },
        }
        return FrozenRuntimeConfig(copy.deepcopy(data))

    def _normalize_preset(self, raw: Any, expected_id: str, path: Path) -> dict:
        if not isinstance(raw, dict):
            raise RuntimeConfigError(f"preset must be an object: {path}")
        preset_id = raw.get("id") or expected_id
        if preset_id != expected_id:
            raise RuntimeConfigError(f"preset id mismatch: expected {expected_id!r}")
        version = str(raw.get("version") or "1")
        entries = raw.get("entries")
        if not isinstance(entries, list) or not entries:
            raise RuntimeConfigError("preset entries must be a non-empty array")
        normalized = []
        seen = set()
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise RuntimeConfigError(f"preset entry {index} must be an object")
            entry_id = entry.get("id")
            self._validate_id(entry_id, "preset entry")
            if entry_id in seen:
                raise RuntimeConfigError(f"duplicate preset entry id: {entry_id}")
            seen.add(entry_id)
            role = entry.get("role", "user")
            placement = entry.get("placement", "relative")
            depth = entry.get("depth", 0)
            order = entry.get("order", index)
            if role not in _ALLOWED_ROLES:
                raise RuntimeConfigError(f"invalid role for {entry_id}: {role}")
            if placement not in _ALLOWED_PLACEMENTS:
                raise RuntimeConfigError(f"invalid placement for {entry_id}: {placement}")
            if not isinstance(depth, int) or depth < 0:
                raise RuntimeConfigError(f"invalid depth for {entry_id}")
            if not isinstance(order, int):
                raise RuntimeConfigError(f"invalid order for {entry_id}")
            raw_content, source = self._entry_source(entry, entry_id)
            placeholders = tuple(dict.fromkeys(_TEMPLATE_RE.findall(raw_content)))
            self._validate_placeholders(placeholders, entry_id)
            normalized.append(
                {
                    "id": entry_id,
                    "name": str(entry.get("name") or entry_id),
                    "kind": str(entry.get("kind") or entry_id),
                    "enabled": bool(entry.get("enabled", True)),
                    "role": role,
                    "placement": placement,
                    "depth": depth,
                    "order": order,
                    "stability": entry.get("stability", "stable"),
                    "inclusion_reason": str(entry.get("inclusion_reason") or "configured prompt entry"),
                    "condition": entry.get("condition"),
                    "raw_content": raw_content,
                    "placeholders": list(placeholders),
                    "source": source,
                    "declaration_index": index,
                }
            )
        return {
            "id": preset_id,
            "version": version,
            "token_budget": _positive_int(raw.get("token_budget"), 32000, "token_budget"),
            "entries": normalized,
        }

    def _normalize_graph(self, raw: Any, expected_id: str, path: Path) -> dict:
        if not isinstance(raw, dict):
            raise RuntimeConfigError(f"graph must be an object: {path}")
        graph_id = raw.get("id") or expected_id
        if graph_id != expected_id:
            raise RuntimeConfigError(f"graph id mismatch: expected {expected_id!r}")
        nodes = raw.get("nodes")
        if not isinstance(nodes, list) or not nodes:
            raise RuntimeConfigError("graph nodes must be a non-empty array")
        normalized = []
        seen = set()
        for index, node in enumerate(nodes):
            if not isinstance(node, dict):
                raise RuntimeConfigError(f"graph node {index} must be an object")
            node_id = node.get("id")
            self._validate_id(node_id, "graph node")
            if node_id in seen:
                raise RuntimeConfigError(f"duplicate graph node id: {node_id}")
            seen.add(node_id)
            provider = node.get("provider", "deepseek")
            if provider not in _ALLOWED_PROVIDERS:
                raise RuntimeConfigError(f"unsupported graph provider: {provider}")
            normalized.append(
                {
                    "id": node_id,
                    "role": str(node.get("role") or "narrative_director"),
                    "enabled": bool(node.get("enabled", True)),
                    "order": node.get("order", index),
                    "provider": provider,
                    "model": str(node.get("model") or "deepseek-v4-flash"),
                    "max_tool_rounds": _positive_int(node.get("max_tool_rounds"), 8, "max_tool_rounds"),
                    "max_retries": _nonnegative_int(node.get("max_retries"), 2, "max_retries"),
                    "instruction": str(node.get("instruction") or ""),
                    "declaration_index": index,
                }
            )
        if any(not isinstance(node["order"], int) for node in normalized):
            raise RuntimeConfigError("graph node order must be an integer")
        normalized.sort(key=lambda node: (node["order"], node["declaration_index"]))
        if not any(node["enabled"] for node in normalized):
            raise RuntimeConfigError("graph must have at least one enabled node")
        enabled = [node for node in normalized if node["enabled"]]
        roles = [node["role"] for node in enabled]
        if len(roles) != len(set(roles)):
            raise RuntimeConfigError("enabled graph node roles must be unique")
        if enabled[-1]["role"] != "narrative_director":
            raise RuntimeConfigError("the final enabled graph node must be narrative_director")
        return {
            "id": graph_id,
            "version": str(raw.get("version") or "1"),
            "mode": "sequential",
            "commit_validation_retries": _positive_int(
                raw.get("commit_validation_retries"), 3, "commit_validation_retries"
            ),
            "nodes": normalized,
        }

    def _entry_source(self, entry: dict, entry_id: str) -> tuple[str, dict]:
        source = entry.get("source")
        if source is None and "content" in entry:
            source = {"type": "inline", "content": entry.get("content")}
        if not isinstance(source, dict):
            raise RuntimeConfigError(f"entry {entry_id} requires content or source")
        source_type = source.get("type")
        if source_type == "inline":
            content = source.get("content")
            if not isinstance(content, str):
                raise RuntimeConfigError(f"inline source for {entry_id} must be text")
            return content, {"type": "inline", "id": entry_id, "version": _hash_text(content)}
        if source_type == "markdown":
            relative = source.get("path")
            if not isinstance(relative, str) or not relative.strip():
                raise RuntimeConfigError(f"markdown source for {entry_id} requires path")
            target = (self.styles_root / relative).resolve()
            try:
                target.relative_to(self.styles_root)
            except ValueError as exc:
                raise RuntimeConfigError(f"markdown path escapes styles root: {relative}") from exc
            if target.suffix.lower() != ".md":
                raise RuntimeConfigError(f"markdown source must use .md: {relative}")
            try:
                content = target.read_text(encoding="utf-8")
            except OSError as exc:
                raise RuntimeConfigError(f"cannot read markdown source: {relative}") from exc
            return content, {
                "type": "markdown",
                "id": relative,
                "path": relative,
                "version": _hash_text(content),
            }
        raise RuntimeConfigError(f"unsupported source type for {entry_id}: {source_type}")

    @staticmethod
    def _validate_placeholders(placeholders: tuple[str, ...], entry_id: str) -> None:
        for name in placeholders:
            root = name.split(".", 1)[0]
            if root not in _placeholder_roots():
                raise RuntimeConfigError(f"unknown placeholder {name!r} in {entry_id}")

    @staticmethod
    def _validate_id(value: Any, label: str) -> None:
        if not isinstance(value, str) or not CONFIG_ID_RE.fullmatch(value):
            raise RuntimeConfigError(f"invalid {label} id: {value!r}")

    @staticmethod
    def _read_json(path: Path) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise RuntimeConfigError(f"runtime config file is unavailable: {path}") from exc
        except json.JSONDecodeError as exc:
            raise RuntimeConfigError(f"runtime config is invalid JSON: {path}: {exc}") from exc

    @staticmethod
    def _source(path: Path) -> dict:
        try:
            raw = path.read_bytes()
        except OSError:
            raw = b""
        return {"id": str(path.name), "path": str(path), "version": hashlib.sha256(raw).hexdigest()}


def prompt_preset_from_snapshot(runtime_config: dict | None) -> PromptPreset | None:
    if not isinstance(runtime_config, dict):
        return None
    spec = runtime_config.get("preset")
    if not isinstance(spec, dict):
        return None
    slots = [_slot_from_entry(entry) for entry in _ordered_entries(spec.get("entries") or [])]
    return PromptPreset(id=spec["id"], version=spec["version"], slots=tuple(slots))


def runtime_config_manifest(runtime_config: dict | None) -> dict | None:
    if not isinstance(runtime_config, dict):
        return None
    preset = runtime_config.get("preset") or {}
    graph = runtime_config.get("graph") or {}
    sources = runtime_config.get("sources") or {}
    return {
        "preset_id": runtime_config.get("preset_id"),
        "preset_version": preset.get("version"),
        "preset_source": sources.get("preset"),
        "graph_id": runtime_config.get("graph_id"),
        "graph_version": graph.get("version"),
        "graph_mode": graph.get("mode"),
        "graph_source": sources.get("graph"),
        "graph_nodes": [
            {
                "id": node.get("id"),
                "role": node.get("role"),
                "order": node.get("order"),
                "model": node.get("model"),
                "instruction": node.get("instruction", ""),
            }
            for node in graph.get("nodes", [])
            if node.get("enabled", True)
        ],
    }


def _slot_from_entry(entry: dict) -> ManifestSlot:
    raw_content = entry["raw_content"]
    placeholders = tuple(entry.get("placeholders") or ())

    def resolve(request):
        expanded = _expand_template(raw_content, request)
        source = dict(entry.get("source") or {})
        source.update(
            {
                "entry_id": entry["id"],
                "raw_hash": _hash_text(raw_content),
                "expanded_hash": _hash_value(expanded),
                "placeholders": list(placeholders),
            }
        )
        return expanded, source

    condition = entry.get("condition")

    def include_when(request):
        if condition in (None, "always"):
            return True
        if condition == "nonempty":
            value = _expand_template(raw_content, request)
            return value not in (None, "", [], {})
        raise RuntimeConfigError(f"unsupported entry condition: {condition}")

    return ManifestSlot(
        kind=entry["kind"],
        stability=entry.get("stability", "stable"),
        inclusion_reason=entry.get("inclusion_reason", "configured prompt entry"),
        resolve=resolve,
        enabled=entry.get("enabled", True),
        include_when=include_when,
        role=entry.get("role", "user"),
        prompt_entry={
            "id": entry["id"],
            "placement": entry.get("placement", "relative"),
            "depth": entry.get("depth", 0),
            "order": entry.get("order", 0),
            "raw_hash": _hash_text(raw_content),
            "placeholders": list(placeholders),
        },
    )


def _ordered_entries(entries: list[dict]) -> list[dict]:
    relative = sorted(
        (entry for entry in entries if entry.get("placement", "relative") == "relative"),
        key=lambda entry: (entry.get("order", 0), entry.get("declaration_index", 0)),
    )
    ordered = list(relative)
    in_chat = sorted(
        (entry for entry in entries if entry.get("placement") == "in_chat"),
        key=lambda entry: (entry.get("order", 0), entry.get("declaration_index", 0)),
    )
    for entry in in_chat:
        index = max(0, len(ordered) - entry.get("depth", 0))
        ordered.insert(index, entry)
    return ordered


def _expand_template(raw: str, request):
    exact = _TEMPLATE_RE.fullmatch(raw.strip())
    if exact:
        return _placeholder_value(exact.group(1), request)

    def replace(match):
        value = _placeholder_value(match.group(1), request)
        if value is None:
            return ""
        if isinstance(value, (dict, list)):
            return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        if isinstance(value, bool):
            return "true" if value else "false"
        return str(value)

    return _TEMPLATE_RE.sub(replace, raw)


def _placeholder_value(name: str, request):
    snapshot = request.snapshot or {}
    root, _, remainder = name.partition(".")
    if root == "player_input":
        value = request.player_input
    elif root == "worldbook_entries":
        value = [
            {
                "title": load["title"],
                "content": load["content"],
                "content_hash": load["content_hash"],
                "catalog_hash": load["catalog_hash"],
                "reference_hash": load["reference_hash"],
                "reason": load["reason"],
            }
            for load in request.worldbook_loads
        ]
    elif root == "variable_baseline":
        value = snapshot.get("initvar", {})
    elif root in {"style", "nsfw", "person", "charName", "user"}:
        value = macro_context(request).get(root, "")
    else:
        value = snapshot.get(root, _default_placeholder_value(root))
    if remainder:
        for part in remainder.split("."):
            if not isinstance(value, dict):
                return ""
            value = value.get(part, "")
    return value


def _placeholder_roots() -> frozenset[str]:
    return frozenset(
        {
            "card_facts",
            "settings",
            "worldbook_catalog",
            "card_structure",
            "variable_baseline",
            "current_state",
            "recent_memory",
            "recent_turns",
            "worldbook_entries",
            "player_input",
            "style",
            "nsfw",
            "person",
            "charName",
            "user",
        }
    )


def _default_placeholder_value(root: str):
    if root in {"worldbook_catalog", "recent_turns"}:
        return []
    if root in {"card_facts", "settings", "card_structure", "current_state"}:
        return {}
    return ""


def _positive_int(value, default: int, label: str) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or value <= 0:
        raise RuntimeConfigError(f"{label} must be a positive integer")
    return value


def _nonnegative_int(value, default: int, label: str) -> int:
    if value is None:
        return default
    if not isinstance(value, int) or value < 0:
        raise RuntimeConfigError(f"{label} must be a non-negative integer")
    return value


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _hash_value(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
