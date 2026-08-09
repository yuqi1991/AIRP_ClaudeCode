"""Graph Run trace persistence and credential-safe debug serialization.

This module depends on the runtime's small persistence seam instead of owning
session, task, or projection state.
"""

from __future__ import annotations

import copy
import json
import re
import time
from typing import TYPE_CHECKING, Any

from airp.engine.graph_runtime import ExecutionPlan

if TYPE_CHECKING:
    from airp.host.rp.session_runtime import SessionTurnRuntime
_TRACE_SECRET_RE = re.compile(r"(?i)(bearer\s+|(?:api[_ -]?key|token|secret|password)\s*[:=]\s*)([^\s,;]+)")
_TRACE_SECRET_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "credentials",
        "password",
        "secret",
        "secret_ref",
        "token",
    }
)


def _redact_trace(value: Any) -> Any:
    """Redact provider credentials from persisted debug payloads."""
    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if str(key).casefold() in _TRACE_SECRET_KEYS else _redact_trace(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_trace(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_trace(item) for item in value]
    if isinstance(value, str):
        return _TRACE_SECRET_RE.sub(lambda match: f"{match.group(1)}[REDACTED]", value)
    return copy.deepcopy(value)


def _trace_json(value: Any) -> str:
    return json.dumps(_redact_trace(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load_trace_json(value: str | None, fallback: Any) -> Any:
    if not value:
        return copy.deepcopy(fallback)
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return copy.deepcopy(fallback)


class GraphRunObserver:
    """Persist one Graph Run while keeping Graph Runtime provider-agnostic."""

    def __init__(self, runtime: "SessionTurnRuntime", task, plan: ExecutionPlan, *, retry_of: str | None = None) -> None:
        self.runtime = runtime
        self.task = task
        self.plan = plan
        self.task_id = task["id"]
        self.retry_of = retry_of
        self.graph_run_id: str | None = None
        self.node_run_ids: dict[str, str] = {}
        # Provider calls are observed synchronously by NodeRunner, but their
        # latency must be measured at this boundary so the durable model_calls
        # table and the node debug payload cannot drift apart.
        self._model_call_started_at: dict[tuple[str, int], float] = {}
        self._aggregate_call_ordinals: dict[tuple[str, int], int] = {}

    def graph_started(self, plan: ExecutionPlan) -> None:
        self._ensure_started(plan)

    def node_started(self, node, input_artifact) -> None:
        self._ensure_started()
        node_run_id = self.node_run_ids.get(node.node_id)
        if node_run_id is None:
            return
        now = int(time.time())
        with self.runtime._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._can_persist(connection):
                return
            connection.execute(
                "UPDATE node_runs SET state = ?, input_artifact_json = ?, started_at = COALESCE(started_at, ?) "
                "WHERE id = ? AND session_id = ?",
                ("running", _trace_json(input_artifact.to_dict()), now, node_run_id, self.runtime.session_id),
            )
            payload = self._node_payload(node, node_run_id, state="running")
            self.runtime._event(connection, "graph.node.started", payload)
            # Existing game-side trace clients consume these aliases.
            self.runtime._event(
                connection,
                "agent_node.started",
                {"task_id": self.task_id, "node_id": node.node_id, "role": node.agent.name, "node_run_id": node_run_id},
            )

    def node_delta(self, node, delta: str) -> None:
        self._ensure_started()
        node_run_id = self.node_run_ids.get(node.node_id)
        if node_run_id is None or not delta:
            return
        with self.runtime._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._can_persist(connection):
                return
            row = connection.execute(
                "SELECT state, streamed_output FROM node_runs WHERE id = ? AND session_id = ?",
                (node_run_id, self.runtime.session_id),
            ).fetchone()
            if row is None or row["state"] != "running":
                return
            streamed = row["streamed_output"] or ""
            streamed += delta
            connection.execute(
                "UPDATE node_runs SET streamed_output = ? WHERE id = ? AND session_id = ?",
                (streamed, node_run_id, self.runtime.session_id),
            )
            payload = self._node_payload(node, node_run_id, state="running")
            payload.update({"delta": delta, "text": delta, "streamed_output": streamed})
            self.runtime._event(connection, "graph.node.delta", payload)
            source_node_id = getattr(node, "source_node_id", node.node_id)
            if source_node_id == self.plan.graph.output_node_id:
                self.runtime._event(
                    connection,
                    "narrative.preview.delta",
                    {"task_id": self.task_id, "preview": delta},
                )

    def input_transformed(self, node, raw: str, result) -> None:
        self._record_regex_transform(node, "input", raw, result)

    def output_transformed(self, node, raw: str, result) -> None:
        self._record_regex_transform(node, "output", raw, result)

    def _record_regex_transform(self, node, target: str, raw: str, result) -> None:
        self._ensure_started()
        node_run_id = self.node_run_ids.get(node.node_id)
        if node_run_id is None:
            return
        if hasattr(result, "to_dict"):
            detail = result.to_dict()
        else:
            detail = {"text": getattr(result, "text", ""), "rules": []}
        detail["raw"] = raw
        detail["transformed"] = detail.get("transformed", detail.get("text", ""))
        with self.runtime._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._can_persist(connection):
                return
            row = connection.execute(
                "SELECT effective_config_json FROM node_runs WHERE id = ? AND session_id = ?",
                (node_run_id, self.runtime.session_id),
            ).fetchone()
            effective = _load_trace_json(row["effective_config_json"] if row else None, {})
            if not isinstance(effective, dict):
                effective = {}
            effective[f"regex_{target}_transform"] = _redact_trace(detail)
            connection.execute(
                "UPDATE node_runs SET effective_config_json = ? WHERE id = ? AND session_id = ?",
                (_trace_json(effective), node_run_id, self.runtime.session_id),
            )

    def model_call_started(self, node, call_ordinal: int, request) -> None:
        self._ensure_started()
        node_run_id = self.node_run_ids.get(node.node_id)
        if node_run_id is None:
            return
        self._model_call_started_at[(node.node_id, int(call_ordinal))] = time.monotonic()
        request_data = self._request_payload(request)
        call = {
            "call_ordinal": call_ordinal,
            "status": "running",
            "request": request_data,
            "streamed_output": "",
            "final_output": None,
        }
        with self.runtime._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._can_persist(connection):
                return
            aggregate_call_ordinal = self._aggregate_call_ordinal(
                connection, node.node_id, call_ordinal
            )
            call["aggregate_call_ordinal"] = aggregate_call_ordinal
            calls = self._column_json(connection, node_run_id, "model_calls_json", [])
            calls.append(call)
            connection.execute(
                "UPDATE node_runs SET model_calls_json = ? WHERE id = ? AND session_id = ?",
                (_trace_json(calls), node_run_id, self.runtime.session_id),
            )
            payload = self._node_payload(node, node_run_id, state="running")
            payload.update(
                {
                    "call_ordinal": call_ordinal,
                    "aggregate_call_ordinal": aggregate_call_ordinal,
                    "model": request_data.get("model"),
                }
            )
            self.runtime._event(connection, "model_call.started", payload)

    def model_call_finished(self, node, call_ordinal: int, request, text: str, result) -> None:
        self._ensure_started()
        node_run_id = self.node_run_ids.get(node.node_id)
        if node_run_id is None:
            return
        started_at = self._model_call_started_at.pop((node.node_id, int(call_ordinal)), None)
        latency_ms = int(max(0.0, (time.monotonic() - started_at) * 1000.0)) if started_at is not None else 0
        usage = result.usage.as_dict() if result is not None and hasattr(result, "usage") else {}
        cost = result.cost_estimate.as_dict() if result is not None and hasattr(result, "cost_estimate") else {}
        stop_reason = getattr(result, "stop_reason", "") if result is not None else ""
        request_data = self._request_payload(request)
        with self.runtime._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._can_persist(connection):
                return
            calls = self._column_json(connection, node_run_id, "model_calls_json", [])
            call = next((item for item in calls if item.get("call_ordinal") == call_ordinal), None)
            if call is None:
                call = {"call_ordinal": call_ordinal, "request": self._request_payload(request)}
                calls.append(call)
            call.update(
                {
                    "status": "succeeded",
                    "streamed_output": text,
                    "final_output": text,
                    "usage": usage,
                    "stop_reason": stop_reason,
                    "cost_estimate": cost,
                    "latency_ms": latency_ms,
                }
            )
            connection.execute(
                "UPDATE node_runs SET model_calls_json = ? WHERE id = ? AND session_id = ?",
                (_trace_json(calls), node_run_id, self.runtime.session_id),
            )
            # ``model_calls`` is the runtime's aggregate telemetry source used
            # by projections and lineage token totals.  Graph runs historically
            # only populated ``node_runs.model_calls_json``; persist the same
            # redacted result here and make callback re-entry idempotent.
            aggregate_call_ordinal = self._aggregate_call_ordinal(
                connection, node.node_id, call_ordinal
            )
            manifest_id = self._manifest_id(connection)
            prompt_tokens = int(usage.get("prompt_tokens") or 0)
            completion_tokens = int(usage.get("completion_tokens") or 0)
            total_tokens = int(usage.get("total_tokens") or (prompt_tokens + completion_tokens))
            cost_amount = float(cost.get("amount") or 0.0)
            cost_currency = str(cost.get("currency") or "USD")
            cost_rate_version = str(cost.get("rate_version") or "unknown")
            self._upsert_model_call(
                connection,
                aggregate_call_ordinal,
                manifest_id=manifest_id,
                model=request_data.get("model") or "",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                stop_reason=stop_reason,
                latency_ms=latency_ms,
                cost_amount=cost_amount,
                cost_currency=cost_currency,
                cost_rate_version=cost_rate_version,
            )
            payload = self._node_payload(node, node_run_id, state="running")
            payload.update({
                "call_ordinal": call_ordinal,
                "aggregate_call_ordinal": aggregate_call_ordinal,
                "model": request_data.get("model"),
                "usage": usage,
                "stop_reason": stop_reason,
                "cost_estimate": cost,
                "latency_ms": latency_ms,
                "final_output": text,
            })
            self.runtime._event(connection, "model_call.finished", payload)

    def model_call_failed(self, node, call_ordinal: int, request, error) -> None:
        """Persist a terminal provider/transport failure for an active call."""
        self._ensure_started()
        node_run_id = self.node_run_ids.get(node.node_id)
        if node_run_id is None:
            return
        started_at = self._model_call_started_at.pop((node.node_id, int(call_ordinal)), None)
        latency_ms = int(max(0.0, (time.monotonic() - started_at) * 1000.0)) if started_at is not None else 0
        code = str(getattr(error, "category", None) or error.__class__.__name__ or "provider_error")
        message = str(error)
        retryable = bool(getattr(error, "retryable", False))
        request_data = self._request_payload(request)
        error_payload = {"code": code, "message": message, "retryable": retryable}
        with self.runtime._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._can_persist(connection):
                return
            calls = self._column_json(connection, node_run_id, "model_calls_json", [])
            call = next((item for item in calls if item.get("call_ordinal") == call_ordinal), None)
            if call is None:
                call = {"call_ordinal": call_ordinal, "request": request_data}
                calls.append(call)
            call.update({"status": "failed", "error": error_payload, "latency_ms": latency_ms})
            connection.execute(
                "UPDATE node_runs SET model_calls_json = ? WHERE id = ? AND session_id = ?",
                (_trace_json(calls), node_run_id, self.runtime.session_id),
            )
            aggregate_call_ordinal = self._aggregate_call_ordinal(
                connection, node.node_id, call_ordinal
            )
            self._upsert_model_call(
                connection,
                aggregate_call_ordinal,
                manifest_id=self._manifest_id(connection),
                model=request_data.get("model") or "",
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                stop_reason=code,
                latency_ms=latency_ms,
                cost_amount=0.0,
                cost_currency="USD",
                cost_rate_version="failure-v1",
            )
            payload = self._node_payload(node, node_run_id, state="failed")
            payload.update(
                {
                    "call_ordinal": call_ordinal,
                    "aggregate_call_ordinal": aggregate_call_ordinal,
                    "model": request_data.get("model"),
                    "error": _redact_trace(error_payload),
                    "retryable": retryable,
                    "latency_ms": latency_ms,
                    "status": "failed",
                }
            )
            self.runtime._event(connection, "model_call.failed", payload)

    def _aggregate_call_ordinal(self, connection, node_id: str, call_ordinal: int) -> int:
        """Map a node-local call ordinal onto the task-wide telemetry sequence.

        ``ProviderNodeRunner`` restarts its ordinal at one for every node, while
        ``model_calls`` is task-scoped.  Persisting the local ordinal directly
        makes a later node overwrite an earlier node's aggregate telemetry.
        Graph execution is sequential today, so allocating on the start event
        produces a stable task-wide sequence without leaking storage concerns
        into the provider-independent runner.
        """
        key = (node_id, int(call_ordinal))
        existing = self._aggregate_call_ordinals.get(key)
        if existing is not None:
            return existing
        row = connection.execute(
            "SELECT COALESCE(MAX(call_ordinal), 0) AS maximum FROM model_calls "
            "WHERE session_id = ? AND task_id = ?",
            (self.runtime.session_id, self.task_id),
        ).fetchone()
        aggregate = int(row["maximum"] or 0) + 1
        self._aggregate_call_ordinals[key] = aggregate
        return aggregate

    def _manifest_id(self, connection) -> str | None:
        # A Graph Task freezes one task-level context manifest (ordinal zero).
        # All node calls share that frozen source snapshot; node-specific prompt
        # provenance remains in ``node_runs``.
        row = connection.execute(
            "SELECT id FROM context_manifests WHERE session_id = ? AND task_id = ? "
            "ORDER BY call_ordinal LIMIT 1",
            (self.runtime.session_id, self.task_id),
        ).fetchone()
        return row["id"] if row else None

    def _upsert_model_call(
        self,
        connection,
        call_ordinal: int,
        *,
        manifest_id: str | None,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        stop_reason: str,
        latency_ms: int,
        cost_amount: float,
        cost_currency: str,
        cost_rate_version: str,
    ) -> None:
        existing = connection.execute(
            "SELECT id FROM model_calls WHERE session_id = ? AND task_id = ? AND call_ordinal = ?",
            (self.runtime.session_id, self.task_id, int(call_ordinal)),
        ).fetchone()
        values = (
            manifest_id,
            model,
            int(prompt_tokens),
            int(completion_tokens),
            int(total_tokens),
            stop_reason,
            int(latency_ms),
            float(cost_amount),
            cost_currency,
            cost_rate_version,
        )
        if existing:
            connection.execute(
                "UPDATE model_calls SET manifest_id = ?, model = ?, prompt_tokens = ?, "
                "completion_tokens = ?, total_tokens = ?, stop_reason = ?, latency_ms = ?, "
                "cost_amount = ?, cost_currency = ?, cost_rate_version = ? "
                "WHERE id = ? AND session_id = ?",
                (*values, existing["id"], self.runtime.session_id),
            )
            return
        connection.execute(
            "INSERT INTO model_calls "
            "(id, session_id, task_id, call_ordinal, manifest_id, model, prompt_tokens, "
            "completion_tokens, total_tokens, stop_reason, latency_ms, cost_amount, "
            "cost_currency, cost_rate_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                self.runtime._id(),
                self.runtime.session_id,
                self.task_id,
                int(call_ordinal),
                *values,
            ),
        )

    def tool_call_started(self, node, call) -> None:
        self._ensure_started()
        node_run_id = self.node_run_ids.get(node.node_id)
        if node_run_id is None:
            return
        with self.runtime._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._can_persist(connection):
                return
            calls = self._column_json(connection, node_run_id, "tool_calls_json", [])
            calls.append({"id": call.get("id"), "name": call.get("name"), "args": _redact_trace(call.get("args") or {}), "status": "running"})
            connection.execute(
                "UPDATE node_runs SET tool_calls_json = ? WHERE id = ? AND session_id = ?",
                (_trace_json(calls), node_run_id, self.runtime.session_id),
            )
            payload = self._node_payload(node, node_run_id, state="running")
            payload.update({"tool": call.get("name"), "args": _redact_trace(call.get("args") or {})})
            self.runtime._event(connection, "tool_run.started", payload)

    def tool_call_finished(self, node, call, value, error) -> None:
        self._ensure_started()
        node_run_id = self.node_run_ids.get(node.node_id)
        if node_run_id is None:
            return
        with self.runtime._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._can_persist(connection):
                return
            calls = self._column_json(connection, node_run_id, "tool_calls_json", [])
            matching = next((item for item in reversed(calls) if item.get("id") == call.get("id")), None)
            if matching is None:
                matching = {"id": call.get("id"), "name": call.get("name"), "args": _redact_trace(call.get("args") or {})}
                calls.append(matching)
            matching.update({"status": "failed" if error is not None else "succeeded", "result": _redact_trace(value), "error": _redact_trace(str(error)) if error else None})
            connection.execute(
                "UPDATE node_runs SET tool_calls_json = ? WHERE id = ? AND session_id = ?",
                (_trace_json(calls), node_run_id, self.runtime.session_id),
            )
            payload = self._node_payload(node, node_run_id, state="running")
            payload.update({"tool": call.get("name"), "ok": error is None, "result": _redact_trace(value), "error": _redact_trace(str(error)) if error else None})
            self.runtime._event(connection, "tool_run.finished", payload)

    def node_finished(self, node, input_artifact, result) -> None:
        self._ensure_started()
        node_run_id = self.node_run_ids.get(node.node_id)
        if node_run_id is None:
            return
        state = "succeeded" if result.ok else "failed"
        artifact = result.primary_artifact.to_dict() if result.primary_artifact else None
        final_output = artifact.get("content") if isinstance(artifact, dict) else None
        if final_output is not None and not isinstance(final_output, str):
            final_output = json.dumps(final_output, ensure_ascii=False)
        error = _redact_trace(result.error) if not result.ok else None
        now = int(time.time())
        with self.runtime._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._can_persist(connection):
                return
            connection.execute(
                "UPDATE node_runs SET state = ?, final_output = ?, artifact_json = ?, error_json = ?, diagnostics_ref = ?, finished_at = ? "
                "WHERE id = ? AND session_id = ?",
                (state, final_output, _trace_json(artifact), _trace_json(error), result.diagnostics_ref, now, node_run_id, self.runtime.session_id),
            )
            payload = self._node_payload(node, node_run_id, state=state)
            payload.update({"artifact": _redact_trace(artifact), "error": error, "final_output": final_output})
            self.runtime._event(connection, "graph.node.finished", payload)
            self.runtime._event(
                connection,
                "agent_node.finished",
                {"task_id": self.task_id, "node_id": node.node_id, "role": node.agent.name, "node_run_id": node_run_id, "state": state},
            )

    def handoff_prepared(self, source, target, source_artifact, handoff_artifact) -> None:
        """Record the exact Regex-processed Artifact delivered to the next node."""
        self._ensure_started()
        node_run_id = self.node_run_ids.get(source.node_id)
        if node_run_id is None:
            return
        handoff = handoff_artifact.to_dict() if hasattr(handoff_artifact, "to_dict") else handoff_artifact
        source_value = source_artifact.to_dict() if hasattr(source_artifact, "to_dict") else source_artifact
        with self.runtime._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._can_persist(connection):
                return
            connection.execute(
                "UPDATE node_runs SET handoff_artifact_json = ? WHERE id = ? AND session_id = ?",
                (_trace_json(handoff), node_run_id, self.runtime.session_id),
            )
            payload = self._node_payload(source, node_run_id, state="succeeded")
            payload.update(
                {
                    "target_node_id": target.node_id,
                    "target_node_run_id": self.node_run_ids.get(target.node_id),
                    "source_artifact": _redact_trace(source_value),
                    "handoff_artifact": _redact_trace(handoff),
                    "loop_id": source.loop_id,
                    "loop_iteration": source.loop_iteration,
                }
            )
            self.runtime._event(connection, "graph.node.handoff", payload)

    def graph_finished(self, result) -> None:
        self._ensure_started()
        graph_error = _redact_trace(getattr(result, "error", None))
        cancelled = bool(
            isinstance(graph_error, dict) and graph_error.get("code") in {"aborted", "cancelled"}
        )
        if result.ok:
            state = "succeeded"
        elif cancelled:
            state = "cancelled"
        else:
            state = "failed"
        retryable = bool(isinstance(graph_error, dict) and graph_error.get("retryable"))
        now = int(time.time())
        with self.runtime._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._can_persist(connection):
                return
            updated = connection.execute(
                "UPDATE graph_runs SET status = ?, failed_node_id = ?, error_json = ?, finished_at = ? "
                "WHERE id = ? AND session_id = ? AND status = 'running'",
                (state, result.failed_node_id, _trace_json(getattr(result, "error", None)), now, self.graph_run_id, self.runtime.session_id),
            )
            if not updated.rowcount:
                return
            self.runtime._event(
                connection,
                "graph.run.finished",
                {
                    "task_id": self.task_id,
                    "graph_run_id": self.graph_run_id,
                    "run_id": self.graph_run_id,
                    "plan_id": result.plan_id,
                    "retry_of": self.retry_of,
                    "state": state,
                    "status": state,
                    "failed_node_id": result.failed_node_id,
                    "error": graph_error,
                    "retryable": retryable,
                },
            )
        self.runtime._prune_graph_runs()

    def mark_interrupted(self, reason="interrupted") -> None:
        if not self.graph_run_id:
            return
        with self.runtime._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE graph_runs SET status = 'interrupted', error_json = ?, finished_at = ? WHERE id = ? AND session_id = ? AND status = 'running'",
                (_trace_json({"code": reason}), int(time.time()), self.graph_run_id, self.runtime.session_id),
            )
            connection.execute(
                "UPDATE node_runs SET state = 'failed', error_json = ?, finished_at = ? WHERE graph_run_id = ? AND session_id = ? AND state = 'running'",
                (_trace_json({"code": reason}), int(time.time()), self.graph_run_id, self.runtime.session_id),
            )

    def _ensure_started(self, plan=None) -> None:
        if self.graph_run_id is not None:
            return
        plan = plan or self.plan
        with self.runtime._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT id FROM graph_runs WHERE session_id = ? AND task_id = ?",
                (self.runtime.session_id, self.task_id),
            ).fetchone()
            if existing:
                self.graph_run_id = existing["id"]
                rows = connection.execute(
                    "SELECT id, node_id FROM node_runs WHERE graph_run_id = ? AND session_id = ?",
                    (self.graph_run_id, self.runtime.session_id),
                ).fetchall()
                self.node_run_ids = {row["node_id"]: row["id"] for row in rows}
                return
            self.graph_run_id = self.runtime._id()
            now = int(time.time())
            tool_snapshot = self.runtime._graph_tool_snapshot(plan, self.task)
            connection.execute(
                "INSERT INTO graph_runs (id, session_id, task_id, plan_id, graph_id, retry_of, status, player_input, plan_json, created_at, started_at) VALUES (?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)",
                (self.graph_run_id, self.runtime.session_id, self.task_id, plan.plan_id, plan.graph.graph_id, self.retry_of, "" if self.task is None else self.task["text"], _trace_json(plan.to_dict()), now, now),
            )
            for node in plan.graph.nodes:
                node_run_id = self.runtime._id()
                self.node_run_ids[node.node_id] = node_run_id
                effective = _redact_trace(node.agent.effective_config)
                connection.execute(
                    "INSERT INTO node_runs (id, session_id, graph_run_id, task_id, node_id, agent_id, label, order_index, state, prompt_json, prompt_provenance_json, effective_config_json, model_calls_json, tool_calls_json, streamed_output, tool_snapshot_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'idle', ?, ?, ?, '[]', '[]', '', ?)",
                    (
                        node_run_id,
                        self.runtime.session_id,
                        self.graph_run_id,
                        self.task_id,
                        node.node_id,
                        node.agent_id,
                        node.label or node.agent.name,
                        node.order,
                        _trace_json(list(node.agent.prompt)),
                        _trace_json(list(node.agent.prompt_provenance)),
                        _trace_json(effective),
                        _trace_json(tool_snapshot),
                    ),
                )
            self.runtime._event(
                connection,
                "graph.run.started",
                {
                    "task_id": self.task_id,
                    "graph_run_id": self.graph_run_id,
                    "run_id": self.graph_run_id,
                    "plan_id": plan.plan_id,
                    "graph_id": plan.graph.graph_id,
                    "retry_of": self.retry_of,
                    "state": "running",
                    "status": "running",
                    "nodes": [node.node_id for node in plan.graph.nodes if node.enabled],
                },
            )

    def _can_persist(self, connection) -> bool:
        """Reject callbacks from a worker superseded by restart recovery."""
        if not self.runtime._lease_is_authoritative(connection, self.task):
            return False
        row = connection.execute(
            "SELECT status FROM graph_runs WHERE id = ? AND session_id = ?",
            (self.graph_run_id, self.runtime.session_id),
        ).fetchone()
        return bool(row and row["status"] == "running")

    def _node_payload(self, node, node_run_id: str, *, state: str) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "graph_run_id": self.graph_run_id,
            "run_id": self.graph_run_id,
            "node_run_id": node_run_id,
            "node_id": node.node_id,
            "agent_id": node.agent_id,
            "label": node.label or node.agent.name,
            "state": state,
            "status": state,
        }

    @staticmethod
    def _request_payload(request) -> dict[str, Any]:
        return _redact_trace(
            {
                "messages": copy.deepcopy(getattr(request, "messages", [])),
                "tools": copy.deepcopy(getattr(request, "tools", [])),
                "model": getattr(request, "model", ""),
                "metadata": copy.deepcopy(getattr(request, "metadata", {})),
            }
        )

    def _column_json(self, connection, node_run_id: str, column: str, fallback):
        row = connection.execute(
            f"SELECT {column} FROM node_runs WHERE id = ? AND session_id = ?",
            (node_run_id, self.runtime.session_id),
        ).fetchone()
        value = _load_trace_json(row[column] if row else None, fallback)
        return value if isinstance(value, list) else copy.deepcopy(fallback)
