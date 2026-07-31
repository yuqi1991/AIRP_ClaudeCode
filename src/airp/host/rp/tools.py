"""RP host tool registry for Session/Memory/Worldbook capabilities.

The narrative director may invoke a CLOSED allowlist of **read-only** tools:

    get_session_snapshot     — read session revision / status / recent turns
    get_recent_memory        — read recent memory text (bounded)
    load_worldbook_entry     — exact-title worldbook load (policy-enforced)

Commit is a **harness** action (ADR-0011), not a model tool. There is no
``commit_turn_draft`` or ``validate_state_proposal`` on the model surface.
There is deliberately NO generic Bash, NO arbitrary filesystem-write tool, and
NO unrestricted network tool. The allowlist is asserted closed by tests.

All arguments are schema-validated BEFORE any domain work; an invalid call
yields a stable :class:`ToolResult` error and performs NO state mutation. Tool
invocations are traced via ``tool_run.started`` / ``tool_run.finished`` runtime
events carrying ``args_hash``, duration and a redacted view of the args — long
content (worldbook bodies) and secret keys never appear verbatim
(spec Implementation Decisions 17, 19, 36).
"""

import hashlib
import json
import time
from dataclasses import dataclass
from collections.abc import Iterable, Mapping

from airp.engine.capabilities import provider_parameters

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


# Model-facing surface is read-only only (ADR-0011). Write/commit tools are
# intentionally absent — the host commits opaque final text.
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
}


class ToolRegistry:
    """Closed, schema-validated read-only tool surface for a narrative director."""

    ALLOWED = tuple(TOOL_SCHEMAS.keys())

    def __init__(
        self,
        runtime,
        task,
        policy,
        *,
        snapshot: Mapping | None = None,
        allowed_tools: Iterable[str] | None = None,
    ):
        self._runtime = runtime
        self._task = task
        self._policy = policy
        self._snapshot = dict(snapshot) if isinstance(snapshot, Mapping) else None
        self._allowed = (
            frozenset(str(name) for name in allowed_tools)
            if allowed_tools is not None
            else frozenset(self.ALLOWED)
        )

    # --- introspection ---

    def names(self, allowed_tools: Iterable[str] | None = None) -> tuple:
        allowed = self._allowed if allowed_tools is None else frozenset(str(name) for name in allowed_tools)
        return tuple(name for name in self.ALLOWED if name in allowed)

    def schemas(self, allowed_tools: Iterable[str] | None = None) -> list:
        out = []
        for name in self.names(allowed_tools):
            schema = TOOL_SCHEMAS[name]
            out.append(
                {
                    "name": name,
                    "description": schema["description"],
                    "parameters": provider_parameters(schema),
                }
            )
        return out

    def dispatch(
        self,
        name: str,
        args: Mapping | None = None,
        *,
        allowed: Iterable[str] | None = None,
    ) -> dict:
        "Expose the generic engine capability seam over this RP registry."
        if allowed is not None and name not in {str(item) for item in allowed}:
            return {"error": f"tool_not_allowed:{name}"}
        result = self.call(name, dict(args or {}))
        if result.ok:
            return result.value or {}
        return {"error": result.error, "value": result.value}

    # --- dispatch ---

    _READONLY_RUNTIME_DISPATCH = {
        "get_session_snapshot": "_tool_session_snapshot",
        "get_recent_memory": "_tool_recent_memory",
        "load_worldbook_entry": "_tool_load_worldbook",
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
        task_id = self._task.get("id") if isinstance(self._task, Mapping) else None
        if task_id:
            self._runtime._emit_tool_started(task_id, name, args_hash, redacted)
        validation_error = _validate_args(name, args_dict)
        if validation_error is not None:
            duration = time.monotonic() - started
            if task_id:
                self._runtime._emit_tool_finished(
                    task_id, name, args_hash, redacted, ok=False,
                    error=validation_error, duration=duration,
                )
            return ToolResult(ok=False, error=validation_error)
        try:
            result = self._dispatch(name, args_dict)
        except _ToolError as exc:
            duration = time.monotonic() - started
            if task_id:
                self._runtime._emit_tool_finished(
                    task_id, name, args_hash, redacted, ok=False,
                    error=exc.code, duration=duration,
                )
            return ToolResult(ok=False, error=exc.code)
        duration = time.monotonic() - started
        if task_id:
            self._runtime._emit_tool_finished(
                task_id, name, args_hash, redacted, ok=bool(result.ok),
                error=result.error, duration=duration,
            )
        return result

    def _dispatch(self, name: str, args: dict) -> ToolResult:
        if name not in self._allowed:
            raise _ToolError(f"tool_not_allowed:{name}")
        if self._snapshot is not None:
            return self._dispatch_snapshot(name, args)
        runtime_method = self._READONLY_RUNTIME_DISPATCH.get(name)
        if runtime_method is None:
            raise _ToolError(f"unknown_tool:{name}")
        return getattr(self._runtime, runtime_method)(self._task, args)

    def _dispatch_snapshot(self, name: str, args: dict) -> ToolResult:
        """Serve a Graph node from its immutable run snapshot.

        Normal Graph nodes must never consult mutable card files or Studio
        definitions after the Execution Plan is frozen. Legacy Director calls
        still use the runtime-backed dispatch above until that compatibility
        path is retired.
        """
        if name == "get_session_snapshot":
            revision = args.get("revision")
            task_id = self._task["id"] if self._task is not None else None
            task_status = self._task["status"] if self._task is not None else None
            base_revision = self._task["base_revision"] if self._task is not None else 0
            if revision is None:
                revision = self._snapshot.get("active_revision", base_revision)
            recent_turns = self._snapshot.get("recent_turns") or []
            return ToolResult(
                ok=True,
                value={
                    "session_id": self._runtime.session_id,
                    "active_revision": revision,
                    "task_id": task_id,
                    "task_status": task_status,
                    "recent_revisions": [
                        turn.get("revision")
                        for turn in recent_turns
                        if isinstance(turn, Mapping) and turn.get("revision") is not None
                    ],
                },
            )
        if name == "get_recent_memory":
            text = self._snapshot.get("recent_memory", "")
            max_chars = args.get("max_chars", 3000)
            if not isinstance(max_chars, int) or max_chars <= 0:
                max_chars = 3000
            truncated = len(text) > max_chars
            return ToolResult(
                ok=True,
                value={"memory": text[-max_chars:] if truncated else text, "truncated": truncated},
            )
        if name == "load_worldbook_entry":
            title = args.get("title")
            for book in self._snapshot.get("worldbooks") or []:
                if not isinstance(book, Mapping):
                    continue
                for entry in book.get("entries") or []:
                    if isinstance(entry, Mapping) and entry.get("title") == title:
                        return ToolResult(
                            ok=True,
                            value={
                                "title": entry.get("title"),
                                "content": entry.get("content", ""),
                                "content_hash": entry.get("content_hash"),
                            },
                        )
            return ToolResult(
                ok=False,
                error="worldbook_entry_not_found",
                value={"title": title},
            )
        raise _ToolError(f"unknown_tool:{name}")


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
    """Validate a structured draft dict before harness commit.

    Raises :class:`_ToolError` with a stable code on any malformed shape so
    that NO domain work runs before validation. Used by the harness commit
    path (not a model-facing tool).
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
