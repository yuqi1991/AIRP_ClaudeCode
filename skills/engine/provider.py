"""engine.provider — AIRP-owned provider adapter seam for the narrative director.

This module is the boundary between AIRP's narrative-director loop and any
underlying model provider. It ships:

* a deterministic :class:`FakeProvider` used by the Session Turn Runtime
  Contract tests (scriptable: fixed text, tool-call sequences, retryable /
  terminal errors, abort, scripted usage);
* a :class:`RealProviderAdapter` that spawns the Node Pi sidecar
  (``skills/sidecar/pi_provider_sidecar.mjs``) and streams real DeepSeek
  (``deepseek-v4-flash``) via ``@earendil-works/pi-ai`` over line-JSON stdio
  (ADR-0010). Mock mode covers the IPC contract without network/key.

The seam is intentionally narrow: domain code and the director loop depend on
:class:`ProviderAdapter` only, never on Pi types or provider-specific error
strings. Provider credentials are process configuration held by the adapter
implementation / sidecar env; they NEVER travel through
:class:`ProviderRequest`, :class:`ProviderDelta`, :class:`ProviderResult`,
:class:`UsageRecord` or any event payload (see spec Implementation Decision 36).
"""

import hashlib
import json
import os
import queue
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


_runtime_provider_overrides: dict[str, dict[str, str]] = {}
_runtime_provider_lock = threading.Lock()


def set_runtime_provider_override(provider: str, *, api_key: str | None = None) -> None:
    """Keep a UI-supplied provider key in process memory only."""
    with _runtime_provider_lock:
        if api_key:
            _runtime_provider_overrides[provider] = {"api_key": api_key}


def runtime_provider_api_key(provider: str) -> str | None:
    with _runtime_provider_lock:
        value = _runtime_provider_overrides.get(provider, {}).get("api_key")
    return value or None


# ═══ Abort signaling ═══


class AbortSignal:
    """Cooperative cancel flag shared between the runtime and a running director.

    ``stop(task_id)`` calls :meth:`cancel`; the director loop and the provider
    stream poll :attr:`cancelled` / block on :meth:`wait`. This is the
    ``AbortSignal`` analogue from spec Implementation Decision 25 — stop uses
    abort semantics, not steering.
    """

    def __init__(self) -> None:
        self._cancelled = False
        self._lock = threading.Lock()
        self._event = threading.Event()

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
        self._event.set()

    @property
    def cancelled(self) -> bool:
        with self._lock:
            return self._cancelled

    def wait(self, timeout: float | None = None) -> bool:
        """Block until cancelled or ``timeout`` elapses. Returns True if cancelled."""
        return self._event.wait(timeout)


class ProviderAborted(Exception):
    """Raised inside a provider stream when an abort is observed.

    Distinct from :class:`ProviderError`: abort is never retryable and never a
    provider failure — it is an operator/player-initiated cancel.


    """


# ═══ Provider error classification (spec Implementation Decision 35) ═══


class ProviderError(Exception):
    """Stable, classified provider failure.

    ``category`` is a stable string (``provider_unavailable`` /
    ``provider_rejected`` / ``terminal_internal``) and ``retryable`` is data,
    not inferred from the message text by callers.
    """

    def __init__(self, message: str, category: str, retryable: bool) -> None:
        super().__init__(message)
        self.message = message
        self.category = category
        self.retryable = retryable


# ═══ Request / delta / result types ═══


@dataclass(frozen=True)
class ProviderRequest:
    """A single model call. ``metadata`` carries correlation ids only — no secrets."""

    messages: list
    tools: list
    model: str
    metadata: dict


@dataclass(frozen=True)
class ProviderDelta:
    """One streamed chunk from a provider call.

    Exactly one of ``text`` / ``tool_call`` is set per delta. ``tool_call`` has
    the shape ``{"id": ..., "name": ..., "args": {...}}``.
    """

    text: str | None = None
    tool_call: dict | None = None


