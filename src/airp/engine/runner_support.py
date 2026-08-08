"""Content-neutral helpers shared by Agent executor adapters."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def serialize_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def notify(observer: Any, method: str, *args: Any) -> None:
    callback = getattr(observer, method, None) if observer is not None else None
    if callable(callback):
        try:
            callback(*args)
        except Exception:
            return


def tools_for(node: Any, execution_context: Any = None) -> list[dict[str, Any]]:
    registry = execution_context.tool_registry if execution_context is not None else None
    if registry is not None and callable(getattr(registry, "schemas", None)):
        return [dict(tool) for tool in registry.schemas(node.tool_allowlist)]
    effective = node.agent.effective_config
    tools = effective.get("tools") if isinstance(effective, dict) else None
    if isinstance(tools, list):
        return [dict(tool) for tool in tools if isinstance(tool, dict)]
    return [{"name": name} for name in node.agent.tool_allowlist]


def parameters_for(node: Any) -> dict[str, Any]:
    parameters: dict[str, Any] = {}
    for source in (node.agent.generation, node.agent.advanced):
        if isinstance(source, Mapping):
            parameters.update(source)
    return parameters
