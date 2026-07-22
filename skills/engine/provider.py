"""engine.provider — AIRP-owned provider adapter seam for the narrative director.

This module is the boundary between AIRP's narrative-director loop and any
underlying model provider. Ticket 03 ships:

* a deterministic :class:`FakeProvider` used by the Session Turn Runtime
  Contract tests (scriptable: fixed text, tool-call sequences, retryable /
  terminal errors, abort, scripted usage);
* a clearly-marked :class:`RealProviderAdapter` that raises
  ``NotImplementedError`` — the landing point for the deferred Node/Pi sidecar
  + real DeepSeek route (see ADR-0004 and the Ticket 03 re-scoping note).

The seam is intentionally narrow: domain code and the director loop depend on
:class:`ProviderAdapter` only, never on Pi types or provider-specific error
strings. Provider credentials are process configuration held by the adapter
implementation; they NEVER travel through :class:`ProviderRequest`,
:class:`ProviderDelta`, :class:`ProviderResult`, :class:`UsageRecord` or any
event payload (see spec Implementation Decision 36).
"""

import hashlib
import threading
import time
from dataclasses import dataclass
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


class RealProviderAdapter(ProviderAdapter):
    """Deferred seam for the real ``@earendil-works/pi-agent-core`` + ``pi-ai`` bridge.

    Ticket 03 is re-scoped to Python + a fake provider. This class is the
    documented attachment point where the follow-up sub-ticket lands:

    * spawn a Node sidecar process speaking the Pi Agent Core protocol,
    * forward :class:`ProviderRequest` messages to a real DeepSeek route via
      ``pi-ai`` (the project's only selection-gate route),
    * stream :class:`ProviderDelta` / :class:`ProviderResult` back over a
      structured IPC channel,
    * translate provider aborts / errors / usage into AIRP's stable types
      (:class:`AbortSignal`, :class:`ProviderError`, :class:`UsageRecord`).

    Raising here keeps the contract honest: nothing in-process claims to be a
    real provider. Credentials are accepted only to model future wiring and are
    NEVER persisted or logged by this stub.
    """

    def __init__(self, sidecar_command=None, credentials=None) -> None:
        self._sidecar_command = sidecar_command
        self._credentials = credentials or {}

    def stream(self, request, signal):
        raise NotImplementedError(
            "RealProviderAdapter is a deferred seam (Ticket 03 re-scoping). "
            "The Node/Pi sidecar + real DeepSeek E2E path lands in a follow-up "
            "sub-ticket; use FakeProvider for contract tests."
        )

    def model_id(self, role):
        raise NotImplementedError(
            "RealProviderAdapter.model_id is deferred; use FakeProvider for now."
        )
