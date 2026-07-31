"""Explicit capability registration for Graph Node execution.

The engine does not scan the filesystem for skills and does not own RP data.
Hosts register the capabilities available to a run, then bind an Agent's
allowlist at the Node Runner seam. This keeps tool discovery deterministic and
makes a frozen Graph Run independent from later registry changes.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from collections.abc import Callable, Iterable, Mapping
from typing import Any


_JSON_TYPE_ALIASES = {
    "str": "string",
    "string": "string",
    "int": "integer",
    "integer": "integer",
    "float": "number",
    "number": "number",
    "bool": "boolean",
    "boolean": "boolean",
    "list": "array",
    "array": "array",
    "dict": "object",
    "object": "object",
}


def provider_parameters(schema: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize a host schema into OpenAI-compatible object JSON Schema."""
    if not isinstance(schema, Mapping):
        return {"type": "object", "properties": {}}
    if isinstance(schema.get("parameters"), Mapping):
        schema = schema["parameters"]
    if isinstance(schema.get("properties"), Mapping):
        return copy.deepcopy(dict(schema))
    required = schema.get("required") if isinstance(schema.get("required"), Mapping) else {}
    optional = schema.get("optional") if isinstance(schema.get("optional"), Mapping) else {}
    properties: dict[str, Any] = {}
    for name, type_name in required.items():
        properties[str(name)] = _property_schema(type_name)
    for name, type_name in optional.items():
        properties[str(name)] = _property_schema(type_name)
    result: dict[str, Any] = {"type": "object", "properties": properties}
    if required:
        result["required"] = [str(name) for name in required]
    return result


def _property_schema(type_name: Any) -> dict[str, Any]:
    if isinstance(type_name, Mapping):
        return copy.deepcopy(dict(type_name))
    normalized = _JSON_TYPE_ALIASES.get(str(type_name).casefold(), "string")
    return {"type": normalized}


class CapabilityError(RuntimeError):
    """Stable error for unknown, forbidden, or failed capability calls."""

    def __init__(self, code: str, message: str | None = None) -> None:
        self.code = code
        super().__init__(message or code)


@dataclass(frozen=True)
class CapabilityDefinition:
    """Provider-neutral description of one callable capability."""

    name: str
    description: str = ""
    parameters: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("capability name must be a non-empty string")
        object.__setattr__(self, "name", self.name.strip())
        object.__setattr__(self, "parameters", copy.deepcopy(dict(self.parameters or {})))

    def as_provider_tool(self) -> dict[str, Any]:
        """Return the portable function shape accepted by both provider APIs."""
        parameters = provider_parameters(self.parameters)
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": parameters,
            },
        }


@dataclass(frozen=True)
class _RegisteredCapability:
    definition: CapabilityDefinition
    handler: Callable[[dict[str, Any]], Any]


class CapabilityRegistry:
    """A host-owned, explicit registry of callable Agent capabilities.

    Registration happens before a Graph Run is started. ``schemas`` and
    ``dispatch`` accept the Agent allowlist so callers cannot accidentally
    expose a capability merely because it exists in the host registry.
    """

    def __init__(self, capabilities: Iterable[tuple[CapabilityDefinition, Callable]] = ()) -> None:
        self._capabilities: dict[str, _RegisteredCapability] = {}
        for definition, handler in capabilities:
            self.register(definition, handler)

    def register(self, definition: CapabilityDefinition, handler: Callable[[dict[str, Any]], Any]) -> None:
        if not isinstance(definition, CapabilityDefinition):
            raise TypeError("capability definition is required")
        if not callable(handler):
            raise TypeError("capability handler must be callable")
        if definition.name in self._capabilities:
            raise ValueError(f"duplicate capability: {definition.name}")
        self._capabilities[definition.name] = _RegisteredCapability(definition, handler)

    def names(self, allowed: Iterable[str] | None = None) -> tuple[str, ...]:
        selected = self._select(allowed)
        return tuple(selected)

    def schemas(self, allowed: Iterable[str] | None = None) -> list[dict[str, Any]]:
        return [self._capabilities[name].definition.as_provider_tool() for name in self._select(allowed)]

    def dispatch(
        self,
        name: str,
        args: Mapping[str, Any] | None = None,
        *,
        allowed: Iterable[str] | None = None,
    ) -> Any:
        if name not in self._capabilities:
            raise CapabilityError(f"unknown_capability:{name}")
        if allowed is not None and name not in {str(item) for item in allowed}:
            raise CapabilityError(f"capability_not_allowed:{name}")
        payload = dict(args or {})
        try:
            return self._capabilities[name].handler(payload)
        except CapabilityError:
            raise
        except Exception as exc:
            raise CapabilityError(f"capability_failed:{name}", str(exc)) from exc

    def _select(self, allowed: Iterable[str] | None) -> list[str]:
        if allowed is None:
            return list(self._capabilities)
        return [name for name in (str(item) for item in allowed) if name in self._capabilities]
