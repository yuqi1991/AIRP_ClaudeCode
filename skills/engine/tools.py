"""engine.tools — typed domain tool registry for the narrative director.

The narrative director may invoke a CLOSED allowlist of tools:

    get_session_snapshot     — read session revision / status / recent turns
    get_recent_memory        — read recent memory text (bounded)
    load_worldbook_entry     — exact-title worldbook load (policy-enforced)
    validate_state_proposal  — dry-run MVU apply (no mutation)
    commit_turn_draft        — the ONLY tool that produces a committed turn

There is deliberately NO generic Bash, NO arbitrary filesystem-write tool, and
NO unrestricted network tool. The allowlist is asserted closed by tests.

All arguments are schema-validated BEFORE any domain work; an invalid call
yields a stable :class:`ToolResult` error and performs NO state mutation. Tool
invocations are traced via ``tool_run.started`` / ``tool_run.finished`` runtime
events carrying ``args_hash``, duration and a redacted view of the args — long
content (draft prose, worldbook bodies) and secret keys never appear verbatim
(spec Implementation Decisions 17, 19, 36).
"""

import hashlib
import json
import time
from dataclasses import dataclass


# ═══ Result type ═══


@dataclass(frozen=True)
class ToolResult:
    """Stable outcome of a tool invocation.

    ``ok=False`` always carries a stable ``error`` code (``tool_validation_error``
    / ``unknown_tool`` / ``stale_revision`` / ``worldbook_load_limit`` / …);
    callers and tests never parse provider strings.
    """

    ok: bool
    value: dict | None = None
    error: str | None = None


