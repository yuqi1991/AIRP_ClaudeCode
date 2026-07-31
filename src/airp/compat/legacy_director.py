"""engine.director — narrative-director execution layer.

The narrative director is AIRP's single writing-role Agent for the tracer
bullet (spec Implementation Decision 1, 33). It is explicitly NOT a story
character or NPC. A run receives:

* a :class:`DirectorHandle` exposing the closed **read-only** typed-tool
  surface, an :class:`~engine.provider.AbortSignal`, a preview emitter and a
  small set of telemetry hooks;
* a :class:`~engine.context_compiler.CompiledContext` produced by the AIRP-owned
  Context Compiler just before the run.

The director drives the model via a :class:`~engine.provider.ProviderAdapter`
and produces text. Commit is a **host** action (ADR-0011): when the model
stops issuing read-only tool calls and emits final text, the director stores
that text on the handle and returns; the runtime validates and commits it as
opaque content. The model never sees a commit tool.

Two concrete director shapes ship here:

* :class:`ScriptedDirector` — a thin test double that replays a canned list of
  ``preview`` / ``tool`` / ``final`` / ``sleep`` steps; used by the contract
  tests. ``final`` steps set the harness-consumed narrative text; successive
  ``direct()`` calls advance through the script (bounded quality retries).
* :class:`ProviderDrivenDirector` — the standard provider + tool loop; it
  consumes provider :class:`ProviderDelta` streaming, dispatches read-only
  typed tool calls, retries retryable provider errors within a bounded policy,
  aborts on cancel, records per-call telemetry, and ends when the model
  returns final text with no further tool calls.
"""

import time

from airp.engine.provider import (
    AbortSignal,
    ProviderAborted,
    ProviderDelta,
    ProviderError,
    ProviderRequest,
    ProviderResult,
    UsageRecord,
    CostEstimate,
)
from airp.host.rp.tools import ToolResult


# ═══ Director handle ═══


class DirectorHandle:
    """Surface handed to a director ``direct()`` each run.

    The handle is the ONLY conduit from a director to the runtime during a
    generation: closed read-only tools, abort signal, preview emitter,
    telemetry, and the final text the host will validate and commit as opaque
    content.
    Directors never touch the database or the projection adapter directly, and
    they never invoke a commit tool.
    """

    def __init__(self, task_id, task_text, tools, signal, runtime):
        self.task_id = task_id
        self._task_text = task_text
        self._tools = tools
        self._signal: AbortSignal = signal
        self._runtime = runtime
        self._final_text: str | None = None
        self._commit_feedback: dict | None = None

    # --- tools ---

    def call_tool(self, name: str, args: dict) -> ToolResult:
        return self._tools.call(name, args)

    def tool_schemas(self) -> list:
        return self._tools.schemas()

    @property
    def task_text(self) -> str:
        return self._task_text

    # --- narrative output (harness commit source) ---

    def set_final_text(self, text: str) -> None:
        """Record the model/director's final narrative for harness commit."""
        self._final_text = text if text is not None else ""

    def take_final_text(self) -> str | None:
        """Return and clear the final narrative (None if the director set nothing)."""
        text = self._final_text
        self._final_text = None
        return text

    def set_commit_feedback(self, error: str, details=None) -> None:
        """Harness feeds the last commit rejection so a re-generation can correct it."""
        self._commit_feedback = {"error": error, "details": details}

    @property
    def commit_feedback(self) -> dict | None:
        return self._commit_feedback

    # --- preview + abort ---

    def emit_preview(self, text: str) -> None:
        if text:
            self._runtime._emit_preview(self.task_id, text)

    @property
    def aborted(self) -> bool:
        return self._signal.cancelled

    @property
    def signal(self) -> AbortSignal:
        return self._signal

    # --- context + telemetry ---

    def compile_follow_up(self):
        """Compile + persist a follow-up Context Manifest for the next model call.

        Used by :class:`ProviderDrivenDirector` between tool rounds so that each
        model call references its own manifest (spec Implementation Decision 13).
        """
        return self._runtime.compile_follow_up_manifest(self.task_id, self._task_text)

    def compile_sequential_handoff(self, compiled, source_node, target_node, text):
        """Persist a graph handoff manifest before its receiving node runs."""
        return self._runtime.compile_sequential_handoff_manifest(
            self.task_id, compiled, source_node, target_node, text
        )

    def report_model_call_started(self, meta: dict) -> None:
        self._runtime._emit_model_call_started(self.task_id, meta)

    def report_agent_node_started(self, node_id: str, role: str) -> None:
        self._runtime._emit_agent_node_event(self.task_id, "agent_node.started", node_id, role)

    def report_agent_node_finished(self, node_id: str, role: str) -> None:
        self._runtime._emit_agent_node_event(self.task_id, "agent_node.finished", node_id, role)

    def report_model_call_finished(self, meta: dict) -> None:
        self._runtime._record_model_call(self.task_id, meta)