@dataclass(frozen=True)
class UsageRecord:
    """Provider-reported token usage for one model call."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    stop_reason: str = ""

    def as_dict(self) -> dict:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "stop_reason": self.stop_reason,
        }


@dataclass(frozen=True)
class CostEstimate:
    """Pi-catalog-rate cost estimate. Explicitly NOT provider billing.

    Carries ``rate_version`` so consumers never confuse it with a real invoice
    (spec Implementation Decision 32).
    """

    amount: float = 0.0
    currency: str = "USD"
    rate_version: str = "fake-rates-v0"

    def as_dict(self) -> dict:
        return {
            "amount": self.amount,
            "currency": self.currency,
            "rate_version": self.rate_version,
        }


@dataclass(frozen=True)
class ProviderResult:
    """Final result of a provider call, carrying usage + stop reason + cost."""

    usage: UsageRecord
    stop_reason: str = "stop"
    cost_estimate: CostEstimate = CostEstimate()


# ═══ Adapter interfaces ═══


class ProviderAdapter:
    """Interface every provider (fake or the future real Node/Pi bridge) satisfies."""

    def stream(
        self, request: ProviderRequest, signal: AbortSignal
    ) -> Iterator[ProviderDelta | ProviderResult]:
        raise NotImplementedError

    def model_id(self, role: str) -> str:
        raise NotImplementedError


class OpenAICompatibleProviderAdapter(ProviderAdapter):
    """Direct HTTP adapter for OpenAI Responses and Chat Completions APIs."""

    SUPPORTED_FORMATS = frozenset({"responses", "chat_completions"})

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        api_format: str,
        model: str | None = None,
        timeout: float = 120.0,
    ) -> None:
        if not isinstance(base_url, str) or not base_url.startswith(("http://", "https://")):
            raise ValueError("base_url must use http:// or https://")
        if api_format not in self.SUPPORTED_FORMATS:
            raise ValueError("api_format must be responses or chat_completions")
        if not isinstance(api_key, str) or not api_key:
            raise ValueError("api_key must be configured")
        if timeout <= 0:
            raise ValueError("timeout must be positive")
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._api_format = api_format
        self._model = model or ""
        self._timeout = timeout

    def model_id(self, role: str) -> str:
        return self._model

    def discover_models(self) -> list[str]:
        payload = self._json_request("GET", self._base_url + "/models")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise ProviderError("provider returned an invalid model catalog", "terminal_internal", False)
        return sorted(
            {
                item["id"]
                for item in data
                if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"]
            }
        )

    def test_connection(self) -> list[str]:
        return self.discover_models()

    def stream(self, request: ProviderRequest, signal: AbortSignal):
        if signal.cancelled:
            raise ProviderAborted("aborted before stream")
        endpoint = "responses" if self._api_format == "responses" else "chat/completions"
        body = self._request_body(request)
        response = self._open("POST", f"{self._base_url}/{endpoint}", body)
        usage = UsageRecord()
        stop_reason = "stop"
        tool_arguments: dict[str, dict[str, str]] = {}
        chat_tool_calls: dict[int, dict[str, str]] = {}
        try:
            for raw_line in response:
                if signal.cancelled:
                    raise ProviderAborted("aborted by signal")
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line or line.startswith(":") or not line.startswith("data:"):
                    continue
                raw_data = line[5:].strip()
                if raw_data == "[DONE]":
                    break
                try:
                    event = json.loads(raw_data)
                except json.JSONDecodeError as exc:
                    raise ProviderError(
                        "provider emitted invalid streaming JSON", "terminal_internal", False
                    ) from exc
                if isinstance(event, dict) and event.get("error"):
                    raise self._event_error(event["error"])
                if self._api_format == "responses":
                    deltas, event_usage, event_stop = self._map_responses_event(
                        event, tool_arguments
                    )
                else:
                    deltas, event_usage, event_stop = self._map_chat_event(
                        event, chat_tool_calls
                    )
                yield from deltas
                usage = event_usage or usage
                stop_reason = event_stop or stop_reason
        finally:
            response.close()
        yield ProviderResult(usage=usage, stop_reason=stop_reason)

    def _request_body(self, request: ProviderRequest) -> dict:
        common = {
            "model": request.model or self._model,
            "stream": True,
        }
        if self._api_format == "responses":
            common["input"] = self._responses_input(request.messages or [])
            if request.tools:
                common["tools"] = [self._responses_tool(tool) for tool in request.tools]
        else:
            common["messages"] = self._chat_messages(request.messages or [])
            common["stream_options"] = {"include_usage": True}
            if request.tools:
                common["tools"] = [self._chat_tool(tool) for tool in request.tools]
        return common

    @staticmethod
    def _chat_messages(messages: list) -> list:
        mapped = []
        for message in messages:
            item = dict(message)
            if item.get("role") == "assistant" and isinstance(item.get("tool_calls"), list):
                item["tool_calls"] = [
                    {
                        "id": call.get("id") or _new_call_id(),
                        "type": "function",
                        "function": {
                            "name": call.get("name") or "",
                            "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False),
                        },
                    }
                    for call in item["tool_calls"]
                ]
            if item.get("role") == "tool" and not isinstance(item.get("content"), str):
                item["content"] = json.dumps(item.get("content"), ensure_ascii=False)
            mapped.append(item)
        return mapped

    @staticmethod
    def _responses_input(messages: list) -> list:
        mapped = []
        for message in messages:
            role = message.get("role")
            if role == "tool":
                output = message.get("content", "")
                if not isinstance(output, str):
                    output = json.dumps(output, ensure_ascii=False)
                mapped.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.get("tool_call_id") or message.get("id") or "",
                        "output": output,
                    }
                )
                continue
            content = message.get("content", "")
            mapped.append({"role": role, "content": content})
            if role == "assistant":
                for call in message.get("tool_calls") or []:
                    mapped.append(
                        {
                            "type": "function_call",
                            "call_id": call.get("id") or _new_call_id(),
                            "name": call.get("name") or "",
                            "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False),
                        }
                    )
        return mapped

    @staticmethod
    def _chat_tool(tool: dict) -> dict:
        if tool.get("type") == "function" and isinstance(tool.get("function"), dict):
            return tool
        return {
            "type": "function",
            "function": {
                "name": tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
            },
        }

    @staticmethod
    def _responses_tool(tool: dict) -> dict:
        function = tool.get("function") if isinstance(tool.get("function"), dict) else tool
        return {
            "type": "function",
            "name": function.get("name", ""),
            "description": function.get("description", ""),
            "parameters": function.get("parameters") or {"type": "object", "properties": {}},
        }

    def _json_request(self, method: str, url: str) -> dict:
        response = self._open(method, url)
        try:
            return json.loads(response.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProviderError("provider returned invalid JSON", "terminal_internal", False) from exc
        finally:
            response.close()

    def _open(self, method: str, url: str, body: dict | None = None):
        encoded = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Accept": "application/json", "Authorization": f"Bearer {self._api_key}"}
        if encoded is not None:
            headers["Content-Type"] = "application/json"
            headers["Accept"] = "text/event-stream"
        try:
            return urlopen(Request(url, data=encoded, headers=headers, method=method), timeout=self._timeout)
        except HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:1000]
            finally:
                exc.close()
            retryable = exc.code in {408, 409, 425, 429} or 500 <= exc.code < 600
            category = "provider_unavailable" if retryable else "provider_rejected"
            raise ProviderError(
                self._redact(f"provider HTTP {exc.code}: {detail}"), category, retryable
            ) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise ProviderError(
                self._redact(f"provider connection failed: {exc}"),
                "provider_unavailable",
                True,
            ) from exc

    def _event_error(self, error) -> ProviderError:
        if isinstance(error, dict):
            message = error.get("message") or error.get("code") or "provider error"
            code = str(error.get("code") or "").lower()
        else:
            message, code = str(error), ""
        retryable = any(token in code for token in ("rate", "timeout", "overload", "unavailable"))
        return ProviderError(
            self._redact(str(message)),
            "provider_unavailable" if retryable else "provider_rejected",
            retryable,
        )

    @staticmethod
    def _map_chat_event(event: dict, tool_calls: dict[int, dict[str, str]]):
        deltas = []
        stop_reason = None
        for choice in event.get("choices") or []:
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if content:
                deltas.append(ProviderDelta(text=content))
            for call in delta.get("tool_calls") or []:
                function = call.get("function") or {}
                index = int(call.get("index") or 0)
                state = tool_calls.setdefault(index, {"id": "", "name": "", "args": ""})
                state["id"] = call.get("id") or state["id"]
                state["name"] += function.get("name") or ""
                state["args"] += function.get("arguments") or ""
            stop_reason = choice.get("finish_reason") or stop_reason
            if stop_reason and tool_calls:
                for index in sorted(tool_calls):
                    state = tool_calls[index]
                    try:
                        args = json.loads(state["args"] or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    deltas.append(
                        ProviderDelta(
                            tool_call={
                                "id": state["id"] or _new_call_id(),
                                "name": state["name"],
                                "args": args if isinstance(args, dict) else {},
                            }
                        )
                    )
                tool_calls.clear()
        usage_raw = event.get("usage") or {}
        usage = None
        if usage_raw:
            usage = UsageRecord(
                prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
                completion_tokens=int(usage_raw.get("completion_tokens") or 0),
                total_tokens=int(usage_raw.get("total_tokens") or 0),
                stop_reason=str(stop_reason or ""),
            )
        return deltas, usage, stop_reason

    def _map_responses_event(self, event: dict, tool_arguments: dict[str, dict[str, str]]):
        event_type = event.get("type")
        deltas = []
        usage = None
        stop_reason = None
        if event_type == "response.output_text.delta" and event.get("delta"):
            deltas.append(ProviderDelta(text=event["delta"]))
        elif event_type == "response.output_item.added":
            item = event.get("item") or {}
            if item.get("type") == "function_call":
                call_id = item.get("call_id") or item.get("id") or ""
                tool_arguments[call_id] = {
                    "name": item.get("name") or "",
                    "args": item.get("arguments") or "",
                }
        elif event_type == "response.function_call_arguments.delta":
            call_id = event.get("call_id") or event.get("item_id") or ""
            state = tool_arguments.setdefault(call_id, {"name": event.get("name") or "", "args": ""})
            state["args"] += event.get("delta") or ""
        elif event_type == "response.function_call_arguments.done":
            call_id = event.get("call_id") or event.get("item_id") or _new_call_id()
            state = tool_arguments.pop(call_id, {})
            raw_args = event.get("arguments") or state.get("args") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                args = {}
            deltas.append(
                ProviderDelta(
                    tool_call={
                        "id": call_id,
                        "name": event.get("name") or state.get("name") or "",
                        "args": args if isinstance(args, dict) else {},
                    }
                )
            )
        elif event_type == "response.completed":
            response = event.get("response") or {}
            raw = response.get("usage") or {}
            usage = UsageRecord(
                prompt_tokens=int(raw.get("input_tokens") or 0),
                completion_tokens=int(raw.get("output_tokens") or 0),
                total_tokens=int(raw.get("total_tokens") or 0),
                stop_reason=str(response.get("status") or "completed"),
            )
            stop_reason = response.get("status") or "completed"
        elif event_type in {"response.failed", "error"}:
            error = (event.get("response") or {}).get("error") or event.get("error") or event
            raise self._event_error(error)
        return deltas, usage, stop_reason

    def _redact(self, message: str) -> str:
        return message.replace(self._api_key, "[REDACTED]")


def _new_call_id() -> str:
    return "call_" + hashlib.sha256(time.monotonic_ns().to_bytes(8, "big")).hexdigest()[:12]


class FakeProvider(ProviderAdapter):
    """Deterministic, scriptable provider for the Session Turn Runtime Contract.

    ``scripts`` is either a single script (a list of step dicts) or a list of
    scripts; each :meth:`stream` call consumes the next script, so a retryable
    error followed by a successful call is expressed as two scripts.

    Step shapes::

        {"type": "text", "text": "..."}
        {"type": "tool_call", "id": "...", "name": "...", "args": {...}}
        {"type": "final", "stop_reason": "...", "usage": {...}}
        {"type": "error", "category": "...", "retryable": bool, "message": "..."}
        {"type": "block_until_aborted", "timeout": 5}

    ``credentials`` is accepted to model the real adapter's secret handling and
    is NEVER emitted into deltas, results, usage, or any structure returned by
    this provider (spec Implementation Decision 36).
    """

    def __init__(
        self,
        scripts,
        usage: dict | None = None,
        rates: CostEstimate | None = None,
        model: str = "fake-narrative-v0",
        credentials: dict | None = None,
    ) -> None:
        if not scripts:
            raise ValueError("FakeProvider requires at least one script")
        if isinstance(scripts[0], dict):
            self._scripts = [list(scripts)]
        else:
            self._scripts = [list(s) for s in scripts]
        self._call_index = 0
        default = {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
        self._default_usage = {**default, **(usage or {})}
        self._rates = rates or CostEstimate()
        self._model = model
        self._credentials = credentials or {}
        # number of times stream() was invoked — useful for assertions
        self.call_count = 0
        self.requests: list[ProviderRequest] = []

    def model_id(self, role: str) -> str:
        return self._model

    def stream(self, request, signal):
        self.requests.append(request)
        idx = min(self._call_index, len(self._scripts) - 1)
        script = self._scripts[idx]
        self._call_index += 1
        self.call_count += 1
        emitted_final = False
        for step in script:
            if signal.cancelled:
                raise ProviderAborted("aborted by signal")
            kind = step.get("type")
            if kind == "text":
                yield ProviderDelta(text=step["text"])
            elif kind == "tool_call":
                yield ProviderDelta(
                    tool_call={
                        "id": step.get("id") or _new_call_id(),
                        "name": step["name"],
                        "args": step.get("args", {}),
                    }
                )
            elif kind == "final":
                usage = UsageRecord(**{**self._default_usage, **step.get("usage", {})})
                yield ProviderResult(
                    usage=usage,
                    stop_reason=step.get("stop_reason", "stop"),
                    cost_estimate=self._rates,
                )
                emitted_final = True
                return
            elif kind == "error":
                raise ProviderError(
                    step.get("message", "provider error"),
                    step.get("category", "provider_rejected"),
                    step.get("retryable", False),
                )
            elif kind == "block_until_aborted":
                if signal.wait(timeout=step.get("timeout", 5)):
                    raise ProviderAborted("aborted during block")
            else:
                raise ValueError(f"unknown fake provider step: {kind!r}")
        if not emitted_final:
            yield ProviderResult(
                usage=UsageRecord(**self._default_usage),
                stop_reason="stop",
                cost_estimate=self._rates,
            )


def _default_sidecar_script() -> Path:
    """``skills/sidecar/pi_provider_sidecar.mjs`` relative to this module."""
    return Path(__file__).resolve().parents[1] / "sidecar" / "pi_provider_sidecar.mjs"


def _default_repo_root() -> Path:
    """Repo root (parent of ``skills/``) — where package.json / node_modules live."""
    return Path(__file__).resolve().parents[2]


class RealProviderAdapter(ProviderAdapter):
    """Node/Pi sidecar bridge for real DeepSeek (``deepseek-v4-flash``).

    Spawns ``skills/sidecar/pi_provider_sidecar.mjs`` and speaks line-delimited
    JSON over stdio (see ADR-0010). Pi types never leave the Node process;
    this adapter only yields AIRP :class:`ProviderDelta` /
    :class:`ProviderResult` and raises :class:`ProviderError` /
    :class:`ProviderAborted`.

    Credentials: the sidecar reads ``DEEPSEEK_API_KEY`` from a minimal,
    allowlisted child environment. Python NEVER puts the key into IPC request
    bodies, metadata, events, manifests, or logs. ``credentials`` is accepted
    for API symmetry with :class:`FakeProvider` but is **not** forwarded into
    the sidecar env or request payloads.
    """

    DEFAULT_MODEL = "deepseek-v4-flash"
    DEFAULT_BASE_URL = "https://api.deepseek.com"
    DEFAULT_PROVIDER = "deepseek"
    RATE_VERSION = "pi-catalog-0.82.1"
    DEFAULT_REQUEST_TIMEOUT = 120.0

    def __init__(
        self,
        sidecar_command=None,
        credentials=None,
        *,
        model: str | None = None,
        base_url: str | None = None,
        provider: str | None = None,
        mock: bool | None = None,
        node_binary: str | None = None,
        cwd: str | Path | None = None,
        rates: CostEstimate | None = None,
        request_timeout: float | None = None,
        runtime_api_key: str | None = None,
    ) -> None:
        self._sidecar_command = list(sidecar_command) if sidecar_command else None
        # Accepted for symmetry with FakeProvider; NEVER forwarded to the
        # sidecar, request bodies, or any durable surface.
        self._credentials = credentials or {}
        # Volatile UI secret: only copied into this call's sidecar environment.
        self._runtime_api_key = runtime_api_key or None
        self._model = model or self.DEFAULT_MODEL
        self._base_url = base_url or self.DEFAULT_BASE_URL
        self._provider = provider or self.DEFAULT_PROVIDER
        if mock is None:
            mock = os.environ.get("PI_SIDECAR_MOCK") == "1"
        self._mock = bool(mock)
        self._node_binary = node_binary or os.environ.get("AIRP_NODE_BINARY") or "node"
        self._cwd = Path(cwd) if cwd else _default_repo_root()
        self._rates = rates or CostEstimate(
            amount=0.0, currency="USD", rate_version=self.RATE_VERSION
        )
        self._request_timeout = (
            self.DEFAULT_REQUEST_TIMEOUT if request_timeout is None else request_timeout
        )
        if self._request_timeout <= 0:
            raise ValueError("request_timeout must be positive")

    def model_id(self, role: str) -> str:
        return self._model

    def stream(self, request: ProviderRequest, signal: AbortSignal):
        request_id = f"req_{uuid.uuid4().hex[:16]}"
        command = self._build_command()
        env = self._spawn_env()
        try:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=str(self._cwd),
                env=env,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise ProviderError(
                f"failed to spawn provider sidecar: {exc}",
                "terminal_internal",
                False,
            ) from exc

        line_queue: queue.Queue = queue.Queue()
        stderr_chunks: list[str] = []

        def _reader():
            try:
                assert proc.stdout is not None
                for line in proc.stdout:
                    line_queue.put(("line", line))
            except Exception as exc:  # noqa: BLE001 — surface via queue
                line_queue.put(("reader_error", str(exc)))
            finally:
                line_queue.put(("eof", None))

        def _stderr_reader():
            try:
                assert proc.stderr is not None
                for chunk in proc.stderr:
                    # Never log env; stderr is only attached to ProviderError
                    # messages for crash diagnosis.
                    stderr_chunks.append(chunk)
            except Exception:
                pass

        stdout_thread = threading.Thread(target=_reader, name="pi-sidecar-stdout", daemon=True)
        stderr_thread = threading.Thread(
            target=_stderr_reader, name="pi-sidecar-stderr", daemon=True
        )
        stdout_thread.start()
        stderr_thread.start()

        abort_sent = False

        def _send_abort():
            nonlocal abort_sent
            if abort_sent:
                return
            abort_sent = True
            try:
                if proc.stdin and proc.poll() is None:
                    proc.stdin.write(
                        json.dumps({"type": "abort", "request_id": request_id}) + "\n"
                    )
                    proc.stdin.flush()
            except (BrokenPipeError, OSError, ValueError):
                pass

        abort_watcher_stop = threading.Event()

        def _watch_abort():
            while not abort_watcher_stop.is_set():
                if signal.cancelled:
                    _send_abort()
                    return
                if signal.wait(timeout=0.05):
                    _send_abort()
                    return

        abort_thread = threading.Thread(
            target=_watch_abort, name="pi-sidecar-abort", daemon=True
        )
        abort_thread.start()

        try:
            if signal.cancelled:
                _send_abort()
                raise ProviderAborted("aborted before stream")

            payload = {
                "type": "stream",
                "request_id": request_id,
                "messages": list(request.messages or []),
                "tools": list(request.tools or []),
                "model": request.model or self._model,
                # Correlation only — never secrets. base_url is non-secret config.
                "metadata": {
                    **dict(request.metadata or {}),
                    "base_url": self._base_url,
                    "provider": self._provider,
                },
            }
            # Defence in depth: strip any accidental credential-shaped keys.
            payload["metadata"] = _strip_secret_keys(payload["metadata"])

            try:
                assert proc.stdin is not None
                proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as exc:
                stderr_text = _redact_sidecar_stderr("".join(stderr_chunks).strip())
                raise ProviderError(
                    f"sidecar stdin write failed: {exc}"
                    + (f"; stderr={stderr_text[:500]}" if stderr_text else ""),
                    "terminal_internal",
                    False,
                ) from exc

            deadline = time.monotonic() + self._request_timeout
            while True:
                if signal.cancelled and not abort_sent:
                    _send_abort()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _terminate_process(proc)
                    raise ProviderError(
                        "provider request timed out", "provider_unavailable", True
                    )
                try:
                    kind, data = line_queue.get(timeout=min(0.1, remaining))
                except queue.Empty:
                    if proc.poll() is not None:
                        # Process exited; drain remaining lines briefly.
                        try:
                            kind, data = line_queue.get(timeout=0.2)
                        except queue.Empty:
                            stderr_text = _redact_sidecar_stderr("".join(stderr_chunks).strip())
                            raise ProviderError(
                                "sidecar exited unexpectedly"
                                + (f"; stderr={stderr_text[:500]}" if stderr_text else ""),
                                "terminal_internal",
                                False,
                            )
                    else:
                        continue

                if kind == "eof":
                    if proc.poll() is None:
                        # stdout closed while process still alive — treat as crash.
                        try:
                            proc.kill()
                        except OSError:
                            pass
                    stderr_text = _redact_sidecar_stderr("".join(stderr_chunks).strip())
                    if signal.cancelled:
                        raise ProviderAborted("aborted (sidecar eof)")
                    raise ProviderError(
                        "sidecar stdout closed without terminal event"
                        + (f"; stderr={stderr_text[:500]}" if stderr_text else ""),
                        "terminal_internal",
                        False,
                    )
                if kind == "reader_error":
                    raise ProviderError(
                        f"sidecar stdout reader failed: {data}",
                        "terminal_internal",
                        False,
                    )

                line = (data or "").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ProviderError(
                        f"sidecar emitted non-json line: {line[:200]!r}",
                        "terminal_internal",
                        False,
                    ) from exc

                etype = event.get("type")
                if etype == "delta":
                    text = event.get("text")
                    if text:
                        yield ProviderDelta(text=text)
                elif etype == "tool_call":
                    yield ProviderDelta(
                        tool_call={
                            "id": event.get("id") or _new_call_id(),
                            "name": event.get("name") or "",
                            "args": event.get("args") or {},
                        }
                    )
                elif etype == "result":
                    usage_raw = event.get("usage") or {}
                    usage = UsageRecord(
                        prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
                        completion_tokens=int(usage_raw.get("completion_tokens") or 0),
                        total_tokens=int(usage_raw.get("total_tokens") or 0),
                        stop_reason=str(event.get("stop_reason") or "stop"),
                    )
                    cost_raw = event.get("cost_estimate") or {}
                    cost = CostEstimate(
                        amount=float(cost_raw.get("amount") or 0.0),
                        currency=str(cost_raw.get("currency") or "USD"),
                        rate_version=str(
                            cost_raw.get("rate_version") or self._rates.rate_version
                        ),
                    )
                    yield ProviderResult(
                        usage=usage,
                        stop_reason=str(event.get("stop_reason") or "stop"),
                        cost_estimate=cost,
                    )
                    return
                elif etype == "error":
                    category = str(event.get("category") or "terminal_internal")
                    # Normalize to the stable category set used by FakeProvider.
                    if category not in {
                        "provider_unavailable",
                        "provider_rejected",
                        "terminal_internal",
                    }:
                        category = "terminal_internal"
                    raise ProviderError(
                        str(event.get("message") or "provider error"),
                        category,
                        bool(event.get("retryable", False)),
                    )
                elif etype == "aborted":
                    raise ProviderAborted("aborted by signal")
                else:
                    raise ProviderError(
                        f"unknown sidecar event type: {etype!r}",
                        "terminal_internal",
                        False,
                    )
        finally:
            abort_watcher_stop.set()
            try:
                if proc.stdin and not proc.stdin.closed:
                    proc.stdin.close()
            except OSError:
                pass
            if proc.poll() is None:
                _terminate_process(proc)

    def _build_command(self) -> list[str]:
        if self._sidecar_command:
            return list(self._sidecar_command)
        script = _default_sidecar_script()
        cmd = [self._node_binary, str(script)]
        if self._mock:
            cmd.append("--mock")
        return cmd

    def _spawn_env(self) -> dict:
        # The child needs only its provider credential, Node resolution/config,
        # and platform loader settings. Deliberately do not inherit unrelated
        # parent-process secrets such as home-directory, CI, or service tokens.
        allowed = {
            "DEEPSEEK_API_KEY",
            "NODE_OPTIONS",
            "NODE_EXTRA_CA_CERTS",
            "NODE_NO_WARNINGS",
            "SSL_CERT_FILE",
            "SSL_CERT_DIR",
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
        }
        env = {key: os.environ[key] for key in allowed if key in os.environ}
        if self._runtime_api_key:
            env["DEEPSEEK_API_KEY"] = self._runtime_api_key
        if self._mock:
            env["PI_SIDECAR_MOCK"] = "1"
        # Ensure node can resolve the root node_modules even if cwd drifts.
        inherited_node_path = os.environ.get("NODE_PATH", "")
        root_modules = str(self._cwd / "node_modules")
        env["NODE_PATH"] = (
            root_modules
            if not inherited_node_path
            else f"{root_modules}{os.pathsep}{inherited_node_path}"
        )
        return env


def _terminate_process(proc: subprocess.Popen) -> None:
    """Stop a sidecar promptly after a deadline or stream failure."""
    if proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=1.0)
    except (OSError, subprocess.TimeoutExpired):
        try:
            proc.kill()
            proc.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            pass


_CREDENTIAL_VALUE_PATTERNS = (
    re.compile(r"(?i)(authorization\s*[:=]\s*bearer\s+)([^\s,;]+)"),
    re.compile(r"(?i)((?:api[_ -]?key|token|secret|password)\s*[:=]\s*)([^\s,;]+)"),
)


def _redact_sidecar_stderr(stderr: str) -> str:
    """Remove likely credential values before attaching sidecar stderr to errors."""
    for pattern in _CREDENTIAL_VALUE_PATTERNS:
        stderr = pattern.sub(r"\1[REDACTED]", stderr)
    return stderr


_SECRET_KEY_NAMES = {
    "api_key",
    "apikey",
    "secret",
    "token",
    "password",
    "authorization",
    "credential",
    "credentials",
    "deepseek_api_key",
}


def _strip_secret_keys(value):
    """Recursively drop credential-shaped keys from metadata before IPC."""
    if isinstance(value, dict):
        return {
            k: _strip_secret_keys(v)
            for k, v in value.items()
            if not (isinstance(k, str) and k.lower() in _SECRET_KEY_NAMES)
        }
    if isinstance(value, list):
        return [_strip_secret_keys(v) for v in value]
    return value
