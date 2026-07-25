"""Session command API — sole external entry into the new Pi-runtime path.

This module is the library seam for Ticket 04 (Session Event Stream) and
Ticket 06 (revision branch / reroll / rollback). HTTP and other transports wrap
:class:`SessionCommandService`; they must not call
:class:`~engine.runtime.SessionTurnRuntime` write paths directly.

Design notes (spec Decisions 8–11, 25–28, 34, 35):

* One serialized command stream per Session — ordering is provided by the
  runtime lock / ``BEGIN IMMEDIATE``, not a second lock hierarchy here.
* Every mutating command carries an idempotency key. Duplicate ``submit`` with
  the same key returns the same task/commit identity (runtime-guaranteed;
  re-asserted at this layer).
* ``reroll`` / ``rollback`` implement branch lineage: globally unique revisions,
  ``parent_revision`` edges, single active head. Superseded commits stay
  auditable; active context walks the parent chain only.
* Error categories are stable strings on :class:`CommandResult`; retryability is
  data, never inferred from free text by callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from engine.runtime import RuntimeEvent, RuntimeResult, SessionTurnRuntime

if TYPE_CHECKING:
    pass


# Stable error categories (spec Decision 35 + Ticket 04/06 minimum set).
ERROR_INVALID_COMMAND = "invalid_command"
ERROR_UNKNOWN_TASK = "unknown_task"
ERROR_UNKNOWN_REVISION = "unknown_revision"
ERROR_CANNOT_REROLL_OPENING = "cannot_reroll_opening"
ERROR_NOT_IMPLEMENTED = "not_implemented"  # retained for genuinely-N/A cases
ERROR_DEFERRED_TICKET_06 = "deferred_to_ticket_06"  # historical; reroll/rollback are live
ERROR_CANCELLED = "cancelled"
ERROR_STALE_REVISION = "stale_revision"
ERROR_TERMINAL_INTERNAL = "terminal_internal_failure"

# Task statuses treated as already finished for cancel no-ops.
_TERMINAL_TASK_STATUSES = frozenset(
    {
        "succeeded",
        "cancelled",
        "failed_terminal",
        "failed_retryable",
        "stale_revision",
        "quality_exhausted",
        "quality_gate_failed",
        "mvu_validation_failed",
        "rolled_back",
        "cannot_reroll_opening",
        "unknown_revision",
    }
)

_SUCCESS_TASK_STATUSES = frozenset({"succeeded", "projection_pending", "rolled_back"})


@dataclass(frozen=True)
class CommandResult:
    """Outcome of a session command. Plain frozen dataclass — no HTTP types."""

    ok: bool
    task_id: str | None = None
    commit_id: str | None = None
    revision: int | None = None
    status: str | None = None
    error: str | None = None
    retryable: bool = False
    message: str | None = None

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "task_id": self.task_id,
            "commit_id": self.commit_id,
            "revision": self.revision,
            "status": self.status,
            "error": self.error,
            "retryable": self.retryable,
            "message": self.message,
        }


@dataclass(frozen=True)
class SessionSnapshot:
    """Recoverable session view for refresh / reconnect (spec Decision 10)."""

    session_id: str
    active_revision: int
    last_event_sequence: int
    current_task: RuntimeResult | None = None

    def to_dict(self) -> dict:
        task = None
        if self.current_task is not None:
            task = {
                "task_id": self.current_task.task_id,
                "commit_id": self.current_task.commit_id,
                "revision": self.current_task.revision,
                "status": self.current_task.status,
            }
        return {
            "session_id": self.session_id,
            "active_revision": self.active_revision,
            "last_event_sequence": self.last_event_sequence,
            "current_task": task,
        }


class SessionCommandService:
    """Only external write entry for the new runtime path.

    Thin orchestration over :class:`SessionTurnRuntime`. Does not invent a second
    lock hierarchy.
    """

    def __init__(self, runtime: SessionTurnRuntime):
        self.runtime = runtime

    # ── mutating commands ──────────────────────────────────────────────

    def submit(self, text: str, idempotency_key: str) -> CommandResult:
        """Submit a player message. Delegates to ``runtime.submit``.

        Duplicate ``idempotency_key`` returns the same task/commit identity and
        does not start a second director run.
        """
        if not isinstance(text, str) or not text.strip():
            return CommandResult(
                ok=False,
                error=ERROR_INVALID_COMMAND,
                retryable=False,
                message="empty input",
            )
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            return CommandResult(
                ok=False,
                error=ERROR_INVALID_COMMAND,
                retryable=False,
                message="missing idempotency_key",
            )
        try:
            result = self.runtime.submit(text, idempotency_key)
        except ValueError as exc:
            return CommandResult(
                ok=False,
                error=ERROR_INVALID_COMMAND,
                retryable=False,
                message=str(exc),
            )
        except RuntimeError as exc:
            return self._from_runtime_error(exc, idempotency_key)
        return self._from_runtime_result(result)

    def cancel(
        self,
        task_id: str | None = None,
        idempotency_key: str | None = None,
        cancel_idempotency_key: str | None = None,
    ) -> CommandResult:
        """Cancel a queued or running task; propagates AbortSignal via ``runtime.stop``.

        ``cancel_idempotency_key`` is accepted for API symmetry (command stream
        ordering) but cancel itself is idempotent on task identity — repeated
        cancel of a terminal task is a no-op success.
        """
        del cancel_idempotency_key  # reserved; cancel is keyed by task identity
        resolved_id = task_id
        if not resolved_id and idempotency_key:
            resolved_id = self.runtime.task_id_for_key(idempotency_key)
        if not resolved_id:
            return CommandResult(
                ok=False,
                error=ERROR_UNKNOWN_TASK,
                retryable=False,
                message="task_id or known idempotency_key required",
            )
        task = self.runtime.task(resolved_id)
        if task is None:
            return CommandResult(
                ok=False,
                error=ERROR_UNKNOWN_TASK,
                retryable=False,
                message=f"unknown task: {resolved_id}",
            )
        if task.status in _TERMINAL_TASK_STATUSES:
            return CommandResult(
                ok=True,
                task_id=task.task_id,
                commit_id=task.commit_id,
                revision=task.revision,
                status=task.status,
                message="already_terminal",
            )
        # Propagate abort / mark queued cancelled.
        self.runtime.stop(resolved_id)
        # Re-read: stop on a running director is cooperative; status may still be
        # non-terminal until the submit thread observes the signal.
        task_after = self.runtime.task(resolved_id) or task
        return CommandResult(
            ok=True,
            task_id=task_after.task_id,
            commit_id=task_after.commit_id,
            revision=task_after.revision,
            status=task_after.status,
            message="cancel_requested",
        )

    def reroll(
        self,
        revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> CommandResult:
        """Reroll the assistant turn at ``revision`` with a fresh task/idempotency key.

        Reuses the original player input and the parent of the target revision.
        On success the new commit becomes the active head; the old branch stays
        auditable off the active lineage.
        """
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            return CommandResult(
                ok=False,
                error=ERROR_INVALID_COMMAND,
                retryable=False,
                message="missing idempotency_key",
            )
        if revision is None or not isinstance(revision, int):
            return CommandResult(
                ok=False,
                error=ERROR_INVALID_COMMAND,
                retryable=False,
                message="revision required",
            )
        try:
            result = self.runtime.reroll(revision, idempotency_key)
        except ValueError as exc:
            return CommandResult(
                ok=False,
                error=ERROR_INVALID_COMMAND,
                retryable=False,
                message=str(exc),
            )
        except RuntimeError as exc:
            return self._from_runtime_error(exc, idempotency_key)
        return self._from_runtime_result(result)

    def rollback(
        self,
        revision: int | None = None,
        idempotency_key: str | None = None,
    ) -> CommandResult:
        """Move the active head to ``revision`` without deleting history."""
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            return CommandResult(
                ok=False,
                error=ERROR_INVALID_COMMAND,
                retryable=False,
                message="missing idempotency_key",
            )
        if revision is None or not isinstance(revision, int):
            return CommandResult(
                ok=False,
                error=ERROR_INVALID_COMMAND,
                retryable=False,
                message="revision required",
            )
        try:
            result = self.runtime.rollback(revision, idempotency_key)
        except ValueError as exc:
            return CommandResult(
                ok=False,
                error=ERROR_INVALID_COMMAND,
                retryable=False,
                message=str(exc),
            )
        except RuntimeError as exc:
            return self._from_runtime_error(exc, idempotency_key)
        return self._from_runtime_result(result)

    # ── reads ──────────────────────────────────────────────────────────

    def snapshot(self) -> SessionSnapshot:
        """Active revision, latest task (if any), and last durable event sequence."""
        active = self.runtime.active_revision()
        events = self.runtime.events_after(0)
        last_seq = events[-1].sequence if events else 0
        current = self._latest_task_from_events(events)
        return SessionSnapshot(
            session_id=self.runtime.session_id,
            active_revision=active,
            last_event_sequence=last_seq,
            current_task=current,
        )

    def events_after(self, sequence: int) -> list[RuntimeEvent]:
        """Thin wrap of ``runtime.events_after`` — durable monotonic stream."""
        return self.runtime.events_after(sequence)

    # ── helpers ────────────────────────────────────────────────────────

    def _latest_task_from_events(self, events: list[RuntimeEvent]) -> RuntimeResult | None:
        task_id = None
        for event in reversed(events):
            if event.type in ("player_message.submitted", "task.reroll_requested"):
                task_id = event.payload.get("task_id")
                break
        if not task_id:
            return None
        return self.runtime.task(task_id)

    def _from_runtime_result(self, result: RuntimeResult) -> CommandResult:
        if result.status in _SUCCESS_TASK_STATUSES:
            return CommandResult(
                ok=True,
                task_id=result.task_id or None,
                commit_id=result.commit_id,
                revision=result.revision,
                status=result.status,
            )
        error = result.status or ERROR_TERMINAL_INTERNAL
        if result.status == "cancelled":
            error = ERROR_CANCELLED
        elif result.status == "cannot_reroll_opening":
            error = ERROR_CANNOT_REROLL_OPENING
        elif result.status == "unknown_revision":
            error = ERROR_UNKNOWN_REVISION
        elif result.status == "stale_revision":
            error = ERROR_STALE_REVISION
        return CommandResult(
            ok=False,
            task_id=result.task_id or None,
            commit_id=result.commit_id,
            revision=result.revision,
            status=result.status,
            error=error,
            retryable=result.status == "failed_retryable",
        )

    def _from_runtime_error(self, exc: RuntimeError, idempotency_key: str) -> CommandResult:
        message = str(exc)
        task_id = self.runtime.task_id_for_key(idempotency_key)
        task = self.runtime.task(task_id) if task_id else None
        if "stale revision" in message.lower() or (task and task.status == "stale_revision"):
            return CommandResult(
                ok=False,
                task_id=task_id,
                commit_id=task.commit_id if task else None,
                revision=task.revision if task else self.runtime.active_revision(),
                status=task.status if task else None,
                error=ERROR_STALE_REVISION,
                retryable=False,
                message=message,
            )
        return CommandResult(
            ok=False,
            task_id=task_id,
            commit_id=task.commit_id if task else None,
            revision=task.revision if task else self.runtime.active_revision(),
            status=task.status if task else None,
            error=ERROR_TERMINAL_INTERNAL,
            retryable=False,
            message=message,
        )
