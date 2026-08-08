"""Pi Agent Core executor for ephemeral AIRP Graph Runs.

Pi owns a single Agent's in-memory tool loop. AIRP keeps ownership of provider
profiles, tool permissions, graph order, regex transformations, trace events,
and the final session commit. Communication is a small local JSONL protocol;
the profile secret exists only in the sidecar command for the active run.
"""

from __future__ import annotations

import json
import queue
import re
import subprocess
import threading
import uuid
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

from airp.engine.graph_runtime import AgentArtifact, GraphNodePlan, NodeExecutionContext, NodeResult
from airp.engine.macros import build_context, expand_template
from airp.engine.runner_support import notify, parameters_for, serialize_content, tools_for
from airp.engine.provider import CostEstimate, ProviderError, ProviderRequest, ProviderResult, UsageRecord
from airp.engine.regex_transformer import RegexTransformError, RegexTransformer


class PiSidecarProcessError(RuntimeError):
    pass


class PiCoreNodeRunner:
    """Run one node through a shared local Pi sidecar.

    A lock deliberately covers an entire node run, including its tool calls.
    It enforces AIRP's current invariant that only one Agent/model/tool action
    executes at a time. The sidecar reuses a transcript only for matching
    ``execution_id`` + Agent id and GraphRuntime closes that id in ``finally``.
    """

    def __init__(
        self,
        execution_config_factory: Callable[[GraphNodePlan], Mapping[str, Any]],
        *,
        tool_handler: Callable[[str, dict[str, Any]], Any] | None = None,
        regex_transformer: RegexTransformer | Callable[[GraphNodePlan], Any] | Iterable[Mapping[str, Any]] | None = None,
        node_binary: str = "node",
        sidecar_path: str | Path | None = None,
    ) -> None:
        if not callable(execution_config_factory):
            raise TypeError("PiCoreNodeRunner requires an execution config factory")
        self.execution_config_factory = execution_config_factory
        self.tool_handler = tool_handler
        self.regex_transformer = regex_transformer
        self.node_binary = node_binary
        self.sidecar_path = Path(sidecar_path) if sidecar_path is not None else Path(__file__).resolve().parents[1] / "resources" / "pi_agent_sidecar.bundle.mjs"
        self._lock = threading.RLock()
        self._process: subprocess.Popen[str] | None = None
        self._events: queue.Queue[dict[str, Any] | BaseException | None] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._stderr_tail: deque[str] = deque(maxlen=16_384)

    def run(
        self,
        node: GraphNodePlan,
        input_artifact: AgentArtifact,
        *,
        observer: Any = None,
        execution_context: NodeExecutionContext | None = None,
    ) -> NodeResult:
        active_request: ProviderRequest | None = None
        active_ordinal: int | None = None
        call_ordinal = 0
        terminal_provider_error: ProviderError | None = None
        config: dict[str, Any] = {}
        try:
            config = self._execution_config(node)
            regex_transformer = self._resolve_regex_transformer(node)
            input_content = self._transform_input(regex_transformer, node, input_artifact.content, observer)
            context_artifact = input_artifact
            if input_content != input_artifact.content and isinstance(input_content, str):
                context_artifact = AgentArtifact.text(
                    input_content,
                    kind=input_artifact.kind,
                    content_type=input_artifact.content_type,
                    metadata=input_artifact.metadata,
                )
            messages = self._messages(node, input_content, context_artifact, execution_context)
            system_prompt, seed_messages = self._pi_prompt(messages, config)
            execution_id = execution_context.execution_id if execution_context and execution_context.execution_id else str(uuid.uuid4())
            run_id = str(uuid.uuid4())
            command = {
                "type": "run",
                "run_id": run_id,
                "execution_id": execution_id,
                "agent_id": node.agent_id,
                "system_prompt": system_prompt,
                "seed_messages": seed_messages,
                "input": input_content,
                "model": config,
                "generation": self._parameters(node),
                "tools": self._tools(node, execution_context),
            }
            with self._lock:
                self._ensure_process()
                self._drain_events()
                self._send(command)
                while True:
                    event = self._next_event(execution_context)
                    if event.get("run_id") not in {None, run_id}:
                        continue
                    kind = event.get("type")
                    if kind == "model_call_started":
                        call_ordinal += 1
                        active_ordinal = call_ordinal
                        trace_messages = list(event.get("messages") or messages)
                        system_prompt = event.get("system_prompt")
                        if isinstance(system_prompt, str) and system_prompt:
                            trace_messages.insert(0, {"role": "system", "content": system_prompt})
                        active_request = ProviderRequest(
                            messages=trace_messages,
                            tools=self._tools(node, execution_context),
                            model=str(event.get("model") or config["model_id"]),
                            metadata={"node_id": node.node_id, "agent_id": node.agent_id, "executor": "pi_core"},
                            parameters=self._parameters(node),
                        )
                        self._notify(observer, "model_call_started", node, active_ordinal, active_request)
                    elif kind == "delta":
                        self._notify(observer, "node_delta", node, str(event.get("text") or ""))
                    elif kind == "model_call_finished":
                        if active_request is not None and active_ordinal is not None:
                            result = self._provider_result(event)
                            self._notify(
                                observer,
                                "model_call_finished",
                                node,
                                active_ordinal,
                                active_request,
                                str(event.get("text") or ""),
                                result,
                            )
                        active_request = None
                        active_ordinal = None
                    elif kind == "model_call_failed":
                        terminal_provider_error = self._provider_error(
                            str(event.get("error") or "Pi model call failed"), config
                        )
                        if active_request is not None and active_ordinal is not None:
                            self._notify(
                                observer,
                                "model_call_failed",
                                node,
                                active_ordinal,
                                active_request,
                                terminal_provider_error,
                            )
                        active_request = None
                        active_ordinal = None
                    elif kind == "tool_call":
                        call = event.get("tool_call") if isinstance(event.get("tool_call"), Mapping) else {}
                        value, error = self._execute_tool(node, call, observer, execution_context)
                        self._send(
                            {
                                "type": "tool_result",
                                "request_id": event.get("request_id"),
                                "value": value,
                                "error": error,
                            }
                        )
                    elif kind == "run_finished":
                        if not event.get("ok"):
                            if active_request is not None and active_ordinal is not None:
                                terminal_provider_error = self._provider_error(
                                    str(event.get("error") or "Pi Agent failed"), config
                                )
                                self._notify(observer, "model_call_failed", node, active_ordinal, active_request, terminal_provider_error)
                            code = "aborted" if event.get("aborted") else "pi_agent_failed"
                            if event.get("aborted"):
                                return NodeResult.failed({"code": code, "message": "aborted", "retryable": False})
                            provider_error = terminal_provider_error or self._provider_error(
                                str(event.get("error") or code), config
                            )
                            return NodeResult.failed(
                                {
                                    "code": provider_error.category,
                                    "message": str(provider_error),
                                    "retryable": bool(provider_error.retryable),
                                }
                            )
                        output = str(event.get("text") or "")
                        if regex_transformer is not None:
                            output_result = regex_transformer.transform(output, target="output")
                            self._notify(observer, "output_transformed", node, output, output_result)
                            output = output_result.text
                        return NodeResult.succeeded(AgentArtifact.text(output))
                    elif kind in {"protocol_error", "sidecar_error"}:
                        return NodeResult.failed({"code": "pi_sidecar_protocol", "message": self._redact_error(str(event.get("error") or kind), config)})
        except RegexTransformError as exc:
            self._notify(observer, "regex_transform_failed", node, exc)
            return NodeResult.failed(exc.to_dict())
        except Exception as exc:
            message = self._redact_error(str(exc), config)
            traced_error = PiSidecarProcessError(message) if isinstance(exc, PiSidecarProcessError) else exc
            if active_request is not None and active_ordinal is not None:
                self._notify(observer, "model_call_failed", node, active_ordinal, active_request, traced_error)
            code = "pi_sidecar_failed" if isinstance(exc, PiSidecarProcessError) else "pi_node_runner_failed"
            return NodeResult.failed({"code": code, "message": message})

    def close_execution(self, execution_id: str) -> None:
        """Discard every private Pi transcript belonging to one Graph Run.

        AIRP currently permits only one active Agent action, so there is no
        value in retaining an idle sidecar after a Graph Run. Terminating it
        also makes process exit an explicit part of the no-transcript-recovery
        contract and avoids stale workers when Studio configuration changes.
        """
        with self._lock:
            if self._process is None:
                return
            try:
                if self._process.poll() is None:
                    self._send({"type": "close", "execution_id": execution_id})
            except OSError:
                pass
            finally:
                self.shutdown()

    def shutdown(self) -> None:
        """Terminate the local worker when its hosting runtime shuts down."""
        with self._lock:
            process = self._process
            self._process = None
            if process is None:
                return
            try:
                if process.poll() is None:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=2)
            finally:
                for pipe in (process.stdin, process.stdout, process.stderr):
                    if pipe is not None:
                        try:
                            pipe.close()
                        except OSError:
                            pass
                current = threading.current_thread()
                for reader in (self._reader, self._stderr_reader):
                    if reader is not None and reader is not current:
                        reader.join(timeout=1)
                self._reader = None
                self._stderr_reader = None

    def _execution_config(self, node: GraphNodePlan) -> dict[str, Any]:
        raw = dict(self.execution_config_factory(node) or {})
        required = ("base_url", "api_key", "api_format", "model_id")
        if any(not isinstance(raw.get(key), str) or not raw[key] for key in required):
            raise ValueError("Pi Agent executor requires base_url, api_key, api_format, and model_id")
        if raw["api_format"] not in {"chat_completions", "responses"}:
            raise ValueError("Pi Agent executor only supports chat_completions or responses")
        return {key: raw[key] for key in required}

    def _messages(self, node, input_content, context_artifact, execution_context):
        runtime_context = build_context(
            execution_context.macro_context if execution_context is not None else None,
            {
                "handoff": input_content,
                "node_input": input_content,
                "input_artifact": context_artifact.to_dict(),
                "skills": execution_context.skill_catalog if execution_context is not None and execution_context.skill_catalog is not None else [],
            },
        )
        return expand_template([dict(message) for message in node.agent.prompt], runtime_context, preserve_unknown=True)

    @staticmethod
    def _pi_prompt(messages: list[Mapping[str, Any]], config: Mapping[str, Any]) -> tuple[str, list[dict[str, Any]]]:
        system_parts: list[str] = []
        seed: list[dict[str, Any]] = []
        for message in messages:
            role = str(message.get("role") or "system")
            content = serialize_content(message.get("content", ""))
            if role == "system":
                system_parts.append(content)
            elif role == "assistant":
                seed.append(
                    {
                        "role": "assistant",
                        "content": [{"type": "text", "text": content}],
                        "api": "openai-responses" if config["api_format"] == "responses" else "openai-completions",
                        "provider": "airp",
                        "model": config["model_id"],
                        "usage": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "totalTokens": 0, "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0}},
                        "stopReason": "stop",
                        "timestamp": 0,
                    }
                )
            else:
                seed.append({"role": "user", "content": [{"type": "text", "text": content}], "timestamp": 0})
        return "\n\n".join(part for part in system_parts if part), seed

    def _transform_input(self, transformer, node, raw, observer):
        text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False, sort_keys=True)
        if transformer is None:
            return text
        result = transformer.transform(text, target="input")
        self._notify(observer, "input_transformed", node, text, result)
        return result.text

    def _resolve_regex_transformer(self, node: GraphNodePlan) -> RegexTransformer | None:
        candidate: Any = self.regex_transformer
        if candidate is not None:
            if isinstance(candidate, RegexTransformer):
                return candidate
            if callable(candidate):
                candidate = candidate(node)
            if isinstance(candidate, RegexTransformer):
                return candidate
            if isinstance(candidate, Mapping):
                candidate = candidate.get("rules")
            if isinstance(candidate, (list, tuple)):
                return RegexTransformer(candidate)
            if candidate is None:
                return None
            raise TypeError("regex_transformer must be a RegexTransformer, rules, or factory")
        candidate = node.agent.effective_config.get("regex_collection", node.agent.effective_config.get("regex_rules"))
        if isinstance(candidate, Mapping):
            candidate = candidate.get("rules")
        return RegexTransformer(candidate) if isinstance(candidate, (list, tuple)) else None

    def _tools(self, node, execution_context):
        return tools_for(node, execution_context)

    def _parameters(self, node):
        return parameters_for(node)

    def _tool_handler_for(self, node, execution_context):
        if execution_context is not None and execution_context.tool_handler is not None:
            return execution_context.tool_handler
        registry = execution_context.tool_registry if execution_context is not None else None
        if registry is not None and callable(getattr(registry, "dispatch", None)):
            return lambda current_node, name, args: registry.dispatch(name, args, allowed=current_node.tool_allowlist)
        tool_handler = self.tool_handler
        if tool_handler is None:
            return None
        return lambda _node, name, args: tool_handler(name, args)

    def _execute_tool(self, node, call, observer, execution_context):
        name = str(call.get("name") or "")
        args = dict(call.get("args") or {}) if isinstance(call.get("args"), Mapping) else {}
        trace_call = {"id": call.get("id"), "name": name, "args": args}
        handler = self._tool_handler_for(node, execution_context)
        if handler is None:
            error = "tool_handler_unavailable"
            self._notify(observer, "tool_call_started", node, trace_call)
            self._notify(observer, "tool_call_finished", node, trace_call, None, RuntimeError(error))
            return None, error
        self._notify(observer, "tool_call_started", node, trace_call)
        try:
            value = handler(node, name, args)
        except Exception as exc:
            self._notify(observer, "tool_call_finished", node, trace_call, None, exc)
            return None, str(exc)
        self._notify(observer, "tool_call_finished", node, trace_call, value, None)
        return value, None

    @staticmethod
    def _provider_result(event: Mapping[str, Any]) -> ProviderResult:
        usage = event.get("usage")
        usage_data = usage if isinstance(usage, Mapping) else {}
        return ProviderResult(
            usage=UsageRecord(
                prompt_tokens=int(usage_data.get("prompt_tokens") or 0),
                completion_tokens=int(usage_data.get("completion_tokens") or 0),
                total_tokens=int(usage_data.get("total_tokens") or 0),
                stop_reason=str(event.get("stop_reason") or "stop"),
            ),
            stop_reason=str(event.get("stop_reason") or "stop"),
            cost_estimate=CostEstimate(rate_version="pi-ai-provider-reported"),
        )

    def _provider_error(self, message: str, config: Mapping[str, Any]) -> ProviderError:
        """Restore AIRP's stable provider failure categories from Pi errors."""
        clean = self._redact_error(message, config)
        status_match = re.search(r"(?:^|\s)([1-5]\d\d)(?=\s*:|\s|$)", clean)
        status = int(status_match.group(1)) if status_match else None
        retry_markers = ("rate", "timeout", "overload", "unavailable", "connection", "fetch failed", "econn", "network")
        retryable = bool(status in {408, 409, 425, 429} or (status is not None and status >= 500))
        retryable = retryable or any(marker in clean.casefold() for marker in retry_markers)
        if retryable:
            return ProviderError(clean, "provider_unavailable", True)
        if status is not None or any(marker in clean.casefold() for marker in ("api key", "authentication", "unauthorized", "forbidden")):
            return ProviderError(clean, "provider_rejected", False)
        return ProviderError(clean, "terminal_internal", False)

    @staticmethod
    def _notify(observer, method, *args):
        notify(observer, method, *args)

    def _ensure_process(self) -> None:
        if self._process is not None and self._process.poll() is None:
            return
        if not self.sidecar_path.is_file():
            raise RuntimeError(f"Pi sidecar is missing: {self.sidecar_path}")
        self._events = queue.Queue()
        self._stderr_tail.clear()
        self._process = subprocess.Popen(
            [self.node_binary, str(self.sidecar_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self._reader = threading.Thread(target=self._read_stdout, daemon=True)
        self._stderr_reader = threading.Thread(target=self._read_stderr, daemon=True)
        self._reader.start()
        self._stderr_reader.start()

    def _read_stdout(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            for line in process.stdout:
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError as exc:
                    self._events.put(exc)
                    continue
                self._events.put(payload if isinstance(payload, dict) else ValueError("invalid Pi sidecar event"))
        finally:
            self._events.put(None)

    def _read_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for chunk in iter(lambda: process.stderr.read(4096), ""):
            self._stderr_tail.extend(chunk)

    def _stderr_text(self) -> str:
        return "".join(self._stderr_tail).strip()

    def _send(self, message: Mapping[str, Any]) -> None:
        if self._process is None or self._process.stdin is None:
            raise RuntimeError("Pi sidecar is not running")
        self._process.stdin.write(json.dumps(dict(message), ensure_ascii=False) + "\n")
        self._process.stdin.flush()

    def _next_event(self, execution_context: NodeExecutionContext | None) -> dict[str, Any]:
        abort_sent = False
        while True:
            signal = execution_context.abort_signal if execution_context is not None else None
            if signal is not None and bool(getattr(signal, "cancelled", False)) and not abort_sent:
                execution_id = execution_context.execution_id if execution_context is not None else ""
                self._send({"type": "abort", "execution_id": execution_id or "", "agent_id": ""})
                abort_sent = True
            try:
                item = self._events.get(timeout=0.05)
            except queue.Empty:
                continue
            if item is None:
                if self._stderr_reader is not None:
                    self._stderr_reader.join(timeout=0.5)
                detail = self._stderr_text()
                message = "Pi sidecar exited before completing the Agent run"
                if detail:
                    message = f"{message}: {detail}"
                raise PiSidecarProcessError(message)
            if isinstance(item, BaseException):
                raise RuntimeError(f"Pi sidecar emitted invalid JSON: {item}") from item
            return item

    def _drain_events(self) -> None:
        while True:
            try:
                self._events.get_nowait()
            except queue.Empty:
                return

    @staticmethod
    def _redact_error(message: str, config: Mapping[str, Any]) -> str:
        secret = str(config.get("api_key") or "")
        return message.replace(secret, "[REDACTED]") if secret else message