# ═══ Director base ═══


class NarrativeDirector:
    """Base contract for a narrative-director execution layer.

    Subclasses implement :meth:`direct`. A director produces text via
    ``handle.set_final_text(...)``; the runtime host commits it without
    interpreting its format.
    Returning without final text is a quality/empty-content concern, not a
    separate "forgot to commit" flow failure (ADR-0011).
    """

    def direct(self, handle: DirectorHandle, compiled) -> None:
        raise NotImplementedError


# ═══ Scripted test director ═══


class ScriptedDirector(NarrativeDirector):
    """Replays a canned list of steps; used by the contract test suite.

    Step shapes::

        ("preview", "<text>")
        ("tool", "<tool_name>", {<args>})
        ("final", "<narrative text>")   # sets handle final text and returns
        ("sleep", <seconds>)
        ("wait_for_aborted",)           # block until aborted (cooperative)

    Successive :meth:`direct` calls continue from the next unconsumed step so
    the harness can re-enter after a quality/MVU rejection (bounded retries).
    """

    def __init__(self, script):
        self.script = list(script)
        self._cursor = 0
        self.observed_aborted = False
        self.results: list[ToolResult] = []
        self.last_result: ToolResult | None = None
        self.final_texts: list[str] = []

    def direct(self, handle, compiled):
        while self._cursor < len(self.script):
            if handle.aborted:
                self.observed_aborted = True
                return
            step = self.script[self._cursor]
            self._cursor += 1
            kind = step[0]
            if kind == "preview":
                handle.emit_preview(step[1])
            elif kind == "tool":
                result = handle.call_tool(step[1], step[2])
                self.results.append(result)
                self.last_result = result
            elif kind == "final":
                text = step[1] if len(step) > 1 else ""
                self.final_texts.append(text)
                handle.set_final_text(text)
                return
            elif kind == "sleep":
                deadline = time.monotonic() + step[1]
                while time.monotonic() < deadline:
                    if handle.aborted:
                        self.observed_aborted = True
                        return
                    handle.signal.wait(timeout=0.01)
            elif kind == "wait_for_aborted":
                handle.signal.wait(timeout=10)
                self.observed_aborted = handle.aborted
                return
            else:
                raise ValueError(f"unknown scripted step: {kind!r}")


# ═══ Provider-driven director ═══


def _tool_result_message(result: ToolResult) -> dict:
    return {"ok": bool(result.ok), "value": result.value, "error": result.error}


