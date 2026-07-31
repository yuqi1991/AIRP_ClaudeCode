"""Deterministic template expansion for Agent instructions and runtime context."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any


MACRO_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_.-]*)\s*\}\}")
DEFAULT_MACRO_ROOTS = frozenset(
    {
        "project",
        "project_id",
        "session",
        "task",
        "graph",
        "node",
        "settings",
        "runtime",
        "card_facts",
        "card_structure",
        "character",
        "character_card",
        "card",
        "charName",
        "char",
        "character_name",
        "user",
        "user_name",
        "player",
        "player_input",
        "project_input",
        "initvar",
        "current_state",
        "recent_memory",
        "recent_turns",
        "worldbooks",
        "worldbook_catalog",
        "handoff",
        "output_contract",
        "tool_protocol",
        "skills",
    }
)


def build_context(*sources: Mapping[str, Any] | None, request: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Merge runtime sources and add stable aliases used by prompt templates."""
    context: dict[str, Any] = {}
    for source in sources:
        if isinstance(source, Mapping):
            context.update(source)
    if isinstance(request, Mapping):
        context.update(request)

    snapshot = context.get("snapshot")
    if isinstance(snapshot, Mapping):
        merged = dict(snapshot)
        merged.update(context)
        context = merged
    settings = context.get("settings")
    settings = settings if isinstance(settings, Mapping) else {}
    card_facts = context.get("card_facts")
    card_facts = card_facts if isinstance(card_facts, Mapping) else {}
    worldbook_catalog = context.get("worldbook_catalog")
    worldbook_catalog = worldbook_catalog if isinstance(worldbook_catalog, (list, tuple)) else []

    context.setdefault("project_input", context.get("player_input", ""))
    context.setdefault("player_input", context.get("project_input", ""))
    context.setdefault("user", settings.get("user") or context.get("user", ""))
    context.setdefault("charName", settings.get("charName") or card_facts.get("name", ""))
    context.setdefault("character", card_facts)
    context.setdefault("character_card", card_facts)
    context.setdefault("card", card_facts)
    context.setdefault("char", context.get("charName", ""))
    context.setdefault("character_name", context.get("charName", ""))
    context.setdefault("user_name", context.get("user", ""))
    context.setdefault("player", context.get("user", ""))
    # Worldbook bodies are intentionally absent from the default macro
    # surface. Agents receive the catalog here; a tool-loaded entry can be
    # supplied explicitly as ``worldbook_entries`` when the runtime has a
    # reason to include it.
    context.setdefault("worldbooks", context.get("worldbook_entries", worldbook_catalog))
    context.setdefault("runtime", {})
    context.setdefault("project", {"id": context.get("project_id", "")})
    return context


def resolve_macro(name: str, context: Mapping[str, Any]) -> Any:
    """Resolve direct or dotted paths; missing values become an empty string."""
    if name in context:
        return context[name]
    value: Any = context
    for part in name.split("."):
        if isinstance(value, Mapping):
            value = value.get(part)
        else:
            return ""
        if value is None:
            return ""
    return value


def has_macro(name: str, context: Mapping[str, Any]) -> bool:
    """Return whether a direct or dotted macro path exists in the context."""
    if name in context:
        return True
    value: Any = context
    for part in name.split("."):
        if not isinstance(value, Mapping) or part not in value:
            return False
        value = value[part]
    return True


def available_macro_roots(context: Mapping[str, Any] | None = None) -> list[str]:
    """Return stable built-ins plus roots supplied by the runtime context."""
    return sorted(DEFAULT_MACRO_ROOTS | set(context or {}))


def stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (Mapping, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def expand_template(value: Any, context: Mapping[str, Any], *, preserve_unknown: bool = True) -> Any:
    """Expand ``{{path.to.value}}`` in strings and recursively in containers."""
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            resolved = resolve_macro(name, context)
            if preserve_unknown and not has_macro(name, context):
                return match.group(0)
            return stringify(resolved)

        return MACRO_RE.sub(replace, value)
    if isinstance(value, list):
        return [expand_template(item, context, preserve_unknown=preserve_unknown) for item in value]
    if isinstance(value, tuple):
        return tuple(expand_template(item, context, preserve_unknown=preserve_unknown) for item in value)
    if isinstance(value, Mapping):
        return {
            key: expand_template(item, context, preserve_unknown=preserve_unknown)
            for key, item in value.items()
        }
    return value