class _ToolError(Exception):
    """Internal control-flow exception carrying a stable error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ═══ Redaction ═══


_SECRET_KEYS = {
    "api_key",
    "apikey",
    "secret",
    "token",
    "password",
    "authorization",
    "credential",
    "credentials",
}


def _canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _hash_args(args) -> str:
    return hashlib.sha256(_canonical(args).encode("utf-8")).hexdigest()


def _redact(value):
    """Recursively redact secret keys and collapse long strings into a marker.

    Long content (draft prose, worldbook bodies, full proposals) is collapsed
    to ``<str len=N hash=...>`` so trace events stay useful for debugging
    without leaking bulk narrative text or any secret that may have slipped
    into args.
    """
    if isinstance(value, dict):
        return {
            k: ("<redacted>" if isinstance(k, str) and k.lower() in _SECRET_KEYS
                else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, str):
        if len(value) > 64:
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
            return f"<str len={len(value)} hash={digest}>"
        return value
    return value


# ═══ Schemas (hand-rolled; typed allowlist is part of the contract) ═══


_TYPE_CHECKS = {
    "str": lambda v: isinstance(v, str),
    "int": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "list": lambda v: isinstance(v, list),
    "dict": lambda v: isinstance(v, dict),
}


TOOL_SCHEMAS = {
    "get_session_snapshot": {
        "description": "Read the current session revision, task status and a short recent-turn summary.",
        "required": {},
        "optional": {"revision": "int"},
    },
    "get_recent_memory": {
        "description": "Read recent project memory, trimmed to max_chars.",
        "required": {},
        "optional": {"max_chars": "int"},
    },
    "load_worldbook_entry": {
        "description": "Load a single worldbook entry by exact catalog title.",
        "required": {"title": "str"},
        "optional": {"reason": "str"},
    },
    "validate_state_proposal": {
        "description": "Dry-run validate a JSONPatch state proposal; performs no mutation.",
        "required": {"proposal": "list"},
        "optional": {},
    },
    "commit_turn_draft": {
        "description": "Commit the structured turn draft as the authoritative turn. The ONLY committing tool.",
        "required": {"draft": "dict", "expected_revision": "int"},
        "optional": {},
    },
}


class ToolRegistry:
    """Closed, schema-validated tool surface presented to a narrative director."""

    ALLOWED = tuple(TOOL_SCHEMAS.keys())

    def __init__(self, runtime, task, policy):
        self._runtime = runtime
        self._task = task
        self._policy = policy

    # --- introspection ---

    def names(self) -> tuple:
        return self.ALLOWED

    def schemas(self) -> list:
        out = []
        for name in self.ALLOWED:
            schema = TOOL_SCHEMAS[name]
            out.append(
                {
                    "name": name,
                    "description": schema["description"],
                    "parameters": {
                        "required": sorted(schema["required"].keys()),
                        "optional": sorted(schema["optional"].keys()),
                        "types": {**schema["required"], **schema["optional"]},
                    },
                }
            )
        return out

    # --- dispatch ---

    # Read-only tools delegated to the runtime (domain owner). ``commit_turn_draft``
    # is special-cased because it must validate the draft shape before any
    # domain work and routes through the tool-path commit service.
    _READONLY_RUNTIME_DISPATCH = {
        "get_session_snapshot": "_tool_session_snapshot",
        "get_recent_memory": "_tool_recent_memory",
        "load_worldbook_entry": "_tool_load_worldbook",
        "validate_state_proposal": "_tool_validate_state",
    }

    def call(self, name: str, args) -> ToolResult:
        """Validate, trace and dispatch a single tool invocation.

        Returns a :class:`ToolResult`; never raises for argument / unknown-tool
        / domain-validation failures (those become ``ok=False``). Internal
        programmer errors still propagate.
        """
        started = time.monotonic()
        args_dict = args if isinstance(args, dict) else {}
        args_hash = _hash_args(args_dict)
        redacted = _redact(args_dict)
        self._runtime._emit_tool_started(self._task["id"], name, args_hash, redacted)
        validation_error = _validate_args(name, args_dict)
        if validation_error is not None:
            duration = time.monotonic() - started
            self._runtime._emit_tool_finished(
                self._task["id"], name, args_hash, redacted, ok=False,
                error=validation_error, duration=duration,
            )
            return ToolResult(ok=False, error=validation_error)
        try:
            result = self._dispatch(name, args_dict)
        except _ToolError as exc:
            duration = time.monotonic() - started
            self._runtime._emit_tool_finished(
                self._task["id"], name, args_hash, redacted, ok=False,
                error=exc.code, duration=duration,
            )
            return ToolResult(ok=False, error=exc.code)
        duration = time.monotonic() - started
        self._runtime._emit_tool_finished(
            self._task["id"], name, args_hash, redacted, ok=bool(result.ok),
            error=result.error, duration=duration,
        )
        return result

    def _dispatch(self, name: str, args: dict) -> ToolResult:
        if name == "commit_turn_draft":
            validate_draft_dict(args["draft"])
            return self._runtime._commit_via_tool(
                self._task, args["draft"], args["expected_revision"]
            )
        runtime_method = self._READONLY_RUNTIME_DISPATCH.get(name)
        if runtime_method is None:
            raise _ToolError(f"unknown_tool:{name}")
        return getattr(self._runtime, runtime_method)(self._task, args)


# ═══ Argument + draft validation (module-level so tests can exercise directly) ═══


def _validate_args(name: str, args) -> str | None:
    """Return a stable error code, or None if args are acceptable for ``name``."""
    schema = TOOL_SCHEMAS.get(name)
    if schema is None:
        return f"unknown_tool:{name}"
    if not isinstance(args, dict):
        return "tool_validation_error:args_not_object"
    for key, typ in schema["required"].items():
        if key not in args:
            return f"tool_validation_error:missing:{key}"
        if not _TYPE_CHECKS[typ](args[key]):
            return f"tool_validation_error:type:{key}"
    for key, typ in schema["optional"].items():
        if key in args and not _TYPE_CHECKS[typ](args[key]):
            return f"tool_validation_error:type:{key}"
    return None


def validate_draft_dict(draft_dict) -> None:
    """Validate the ``draft`` payload of ``commit_turn_draft``.

    Raises :class:`_ToolError` with a stable code on any malformed shape so
    that NO domain work runs before validation.
    """
    if not isinstance(draft_dict, dict):
        raise _ToolError("tool_validation_error:draft_not_object")
    content = draft_dict.get("content")
    if not isinstance(content, str) or not content.strip():
        raise _ToolError("tool_validation_error:draft_content_empty")
    for key in ("summary", "options", "polished_input", "mvu_commands"):
        value = draft_dict.get(key, "")
        if value is None:
            continue
        if not isinstance(value, str):
            raise _ToolError(f"tool_validation_error:type:{key}")