class ProviderDrivenDirector(NarrativeDirector):
    """Standard narrative-director loop: provider stream + read-only tool dispatch.

    The loop:

    1. builds a :class:`ProviderRequest` from the compiled messages + tool
       schemas (read-only only);
    2. consumes the provider stream, emitting preview deltas as they arrive;
    3. dispatches each provider tool_call through the (schema-validating)
       tool registry and feeds the result back into the message history;
    4. reports retryable :class:`ProviderError` to the harness; a new graph run
       is created only when the user explicitly requests a retry;
    5. terminates on abort (``ProviderAborted``), terminal provider error, or a
       final response with **no further tool calls** — at which point the
       accumulated assistant text is stored on the handle for harness commit.

    The director NEVER writes authoritative state. Commit is performed by the
    runtime after :meth:`direct` returns (ADR-0011).
    """

    def __init__(
        self,
        provider,
        max_tool_rounds: int = 8,
        max_retries: int = 0,
        *,
        role: str = "narrative_director",
        model: str | None = None,
        instruction: str = "",
        parameters: dict | None = None,
    ):
        # ``max_retries`` remains accepted for compatibility with embedders;
        # provider calls are intentionally single-attempt now.
        self._provider = provider
        self._max_tool_rounds = max_tool_rounds
        self._role = role
        self._model = model
        self._instruction = instruction
        self._parameters = dict(parameters or {})
        self.last_final_text: str | None = None

    def direct(self, handle: DirectorHandle, compiled) -> None:
        messages = list(compiled.payload)
        tools = handle.tool_schemas()
        instruction = self._instruction
        if instruction:
            messages.append({"role": "system", "content": instruction})
        model = (
            self._model
            or self._provider.model_id(self._role)
        )
        current_manifest = compiled
        rates = getattr(self._provider, "_rates", CostEstimate())
        accumulated_text = ""

        for round_index in range(self._max_tool_rounds):
            if handle.aborted:
                return
            if round_index > 0:
                current_manifest = handle.compile_follow_up()
            call_ordinal = current_manifest.manifest["call_ordinal"] + 1
            deltas, _usage, stop_reason = self._invoke_model(
                handle, messages, tools, model, current_manifest, call_ordinal, rates
            )
            assistant_text = "".join(d.text or "" for d in deltas if d.text)
            tool_calls = [d.tool_call for d in deltas if d.tool_call]
            if assistant_text:
                accumulated_text = assistant_text
            if assistant_text or tool_calls:
                # The assistant message MUST carry tool_calls back to the model;
                # otherwise a following role:"tool" message has no preceding
                # tool_calls to respond to and the provider rejects the request
                # (real DeepSeek enforces this; FakeProvider does not).
                assistant_msg = {"role": "assistant", "content": assistant_text}
                if tool_calls:
                    assistant_msg["tool_calls"] = [
                        {
                            "id": call.get("id") or f"call_{name}",
                            "name": (name := call.get("name")),
                            "args": call.get("args", {}),
                        }
                        for call in tool_calls
                    ]
                messages.append(assistant_msg)
            for call in tool_calls:
                if handle.aborted:
                    return
                name = call.get("name")
                call_id = call.get("id") or f"call_{name}"
                result = handle.call_tool(name, call.get("args", {}))
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": name,
                        "content": _tool_result_message(result),
                    }
                )
            if not tool_calls:
                # Final response: no more read-only tool work. Hand text to harness.
                final = accumulated_text or assistant_text or ""
                self.last_final_text = final
                handle.set_final_text(final)
                return

        # Bounded rounds exhausted while still requesting tools — commit whatever
        # narrative text we accumulated (may be empty → quality gate decides).
        self.last_final_text = accumulated_text
        handle.set_final_text(accumulated_text)

    # --- model invocation with retry + telemetry ---

    def _invoke_model(
        self, handle, messages, tools, model, manifest, call_ordinal, rates
    ):
        manifest_id = manifest.manifest.get("id")
        request = ProviderRequest(
            messages=messages,
            tools=tools,
            model=model,
            metadata={
                "task_id": handle.task_id,
                "call_ordinal": call_ordinal,
                "manifest_id": manifest_id,
            },
            parameters=dict(self._parameters),
        )
        handle.report_model_call_started(
            {
                "call_ordinal": call_ordinal,
                "manifest_id": manifest_id,
                "model": model,
                "messages": messages,
                "tools": tools,
            }
        )
        started = time.monotonic()
        deltas: list[ProviderDelta] = []
        usage = UsageRecord()
        stop_reason = "stop"
        # Provider failures are terminal for this node. The public graph retry
        # command replays the whole graph instead of retrying a hidden call.
        for _attempt in range(1):
            if handle.aborted:
                raise ProviderAborted("aborted before stream")
            deltas = []
            try:
                for item in self._provider.stream(request, handle.signal):
                    if isinstance(item, ProviderDelta):
                        deltas.append(item)
                        if item.text:
                            handle.emit_preview(item.text)
                    elif isinstance(item, ProviderResult):
                        usage = item.usage
                        stop_reason = item.stop_reason
                        rates = item.cost_estimate
                break
            except ProviderError:
                raise
        latency_ms = int((time.monotonic() - started) * 1000)
        handle.report_model_call_finished(
            {
                "call_ordinal": call_ordinal,
                "manifest_id": manifest_id,
                "model": model,
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "stop_reason": stop_reason,
                "output": "".join(d.text or "" for d in deltas if d.text),
                "latency_ms": latency_ms,
                "cost_amount": rates.amount,
                "cost_currency": rates.currency,
                "cost_rate_version": rates.rate_version,
            }
        )
        return deltas, usage, stop_reason
