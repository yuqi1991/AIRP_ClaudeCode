"""engine.director — narrative-director execution layer.

The narrative director is AIRP's single writing-role Agent for the tracer
bullet (spec Implementation Decision 1, 33). It is explicitly NOT a story
character or NPC. A run receives:

* a :class:`DirectorHandle` exposing the closed typed-tool surface, an
  :class:`~engine.provider.AbortSignal`, a preview emitter and a small set of
  telemetry hooks;
* a :class:`~engine.context_compiler.CompiledContext` produced by the AIRP-owned
  Context Compiler just before the run.

The director drives the model via a :class:`~engine.provider.ProviderAdapter`
and produces at most one structured turn by calling the ``commit_turn_draft``
tool. Streaming text emitted before commit is preview only — only a committed
draft becomes the player's authoritative turn (spec Implementation Decision
20–21, Testing Decision 6).

Two concrete director shapes ship here:

* :class:`ScriptedDirector` — a thin test double that replays a canned list of
  ``preview`` / ``tool`` / ``sleep`` steps; used by the Slice 1–3 contract
  tests;
* :class:`ProviderDrivenDirector` — the standard provider + tool loop used by
  the Slice 4 contract tests; it consumes provider :class:`ProviderDelta`
  streaming, dispatches typed tool calls, retries retryable provider errors
  within a bounded policy, aborts on cancel, and records per-call telemetry.
"""

import time

from engine.provider import (
    AbortSignal,
    ProviderAborted,
    ProviderDelta,
    ProviderError,
    ProviderRequest,
    ProviderResult,
    UsageRecord,
    CostEstimate,
)
from engine.tools import ToolResult


# ═══ Director handle ═══


class DirectorHandle:
    """Surface handed to a director ``direct()`` each run.

    The handle is the ONLY conduit from a director to authoritative state: it
    exposes the closed tool registry, the abort signal, a preview emitter and
    telemetry hooks. Directors never touch the runtime, the database, or the
    projection adapter directly.
    """

    def __init__(self, task_id, task_text, tools, signal, runtime):
        self.task_id = task_id
        self._task_text = task_text
        self._tools = tools
        self._signal: AbortSignal = signal
        self._runtime = runtime

    # --- tools ---

    def call_tool(self, name: str, args: dict) -> ToolResult:
        return self._tools.call(name, args)

    def tool_schemas(self) -> list:
        return self._tools.schemas()

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

    def report_model_call_started(self, meta: dict) -> None:
        self._runtime._emit_model_call_started(self.task_id, meta)

    def report_model_call_finished(self, meta: dict) -> None:
        self._runtime._record_model_call(self.task_id, meta)


# ═══ Director base ═══


class NarrativeDirector:
    """Base contract for a narrative-director execution layer.

    Subclasses implement :meth:`direct`. A director MUST call
    ``handle.call_tool("commit_turn_draft", ...)`` to produce a turn; if it
    returns without committing, the runtime transitions the task to a terminal
    non-committed state (see ``_run_director``).
    """

    def direct(self, handle: DirectorHandle, compiled) -> None:
        raise NotImplementedError


# ═══ Scripted test director (Slices 1–3) ═══


class ScriptedDirector(NarrativeDirector):
    """Replays a canned list of steps; used by the contract test suite.

    Step shapes::

        ("preview", "<text>")
        ("tool", "<tool_name>", {<args>})
        ("sleep", <seconds>)
        ("wait_for_aborted",)        # block until aborted (cooperative)
    """

    def __init__(self, script):
        self.script = list(script)
        self.observed_aborted = False
        self.results: list[ToolResult] = []
        self.last_result: ToolResult | None = None

    def direct(self, handle, compiled):
        for step in self.script:
            if handle.aborted:
                self.observed_aborted = True
                return
            kind = step[0]
            if kind == "preview":
                handle.emit_preview(step[1])
            elif kind == "tool":
                result = handle.call_tool(step[1], step[2])
                self.results.append(result)
                self.last_result = result
            elif kind == "sleep":
                # poll abort at a fine grain so stop() is observed promptly
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


# ═══ Provider-driven director (Slice 4) ═══


def _tool_result_message(result: ToolResult) -> dict:
    return {"ok": bool(result.ok), "value": result.value, "error": result.error}


class ProviderDrivenDirector(NarrativeDirector):
    """Standard narrative-director loop: provider stream + typed tool dispatch.

    The loop:

    1. builds a :class:`ProviderRequest` from the compiled messages + tool
       schemas;
    2. consumes the provider stream, emitting preview deltas as they arrive;
    3. dispatches each provider tool_call through the (schema-validating)
       tool registry and feeds the result back into the message history;
    4. retries retryable :class:`ProviderError` up to ``max_retries`` times;
    5. terminates on abort (``ProviderAborted``), terminal provider error, a
       committed draft, or a final response with no further tool calls.

    The director NEVER writes authoritative state directly — only
    ``commit_turn_draft`` can, and only via the tool surface.
    """

    def __init__(self, provider, max_tool_rounds: int = 8, max_retries: int = 3):
        self._provider = provider
        self._max_tool_rounds = max_tool_rounds
        self._max_retries = max_retries

    def direct(self, handle: DirectorHandle, compiled) -> None:
        messages = list(compiled.payload)
        tools = handle.tool_schemas()
        model = self._provider.model_id("narrative_director")
        current_manifest = compiled
        rates = getattr(self._provider, "_rates", CostEstimate())

        for round_index in range(self._max_tool_rounds):
            if handle.aborted:
                return
            if round_index > 0:
                current_manifest = handle.compile_follow_up()
            call_ordinal = round_index + 1
            deltas, _usage, stop_reason = self._invoke_model(
                handle, messages, tools, model, current_manifest, call_ordinal, rates
            )
            assistant_text = "".join(d.text or "" for d in deltas if d.text)
            tool_calls = [d.tool_call for d in deltas if d.tool_call]
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
            committed = False
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
                if name == "commit_turn_draft" and result.ok:
                    committed = True
            if committed:
                return
            if not tool_calls:
                # Provider returned a final response without requesting commit.
                # The task will reach a terminal non-committed state.
                return

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
        )
        handle.report_model_call_started(
            {
                "call_ordinal": call_ordinal,
                "manifest_id": manifest_id,
                "model": model,
            }
        )
        started = time.monotonic()
        deltas: list[ProviderDelta] = []
        usage = UsageRecord()
        stop_reason = "stop"
        for attempt in range(self._max_retries + 1):
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
            except ProviderError as exc:
                if not exc.retryable or attempt >= self._max_retries:
                    raise
                continue
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
                "latency_ms": latency_ms,
                "cost_amount": rates.amount,
                "cost_currency": rates.currency,
                "cost_rate_version": rates.rate_version,
            }
        )
        return deltas, usage, stop_reason
