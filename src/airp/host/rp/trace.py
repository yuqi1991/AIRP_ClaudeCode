"""Graph Run trace persistence and credential-safe debug serialization.

This module depends on the runtime's small persistence seam instead of owning
session, task, or projection state.
"""

from __future__ import annotations

import copy
import json
import re
import time
from typing import Any

from airp.engine.graph_runtime import ExecutionPlan
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
            calls = self._column_json(connection, node_run_id, "model_calls_json", [])
            calls.append(call)
            connection.execute(
                "UPDATE node_runs SET model_calls_json = ? WHERE id = ? AND session_id = ?",
                (_trace_json(calls), node_run_id, self.runtime.session_id),
            )
            payload = self._node_payload(node, node_run_id, state="running")
            payload.update({"call_ordinal": call_ordinal, "model": request_data.get("model")})
            self.runtime._event(connection, "model_call.started", payload)

    def model_call_finished(self, node, call_ordinal: int, request, text: str, result) -> None:
        self._ensure_started()
        node_run_id = self.node_run_ids.get(node.node_id)
        if node_run_id is None:
            return
        usage = result.usage.as_dict() if result is not None and hasattr(result, "usage") else {}
        cost = result.cost_estimate.as_dict() if result is not None and hasattr(result, "cost_estimate") else {}
        stop_reason = getattr(result, "stop_reason", "") if result is not None else ""
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
                }
            )
            connection.execute(
                "UPDATE node_runs SET model_calls_json = ? WHERE id = ? AND session_id = ?",
                (_trace_json(calls), node_run_id, self.runtime.session_id),
            )
            payload = self._node_payload(node, node_run_id, state="running")
            payload.update({
                "call_ordinal": call_ordinal,
                "model": self._request_payload(request).get("model"),
                "usage": usage,
                "stop_reason": stop_reason,
                "cost_estimate": cost,
                "final_output": text,
            })
            self.runtime._event(connection, "model_call.finished", payload)

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

    def graph_finished(self, result) -> None:
        self._ensure_started()
        state = "succeeded" if result.ok else "failed"
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
