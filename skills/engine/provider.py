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
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator


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

    def model_id(self, role: str) -> str:
        return self._model

    def stream(self, request, signal):
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

    Credentials: the sidecar reads ``DEEPSEEK_API_KEY`` from its own process
    env. Python passes ``os.environ`` through at spawn and NEVER puts the key
    into IPC request bodies, metadata, events, manifests, or logs.
    ``credentials`` is accepted for API symmetry with :class:`FakeProvider`
    but is **not** forwarded into the sidecar env or request payloads.
    """

    DEFAULT_MODEL = "deepseek-v4-flash"
    DEFAULT_BASE_URL = "https://api.deepseek.com"
    DEFAULT_PROVIDER = "deepseek"
    RATE_VERSION = "pi-catalog-0.82.1"

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
    ) -> None:
        self._sidecar_command = list(sidecar_command) if sidecar_command else None
        # Accepted for symmetry with FakeProvider; NEVER forwarded to the
        # sidecar, request bodies, or any durable surface.
        self._credentials = credentials or {}
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
                stderr_text = "".join(stderr_chunks).strip()
                raise ProviderError(
                    f"sidecar stdin write failed: {exc}"
                    + (f"; stderr={stderr_text[:500]}" if stderr_text else ""),
                    "terminal_internal",
                    False,
                ) from exc

            while True:
                if signal.cancelled and not abort_sent:
                    _send_abort()
                try:
                    kind, data = line_queue.get(timeout=0.1)
                except queue.Empty:
                    if proc.poll() is not None:
                        # Process exited; drain remaining lines briefly.
                        try:
                            kind, data = line_queue.get(timeout=0.2)
                        except queue.Empty:
                            stderr_text = "".join(stderr_chunks).strip()
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
                    stderr_text = "".join(stderr_chunks).strip()
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
                try:
                    proc.terminate()
                    try:
                        proc.wait(timeout=1.0)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=1.0)
                except OSError:
                    pass

    def _build_command(self) -> list[str]:
        if self._sidecar_command:
            return list(self._sidecar_command)
        script = _default_sidecar_script()
        cmd = [self._node_binary, str(script)]
        if self._mock:
            cmd.append("--mock")
        return cmd

    def _spawn_env(self) -> dict:
        # Pass through the process environment so DEEPSEEK_API_KEY reaches the
        # sidecar when present. Do NOT inject self._credentials — those exist
        # only for FakeProvider-style secret-isolation tests and must never
        # become real env values or IPC content.
        env = dict(os.environ)
        if self._mock:
            env["PI_SIDECAR_MOCK"] = "1"
        # Ensure node can resolve the root node_modules even if cwd drifts.
        node_path = env.get("NODE_PATH", "")
        root_modules = str(self._cwd / "node_modules")
        env["NODE_PATH"] = (
            root_modules if not node_path else f"{root_modules}{os.pathsep}{node_path}"
        )
        return env


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
