"""RP host Session Turn Runtime.

This module owns durable story sessions, revision commits, card projection,
and Graph-to-turn integration. The content-neutral execution contracts remain
in :mod:`airp.engine` and are injected here by the host composition root.
"""

import copy
import hashlib
import inspect
import json
import re
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from airp.engine.agent_framework import AgentFrameworkExecutor
from airp.compat.legacy_turn_import import parse_legacy_turn
from airp.engine.context_compiler import (
    CompiledContext,
    ContextCompileRequest,
    ContextPolicy,
    compile_context,
    compile_sequential_handoff_context,
    replay_payload,
)
from airp.engine.graph_runtime import (
    AgentArtifact,
    ExecutionPlan,
    ExecutionPlanCompiler,
    GraphExecutionError,
    GraphRuntime,
    NodeExecutionContext,
    NodeResult,
)
from airp.engine.mvu import execute_commands, extract_commands, generate_schema, validate_command_strict
from airp.engine.provider import AbortSignal
from airp.engine.quality import DefaultQualityGate, QualityContext, QualityGate, QualityPolicy
from airp.host.rp.tools import ToolResult, ToolRegistry, validate_draft_dict
from airp.host.card_projection import CardProjection
from airp.host.graph_turn_commit import GraphTurnCommitExecutor, turn_draft_from_artifact
from airp.engine.worldbook import load_worldbook_entry_from_texts


@dataclass(frozen=True)
class TurnDraft:
    """Structured narrative turn, authored by the director and committed by the runtime.

    ``content`` / ``summary`` / ``options`` mirror the existing projection
    contract; ``polished_input`` carries an optional model editor-pass for
    inspection only (the durable player message always remains task-owned
    input) and ``mvu_commands`` carries the raw MVU payload
    (``_.set()`` / ``<JSONPatch>`` / ``<UpdateVariable>``). When
    ``mvu_commands`` is empty, MVU is extracted from ``content`` for backward
    compatibility with the deterministic fake executor.
"""

    content: str
    summary: str = ""
    options: str = ""
    polished_input: str = ""
    mvu_commands: str = ""

    def to_json(self):
        return json.dumps(
            {
                "content": self.content,
                "summary": self.summary,
                "options": self.options,
                "polished_input": self.polished_input,
                "mvu_commands": self.mvu_commands,
            },
            ensure_ascii=False,
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, raw):
        data = json.loads(raw)
        return cls(
            content=data["content"],
            summary=data.get("summary", ""),
            options=data.get("options", ""),
            polished_input=data.get("polished_input", ""),
            mvu_commands=data.get("mvu_commands", ""),
        )


@dataclass(frozen=True)
class RuntimeResult:
    task_id: str
    commit_id: str | None
    revision: int
    status: str
    attempt: int = 0


@dataclass(frozen=True)
class RuntimeEvent:
    sequence: int
    type: str
    payload: dict


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


@dataclass(frozen=True)
class TurnCommit:
    id: str
    revision: int
    task_id: str


@dataclass(frozen=True)
class CommitLineage:
    """Public read model for a commit's place in the revision DAG."""

    id: str
    revision: int
    task_id: str
    parent_revision: int
    text: str = ""


class SessionTurnRuntime:
    def __init__(
        self,
        database_path,
        card_folder,
        projection_root,
        session_id="local",
        manifest_policy=None,
        session_settings=None,
        quality_gate: QualityGate | None = None,
        quality_policy: QualityPolicy | None = None,
        max_commit_validation_retries: int = 1,
        execution_plan_compiler: ExecutionPlanCompiler | None = None,
        graph_runtime: GraphRuntime | None = None,
        bootstrap_legacy_history: bool = True,
        worldbook_snapshot_provider=None,
        project_id=None,
        execution_graph_id: str | None = None,
    ):
        self.database_path = Path(database_path)
        self.card_folder = Path(card_folder)
        self.session_id = session_id
        self.manifest_policy = manifest_policy or ContextPolicy(version="runtime-v1", token_budget=8000)
        self.session_settings = json.loads(self._canonical(session_settings or {}))
        self.execution_plan_compiler = execution_plan_compiler
        self.graph_runtime = graph_runtime
        # Selection is supplied by the host/runtime state, never inferred from
        # Project content. The constructor seam keeps embedders and test hosts
        # able to restore an already-selected Graph before their first run.
        self.execution_graph_id = execution_graph_id
        self.bootstrap_legacy_history = bool(bootstrap_legacy_history)
        self.worldbook_snapshot_provider = worldbook_snapshot_provider
        self.project_id = project_id or self.card_folder.name
        self.quality_policy = quality_policy or QualityPolicy()
        self.quality_gate = quality_gate or DefaultQualityGate(self.quality_policy)
        # Commit validation is terminal for this task. A user retry creates a
        # fresh graph run, so there is no in-task regeneration budget.
        # Kept as an ignored constructor argument for old embedders. A failed
        # validation is terminal for this graph run; the user retries the run.
        self.projection = CardProjection(card_folder, projection_root)
        self._lock = threading.RLock()
        self._abort_signals: dict[str, AbortSignal] = {}
        self._initialize()

    def configure_worldbook_library(self, snapshot_provider, *, project_id=None):
        """Use Project bindings as the Worldbook source for future snapshots."""
        self.worldbook_snapshot_provider = snapshot_provider
        if project_id is not None:
            self.project_id = project_id

    def configure_execution_graph(
        self,
        compiler: ExecutionPlanCompiler | None,
        graph_runtime: GraphRuntime | None,
        *,
        project_id=None,
        graph_id=None,
    ):
        """Enable Studio Graph execution for subsequent task snapshots."""
        self.execution_plan_compiler = compiler
        self.graph_runtime = graph_runtime
        if project_id is not None:
            self.project_id = project_id
        self.execution_graph_id = graph_id

    def submit(self, text, idempotency_key):
        if not text.strip():
            raise ValueError("empty input")
        with self._lock:
            task = self._create_or_get_task(text, idempotency_key)
        return self._submit_queued_task(task)

    def compile_opening_context(self, instruction):
        """Compile generic runtime context against revision 0 for opening generation.

        Opening generation is a provider phase, not a player turn, so it must
        not allocate a task, commit, or revision. The returned context still
        uses the same frozen card/settings sources as normal turns.
        """
        if not isinstance(instruction, str):
            raise ValueError("opening instruction must be text")
        snapshot = self._source_snapshot(0)
        return compile_context(
            ContextCompileRequest(
                session_id=self.session_id,
                task_id="opening",
                base_revision=0,
                player_input=instruction,
                snapshot=snapshot,
                policy=self._policy_for_snapshot(snapshot),
            )
        )

    def generate_opening_draft(self) -> TurnDraft:
        """Run the active Studio Graph once for an AI-only opening turn.

        This is deliberately a host operation: it does not create a player
        task, a Graph Run trace, or a revision. It does execute the same frozen
        Agent/Provider/Regex plan used by normal Graph Runs.
        """
        snapshot = self._source_snapshot(0, player_input="")
        plan_data = snapshot.get("execution_plan")
        if not plan_data or self.graph_runtime is None:
            raise ValueError("generated opening requires an active Studio Graph")
        plan = ExecutionPlan.from_dict(plan_data)
        # An opening has no durable task or Graph Run trace, but it must expose
        # exactly the same frozen, read-only host capabilities as a player turn.
        opening_task = {
            "id": None,
            "status": "opening",
            "base_revision": 0,
            "source_snapshot": self._canonical(snapshot),
        }
        opening_context = NodeExecutionContext(
            tool_registry=ToolRegistry(
                self,
                opening_task,
                self.manifest_policy,
                snapshot=self._graph_tool_snapshot(plan, opening_task),
            )
        )
        result = self.graph_runtime.run(
            plan,
            AgentArtifact.input(""),
            execution_context=opening_context,
        )
        if not result.ok or result.output_artifact is None:
            raise GraphExecutionError(result)
        return turn_draft_from_artifact(result.output_artifact)

    def _submit_queued_task(self, task):
        """Return promptly unless this caller atomically acquires the generation lease.

        A session owns one durable generation lease. The owner drains FIFO work;
        all other callers merely observe their durable task row. This deliberately
        keeps HTTP's background submit contract intact while making direct library
        calls safe during a blocked Graph Run.
        """
        result = self._result(task)
        if result.commit_id:
            return self._project(result)
        if result.status not in ("queued", "leased", "running"):
            return result
        claimed = self._claim_next_generation()
        if claimed is None or claimed["id"] != task["id"]:
            return self.task(task["id"]) or result
        return self._drain_generation(claimed, task["id"])

    def _drain_generation(self, task, requested_task_id):
        """Run the leased task and synchronously drain later FIFO work."""
        requested_result = None
        current = task
        while current is not None:
            try:
                result = self._run_leased_task(current)
            finally:
                self._release_generation(current)
            if current["id"] == requested_task_id:
                requested_result = result
            current = self._claim_next_generation()
        return requested_result or self.task(requested_task_id)

    def _run_leased_task(self, task):
        signal = AbortSignal()
        self._abort_signals[task["id"]] = signal
        try:
            compiled = self._compile_and_persist(task)
            if compiled is None:
                return self.task(task["id"]) or self._result(task)
            try:
                executor = self._graph_turn_executor_for_task(task, signal)
                if executor is None:
                    return self._fail_graph_configuration(task)
                draft = executor.run(task["text"], compiled)
                return self._project(self._commit_draft(task, draft))
            except GraphExecutionError as exc:
                return self._fail_graph_run(task, exc)
        finally:
            self._abort_signals.pop(task["id"], None)

    def _claim_next_generation(self):
        """Lease the oldest eligible task, incrementing the durable generation fence."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            session = connection.execute(
                "SELECT active_generation_task_id, generation_fence FROM sessions WHERE id = ?",
                (self.session_id,),
            ).fetchone()
            if session is None or session["active_generation_task_id"]:
                return None
            task = connection.execute(
                "SELECT id, commit_id, revision, status, text, base_revision, source_snapshot, queue_sequence, lease_fence "
                "FROM tasks WHERE session_id = ? AND status = 'queued' "
                "ORDER BY queue_sequence, rowid LIMIT 1",
                (self.session_id,),
            ).fetchone()
            if task is None:
                return None
            fence = int(session["generation_fence"] or 0) + 1
            connection.execute(
                "UPDATE sessions SET active_generation_task_id = ?, generation_fence = ? WHERE id = ? "
                "AND active_generation_task_id IS NULL",
                (task["id"], fence, self.session_id),
            )
            connection.execute(
                "UPDATE tasks SET status = ?, lease_fence = ? WHERE id = ? AND status = 'queued'",
                ("leased", fence, task["id"]),
            )
            connection.execute(
                "INSERT INTO task_attempts (id, session_id, task_id, lease_fence, status) VALUES (?, ?, ?, ?, ?)",
                (self._id(), self.session_id, task["id"], fence, "leased"),
            )
            self._event(connection, "task.leased", {"task_id": task["id"], "lease_fence": fence})
            return connection.execute(
                "SELECT id, commit_id, revision, status, text, base_revision, source_snapshot, queue_sequence, lease_fence "
                "FROM tasks WHERE id = ?", (task["id"],)
            ).fetchone()

    def _release_generation(self, task):
        """Release only the lease instance that acquired this exact fence."""
        fence = task["lease_fence"]
        if fence is None:
            return
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE sessions SET active_generation_task_id = NULL WHERE id = ? "
                "AND active_generation_task_id = ? AND generation_fence = ?",
                (self.session_id, task["id"], fence),
            )
            connection.execute(
                "UPDATE task_attempts SET status = ? WHERE task_id = ? AND lease_fence = ? AND status = ?",
                ("finished", task["id"], fence, "leased"),
            )

    def _lease_is_authoritative(self, connection, task):
        # Legacy/direct reroll paths predate the lease columns on their selected
        # row. Command-service generation always supplies a fenced row.
        if "lease_fence" not in task.keys():
            return True
        return bool(connection.execute(
            "SELECT 1 FROM sessions WHERE id = ? AND active_generation_task_id = ? AND generation_fence = ?",
            (self.session_id, task["id"], task["lease_fence"]),
        ).fetchone())

    def stop(self, task_id):
        """Cancel a running or queued Graph task.

        For a task whose executor is currently running, this cancels the shared
        :class:`AbortSignal`; the node runner and provider stream observe it
        cooperatively and the runtime then transitions the task to
        ``cancelled``. For a not-yet-running task, the row is marked
        ``cancelled`` directly. Never un-commits an already-committed turn.
        """
        signal = self._abort_signals.get(task_id)
        if signal is not None:
            signal.cancel()
            return True
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM tasks WHERE id = ? AND session_id = ?",
                (task_id, self.session_id),
            ).fetchone()
            if row and row["status"] in ("queued",):
                connection.execute(
                    "UPDATE tasks SET status = ? WHERE id = ?", ("cancelled", task_id)
                )
                self._event(connection, "task.cancelled", {"task_id": task_id})
                return True
        return False

    def task_id_for_key(self, idempotency_key):
        stored_key = self._stored_idempotency_key(idempotency_key)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id FROM tasks WHERE session_id = ? "
                "AND idempotency_key IN (?, ?) "
                "ORDER BY (idempotency_key = ?) DESC LIMIT 1",
                (self.session_id, stored_key, idempotency_key, stored_key),
            ).fetchone()
        return row["id"] if row else None

    def active_revision(self):
        """Return the active branch head revision (what new turns build on)."""
        with self._connect() as connection:
            return connection.execute(
                "SELECT active_revision FROM sessions WHERE id = ?", (self.session_id,)
            ).fetchone()["active_revision"]

    def generation_active(self):
        """Whether this session currently has a durable generation lease."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT active_generation_task_id FROM sessions WHERE id = ?", (self.session_id,)
            ).fetchone()
        return bool(row and row["active_generation_task_id"])

    def task(self, task_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, commit_id, revision, status, (SELECT COUNT(*) FROM task_attempts WHERE task_id = tasks.id) AS attempt FROM tasks WHERE id = ? AND session_id = ?",
                (task_id, self.session_id),
            ).fetchone()
        return self._result(row) if row else None

    def commit_for_revision(self, revision):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, revision, task_id FROM commits WHERE session_id = ? AND revision = ?",
                (self.session_id, revision),
            ).fetchone()
        if not row:
            return None
        return TurnCommit(id=row["id"], revision=row["revision"], task_id=row["task_id"])

    def commit_lineage(self, revision):
        """Public read: commit identity + parent_revision + player text at ``revision``."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT commits.id, commits.revision, commits.task_id, commits.parent_revision, tasks.text "
                "FROM commits JOIN tasks ON tasks.id = commits.task_id "
                "WHERE commits.session_id = ? AND commits.revision = ?",
                (self.session_id, revision),
            ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "revision": row["revision"],
            "task_id": row["task_id"],
            "parent_revision": row["parent_revision"] if row["parent_revision"] is not None else 0,
            "text": row["text"] or "",
        }

    def active_lineage_turns(self, limit=3, head_revision=None):
        """Recent turns on the active branch only (parent-chain walk, oldest→newest)."""
        head = self.active_revision() if head_revision is None else head_revision
        return self._runtime_turns(head if head is not None else 0, limit=limit)

    def reroll(self, revision, idempotency_key):
        """Reroll the assistant outcome at ``revision`` reusing the original player input.

        Creates a **new** task/commit whose ``parent_revision`` equals the parent of
        the rerolled turn. On success the new commit becomes the active head; the
        superseded commit remains queryable for audit but leaves the active lineage.
        """
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("missing idempotency_key")
        if self.generation_active():
            return RuntimeResult("", None, self.active_revision(), "generation_busy")
        if revision is None or not isinstance(revision, int) or revision < 0:
            raise ValueError("invalid revision")
        if revision == 0:
            return RuntimeResult("", None, self.active_revision(), "cannot_reroll_opening")

        lineage = self.commit_lineage(revision)
        if lineage is None:
            return RuntimeResult("", None, self.active_revision(), "unknown_revision")
        text = (lineage.get("text") or "").strip()
        if not text:
            return RuntimeResult("", None, self.active_revision(), "cannot_reroll_opening")

        parent_revision = lineage["parent_revision"]
        # Snapshot from the parent chain only — safe outside the write txn
        # because _runtime_turns walks parents of parent_revision, not the
        # active head. Avoids nested connections while BEGIN IMMEDIATE is held.
        source_snapshot = self._source_snapshot(parent_revision, player_input=text)
        project_existing = None
        task = None
        signal = None
        with self._lock:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = self._task_for_key(connection, idempotency_key)
                if existing and existing["commit_id"]:
                    project_existing = self._result(existing)
                    task = existing
                elif existing:
                    task = existing
                else:
                    # Freeze base at the *parent* of the rerolled turn — not the active head.
                    # Move the active head to that parent BEFORE generation so the
                    # optimistic ``active == base`` check can succeed when the new
                    # tip commits. The superseded tip stays in commits for audit.
                    from_revision = self._active_revision(connection)
                    if from_revision != parent_revision:
                        connection.execute(
                            "UPDATE sessions SET active_revision = ? WHERE id = ?",
                            (parent_revision, self.session_id),
                        )
                        self._event(
                            connection,
                            "session.head_moved",
                            {
                                "from_revision": from_revision,
                                "to_revision": parent_revision,
                                "reason": "reroll",
                                "reroll_of_revision": revision,
                                "idempotency_key": idempotency_key,
                            },
                        )
                    task_id = self._id()
                    connection.execute(
                        "INSERT INTO tasks (id, session_id, idempotency_key, text, status, revision, base_revision, source_snapshot) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            task_id,
                            self.session_id,
                            self._stored_idempotency_key(idempotency_key),
                            text,
                            "queued",
                            0,
                            parent_revision,
                            self._canonical(source_snapshot),
                        ),
                    )
                    self._event(
                        connection,
                        "task.reroll_requested",
                        {
                            "task_id": task_id,
                            "reroll_of_revision": revision,
                            "parent_revision": parent_revision,
                            "text": text,
                            "idempotency_key": idempotency_key,
                        },
                    )
                    self._event(connection, "task.queued", {"task_id": task_id, "base_revision": parent_revision})
                    task = self._task_for_key(connection, idempotency_key)

            if project_existing is not None:
                return self._project(project_existing)

            result = self._result(task)
            if result.commit_id:
                return self._project(result)
            if result.status == "succeeded":
                return result

            signal = AbortSignal()
            self._abort_signals[task["id"]] = signal
        try:
            compiled = self._compile_and_persist(task)
            if compiled is None:
                return self.task(task["id"]) or self._result(task)
            try:
                executor = self._graph_turn_executor_for_task(task, signal)
                if executor is None:
                    return self._fail_graph_configuration(task)
                draft = executor.run(text, compiled)
                result = self._commit_draft(task, draft)
                return self._project(result)
            except GraphExecutionError as exc:
                return self._fail_graph_run(task, exc)
        finally:
            self._abort_signals.pop(task["id"], None)

    def rollback(self, revision, idempotency_key):
        """Move the active head to an existing committed revision without deleting history.

        Subsequent submits descend from the new head (may create a new branch).
        In-flight tasks frozen at a different ``base_revision`` fail with ``stale_revision``.
        """
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("missing idempotency_key")
        if revision is None or not isinstance(revision, int) or revision < 0:
            raise ValueError("invalid revision")

        with self._lock:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                # Idempotent replay: same key already moved the head.
                prior = connection.execute(
                    "SELECT payload FROM events WHERE session_id = ? AND type = ? ORDER BY sequence",
                    (self.session_id, "session.head_moved"),
                ).fetchall()
                for row in prior:
                    payload = json.loads(row["payload"])
                    if payload.get("idempotency_key") == idempotency_key:
                        to_rev = payload.get("to_revision", revision)
                        return RuntimeResult("", None, to_rev, "rolled_back")

                if revision > 0:
                    commit_row = connection.execute(
                        "SELECT id FROM commits WHERE session_id = ? AND revision = ?",
                        (self.session_id, revision),
                    ).fetchone()
                    if not commit_row:
                        return RuntimeResult("", None, self._active_revision(connection), "unknown_revision")
                # revision == 0 is always valid (empty/opening head)

                from_revision = self._active_revision(connection)
                if from_revision == revision:
                    self._event(
                        connection,
                        "session.head_moved",
                        {
                            "from_revision": from_revision,
                            "to_revision": revision,
                            "idempotency_key": idempotency_key,
                            "noop": True,
                        },
                    )
                    return RuntimeResult("", None, revision, "rolled_back")

                connection.execute(
                    "UPDATE sessions SET active_revision = ? WHERE id = ?",
                    (revision, self.session_id),
                )
                self._event(
                    connection,
                    "session.head_moved",
                    {
                        "from_revision": from_revision,
                        "to_revision": revision,
                        "idempotency_key": idempotency_key,
                    },
                )
                self._event(
                    connection,
                    "session.rolled_back",
                    {
                        "from_revision": from_revision,
                        "to_revision": revision,
                        "idempotency_key": idempotency_key,
                    },
                )

            # Rebuild compatibility projections for the new active head (no commit delete).
            self._rebuild_active_projections(revision)
            return RuntimeResult("", None, revision, "rolled_back")

    def events_after(self, sequence):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT sequence, type, payload FROM events WHERE session_id = ? AND sequence > ? ORDER BY sequence",
                (self.session_id, sequence),
            ).fetchall()
        return [RuntimeEvent(row["sequence"], row["type"], json.loads(row["payload"])) for row in rows]

    def graph_run_id_for_task(self, task_id):
        """Return the persisted Graph Run identity for a task, if any."""
        if not task_id:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id FROM graph_runs WHERE task_id = ? AND session_id = ?",
                (task_id, self.session_id),
            ).fetchone()
        return row["id"] if row else None

    def retry_graph_run(self, graph_run_id, idempotency_key):
        """Run a failed Graph again from current saved definitions.

        The failed run supplies only its original player input.  The new task
        snapshots the current Project, Graph, Agent, Provider and Worldbook
        stores, so no execution artifact or frozen plan from the failed run is
        reused.  The new Graph Run records ``retry_of`` for auditability.
        """
        if not isinstance(graph_run_id, str) or not graph_run_id.strip():
            raise ValueError("missing graph_run_id")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("missing idempotency_key")
        if self.generation_active():
            return RuntimeResult("", None, self.active_revision(), "generation_busy")

        previous = self.graph_run_detail(graph_run_id)
        if previous is None:
            return RuntimeResult("", None, self.active_revision(), "unknown_graph_run")
        if previous.get("status") not in {"failed", "interrupted"}:
            return RuntimeResult("", None, self.active_revision(), "graph_not_retryable")
        text = (previous.get("player_input") or "").strip()
        if not text:
            return RuntimeResult("", None, self.active_revision(), "invalid_graph_input")

        task = self._create_or_get_task(
            text,
            idempotency_key,
            graph_retry_of=graph_run_id,
        )
        return self._submit_queued_task(task)

    def node_run_detail(self, node_run_id):
        """Return the complete safe debug record for one Node Run."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM node_runs WHERE id = ? AND session_id = ?",
                (node_run_id, self.session_id),
            ).fetchone()
        if row is None:
            return None
        return self._node_run_payload(row)

    def agent_trace_detail(self, task_id: str, node_id: str):
        """Return prompt/model/output evidence for legacy ``agent_node`` traces.

        The compatibility director path predates ``node_runs`` and only emits
        ``agent_node.*`` telemetry. Its durable prompt lives in context
        manifests, while model output is carried by model-call events and the
        committed draft. Keep this read model separate from Graph Node Runs so
        both runtime shapes remain independently debuggable.
        """
        if not isinstance(task_id, str) or not task_id.strip():
            return None
        if not isinstance(node_id, str) or not node_id.strip():
            return None
        with self._connect() as connection:
            task = connection.execute(
                "SELECT id, text, status, commit_id, source_snapshot FROM tasks "
                "WHERE id = ? AND session_id = ?",
                (task_id, self.session_id),
            ).fetchone()
            if task is None:
                return None
            manifest_rows = connection.execute(
                "SELECT id, manifest_json, payload_json FROM context_manifests "
                "WHERE session_id = ? AND task_id = ? ORDER BY call_ordinal",
                (self.session_id, task_id),
            ).fetchall()
            model_rows = connection.execute(
                "SELECT call_ordinal, manifest_id, model, prompt_tokens, completion_tokens, "
                "total_tokens, stop_reason, latency_ms, cost_amount, cost_currency, cost_rate_version "
                "FROM model_calls WHERE session_id = ? AND task_id = ? ORDER BY call_ordinal",
                (self.session_id, task_id),
            ).fetchall()
            event_rows = connection.execute(
                "SELECT type, payload FROM events WHERE session_id = ? ORDER BY sequence",
                (self.session_id,),
            ).fetchall()
            commit = None
            if task["commit_id"]:
                commit = connection.execute(
                    "SELECT draft FROM commits WHERE id = ? AND session_id = ?",
                    (task["commit_id"], self.session_id),
                ).fetchone()

        manifests = []
        for row in manifest_rows:
            manifest = self._manifest_row(row)
            provenance = manifest.get("graph_provenance") or {}
            target = provenance.get("node") or {}
            if target.get("id") and target.get("id") != node_id:
                continue
            manifests.append(manifest)
        if not manifests:
            # A single-node/legacy director has no graph provenance. Returning
            # its manifests is more useful than an empty debug panel.
            manifests = [self._manifest_row(row) for row in manifest_rows]

        trace_events = []
        for row in event_rows:
            try:
                payload = json.loads(row["payload"])
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict) and payload.get("task_id") == task_id:
                trace_events.append((row["type"], payload))

        node_events = [
            payload for event_type, payload in trace_events
            if event_type in {"agent_node.started", "agent_node.finished"}
            and payload.get("node_id") == node_id
        ]
        role = next((payload.get("role") for payload in node_events if payload.get("role")), None)
        model_outputs = {}
        model_requests = {}
        for event_type, payload in trace_events:
            if event_type == "model_call.started":
                event_node_id = payload.get("agent_node_id")
                if not event_node_id or event_node_id == node_id:
                    model_requests[payload.get("call_ordinal")] = {
                        "messages": payload.get("messages") or [],
                        "tools": payload.get("tools") or [],
                    }
                continue
            if event_type != "model_call.finished":
                continue
            event_node_id = payload.get("agent_node_id")
            if event_node_id and event_node_id != node_id:
                continue
            output = payload.get("output")
            if output:
                model_outputs[payload.get("call_ordinal")] = output

        model_calls = []
        manifest_ids = {manifest.get("id") for manifest in manifests}
        for row in model_rows:
            if manifest_ids and row["manifest_id"] not in manifest_ids:
                continue
            call = dict(row)
            call["output"] = model_outputs.get(row["call_ordinal"], "")
            call.update(model_requests.get(row["call_ordinal"], {}))
            model_calls.append(call)

        prompt = next(
            (call.get("messages") for call in reversed(model_calls) if call.get("messages")),
            manifests[-1].get("payload", []) if manifests else [],
        )
        output = next((call["output"] for call in reversed(model_calls) if call.get("output")), "")
        if not output and commit:
            try:
                output = json.loads(commit["draft"]).get("content", "")
            except (TypeError, json.JSONDecodeError):
                output = ""
        if not output:
            output = "".join(
                payload.get("preview", "") for event_type, payload in trace_events
                if event_type == "narrative.preview.delta"
            )

        source_snapshot = {}
        try:
            source_snapshot = json.loads(task["source_snapshot"] or "{}")
        except (TypeError, json.JSONDecodeError):
            pass
        state = next(
            (payload.get("state") for payload in reversed(node_events) if payload.get("state")),
            task["status"],
        )
        return _redact_trace({
            "task_id": task_id,
            "node_id": node_id,
            "role": role or node_id,
            "label": f"{role or 'agent'} · {node_id}",
            "state": state,
            "input": {"player_input": task["text"]},
            "prompt": prompt,
            "manifests": manifests,
            "model_calls": model_calls,
            "output": output,
            "error": next((payload.get("error") for payload in reversed(node_events) if payload.get("error")), None),
            "detail_source": "legacy agent trace",
        })

    def graph_run_detail(self, graph_run_id):
        """Return one Graph Run and all persisted Node Run debug records."""
        with self._connect() as connection:
            graph = connection.execute(
                "SELECT * FROM graph_runs WHERE id = ? AND session_id = ?",
                (graph_run_id, self.session_id),
            ).fetchone()
            if graph is None:
                return None
            nodes = connection.execute(
                "SELECT * FROM node_runs WHERE graph_run_id = ? AND session_id = ? ORDER BY order_index",
                (graph_run_id, self.session_id),
            ).fetchall()
        payload = {
            "graph_run_id": graph["id"],
            "run_id": graph["id"],
            "task_id": graph["task_id"],
            "plan_id": graph["plan_id"],
            "graph_id": graph["graph_id"],
            "retry_of": graph["retry_of"],
            "retry_of_run_id": graph["retry_of"],
            "status": graph["status"],
            "state": graph["status"],
            "player_input": graph["player_input"],
            "plan": _load_trace_json(graph["plan_json"], {}),
            "failed_node_id": graph["failed_node_id"],
            "error": _load_trace_json(graph["error_json"], None),
            "created_at": graph["created_at"],
            "started_at": graph["started_at"],
            "finished_at": graph["finished_at"],
            "nodes": [self._node_run_payload(row) for row in nodes],
        }
        return _redact_trace(payload)

    def graph_runs_snapshot(self) -> dict[str, Any]:
        """Return the active run and latest terminal run for refresh/reconnect."""
        with self._connect() as connection:
            current = connection.execute(
                "SELECT id FROM graph_runs WHERE session_id = ? AND status = 'running' ORDER BY created_at DESC, rowid DESC LIMIT 1",
                (self.session_id,),
            ).fetchone()
            recent = connection.execute(
                "SELECT id FROM graph_runs WHERE session_id = ? AND status IN ('succeeded', 'failed', 'interrupted') ORDER BY finished_at DESC, created_at DESC, rowid DESC LIMIT 1",
                (self.session_id,),
            ).fetchone()
        return {
            "current": self.graph_run_detail(current["id"]) if current else None,
            "most_recent": self.graph_run_detail(recent["id"]) if recent else None,
        }

    def debug_replay(self, node_run_id, idempotency_key=None):
        """Execute one retained Node Run input against the current Agent config.

        Replay is deliberately outside Graph Runtime: it invokes exactly one
        Node Runner with the persisted input Artifact and read-only tool data.
        It never creates a Session task, commit, Graph Run or story projection.
        """
        if not isinstance(node_run_id, str) or not node_run_id.strip():
            raise ValueError("missing node_run_id")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("missing idempotency_key")
        source = self.node_run_detail(node_run_id)
        if source is None:
            return {"ok": False, "state": "failed", "error": {"code": "node_run_not_found"}}
        graph = self.graph_run_detail(source["graph_run_id"])
        if graph is None:
            return {"ok": False, "state": "failed", "error": {"code": "graph_run_not_found"}}

        replay_id, created = self._create_debug_replay(source, graph, idempotency_key)
        existing = self.debug_replay_detail(replay_id)
        if not created:
            return existing

        input_payload = source.get("input_artifact") or source.get("input")
        try:
            input_artifact = AgentArtifact.from_dict(input_payload) if isinstance(input_payload, dict) else None
            if input_artifact is None:
                raise ValueError("source Node Run has no frozen input Artifact")
            current_plan = self._compile_replay_plan(graph, source)
            current_node = next(
                node for node in current_plan.graph.nodes if node.node_id == source["node_id"]
            )
            old_config = source.get("effective_config") or {}
            new_config = _redact_trace(dict(current_node.agent.effective_config))
            diff = self._effective_config_diff(old_config, new_config)
            self._update_debug_replay(
                replay_id,
                new_effective_config=new_config,
                effective_config_diff=diff,
            )

            if self.graph_runtime is None:
                raise RuntimeError("Graph Runtime is unavailable for Debug Replay")
            runner = self._replay_node_runner(source.get("tool_snapshot") or {})
            result = self._run_replay_node(runner, current_node, input_artifact)
            if isinstance(result, AgentArtifact):
                result = NodeResult.succeeded(result)
            if not isinstance(result, NodeResult):
                result = NodeResult.failed("Node Runner returned an invalid Node Result")
            state = "succeeded" if result.ok else "failed"
            artifact = result.primary_artifact.to_dict() if result.primary_artifact else None
            new_output = artifact.get("content") if isinstance(artifact, dict) else None
            if new_output is not None and not isinstance(new_output, str):
                new_output = json.dumps(new_output, ensure_ascii=False)
            self._update_debug_replay(
                replay_id,
                state=state,
                new_output=new_output,
                error=None if result.ok else _redact_trace(result.error),
            )
        except Exception as exc:
            self._update_debug_replay(
                replay_id,
                state="failed",
                error={"code": "debug_replay_failed", "message": str(exc)},
            )
        self._prune_debug_replays()
        return self.debug_replay_detail(replay_id)

    def debug_replay_detail(self, replay_id):
        """Return one persisted isolated Debug Replay result."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM debug_replays WHERE id = ? AND session_id = ?",
                (replay_id, self.session_id),
            ).fetchone()
        if row is None:
            return None
        old_output = row["old_output"]
        new_output = row["new_output"]
        diff = _load_trace_json(row["effective_config_diff_json"], {})
        return _redact_trace(
            {
                "ok": row["state"] == "succeeded",
                "debug_replay_id": row["id"],
                "replay_id": row["id"],
                "id": row["id"],
                "state": row["state"],
                "status": row["state"],
                "source_node_run_id": row["source_node_run_id"],
                "source_graph_run_id": row["source_graph_run_id"],
                "node_id": row["node_id"],
                "agent_id": row["agent_id"],
                "input_artifact": _load_trace_json(row["input_artifact_json"], None),
                "frozen_input": _load_trace_json(row["input_artifact_json"], None),
                "upstream_artifacts": _load_trace_json(row["upstream_artifacts_json"], []),
                "tool_snapshot": _load_trace_json(row["tool_snapshot_json"], {}),
                "old_output": old_output,
                "old_final_output": old_output,
                "new_output": new_output,
                "new_final_output": new_output,
                "old_effective_config": _load_trace_json(row["old_effective_config_json"], {}),
                "new_effective_config": _load_trace_json(row["new_effective_config_json"], {}),
                "effective_config_diff": diff,
                "config_diff": diff,
                "error": _load_trace_json(row["error_json"], None),
                "created_at": row["created_at"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
            }
        )

    def _create_debug_replay(self, source, graph, idempotency_key):
        if idempotency_key:
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT id FROM debug_replays WHERE session_id = ? AND idempotency_key = ?",
                    (self.session_id, idempotency_key),
                ).fetchone()
            if existing:
                return existing["id"], False
        source_nodes = sorted(graph.get("nodes") or [], key=lambda item: item.get("order", 0))
        source_order = source.get("order", 0)
        upstream = [
            node.get("artifact")
            for node in source_nodes
            if node.get("order", 0) < source_order and isinstance(node.get("artifact"), dict)
        ]
        old_output = source.get("final_output")
        if old_output is None:
            artifact = source.get("artifact")
            old_output = artifact.get("content") if isinstance(artifact, dict) else source.get("streamed_output")
        now = int(time.time())
        replay_id = self._id()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "INSERT INTO debug_replays (id, session_id, idempotency_key, source_node_run_id, source_graph_run_id, node_id, agent_id, state, input_artifact_json, upstream_artifacts_json, tool_snapshot_json, old_output, new_output, old_effective_config_json, new_effective_config_json, effective_config_diff_json, created_at, started_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?, NULL, ?, '{}', '{}', ?, ?)",
                    (
                        replay_id,
                        self.session_id,
                        idempotency_key,
                        source["node_run_id"],
                        source["graph_run_id"],
                        source["node_id"],
                        source["agent_id"],
                        _trace_json(source.get("input_artifact") or source.get("input")),
                        _trace_json(upstream),
                        _trace_json(source.get("tool_snapshot") or {}),
                        _redact_trace(old_output),
                        _trace_json(source.get("effective_config") or {}),
                        now,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                if not idempotency_key:
                    raise
                existing = connection.execute(
                    "SELECT id FROM debug_replays WHERE session_id = ? AND idempotency_key = ?",
                    (self.session_id, idempotency_key),
                ).fetchone()
                if existing:
                    return existing["id"], False
                raise
        return replay_id, True

    def _update_debug_replay(
        self,
        replay_id,
        *,
        state=None,
        new_output=None,
        new_effective_config=None,
        effective_config_diff=None,
        error=None,
    ):
        updates = []
        values = []
        if state is not None:
            updates.append("state = ?")
            values.append(state)
        if new_output is not None:
            updates.append("new_output = ?")
            values.append(_redact_trace(new_output))
        if new_effective_config is not None:
            updates.append("new_effective_config_json = ?")
            values.append(_trace_json(new_effective_config))
        if effective_config_diff is not None:
            updates.append("effective_config_diff_json = ?")
            values.append(_trace_json(effective_config_diff))
        if error is not None or state in {"succeeded", "failed"}:
            updates.append("error_json = ?")
            values.append(_trace_json(error))
        if state in {"succeeded", "failed"}:
            updates.append("finished_at = ?")
            values.append(int(time.time()))
        if not updates:
            return
        values.extend([replay_id, self.session_id])
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                f"UPDATE debug_replays SET {', '.join(updates)} WHERE id = ? AND session_id = ? AND state = 'running'",
                values,
            )

    def _compile_replay_plan(self, graph, source):
        plan_data = graph.get("plan") or {}
        old_graph = plan_data.get("graph") or {}
        old_node = next(
            (node for node in old_graph.get("nodes") or [] if node.get("node_id") == source["node_id"]),
            None,
        )
        if old_node is None:
            raise ValueError("source Node Run is absent from its frozen Graph plan")
        agent_id = source.get("agent_id") or old_node.get("agent_id")
        compiler = self.execution_plan_compiler
        agent_store = getattr(compiler, "agent_store", None) if compiler is not None else None
        if compiler is None or not callable(getattr(agent_store, "get_agent", None)):
            raise RuntimeError("current Agent Definition compiler is unavailable for Debug Replay")
        try:
            current_agent = agent_store.get_agent(agent_id)
        except Exception as exc:
            raise RuntimeError(f"current Agent Definition {agent_id!r} is unavailable") from exc
        if not isinstance(current_agent, dict):
            raise RuntimeError(f"current Agent Definition {agent_id!r} is invalid")
        current_agent = copy.deepcopy(current_agent)
        node = copy.deepcopy(old_node)
        node["agent_id"] = agent_id
        replay_graph = {
            "id": old_graph.get("graph_id") or old_graph.get("id") or graph.get("graph_id"),
            "name": old_graph.get("name") or graph.get("graph_id") or "Graph Replay",
            "nodes": [node],
            "output_node_id": node.get("node_id") or source["node_id"],
        }
        return compiler.compile(
            project=copy.deepcopy(plan_data.get("project") or {}),
            graph=replay_graph,
            agents={agent_id: current_agent},
            worldbooks=copy.deepcopy(plan_data.get("worldbooks") or []),
            player_input=plan_data.get("player_input") or graph.get("player_input") or "",
        )

    def _replay_node_runner(self, tool_snapshot):
        runner = self.graph_runtime.node_runner
        read_only_tools = self._read_only_tool_handler(tool_snapshot)
        if hasattr(runner, "tool_handler"):
            isolated = copy.copy(runner)
            isolated.tool_handler = read_only_tools
            return isolated
        return runner

    @staticmethod
    def _run_replay_node(runner, node, input_artifact):
        run = runner.run
        try:
            parameters = inspect.signature(run).parameters
        except (TypeError, ValueError):
            parameters = {}
        if "observer" in parameters:
            return run(node, input_artifact, observer=None)
        return run(node, input_artifact)

    @staticmethod
    def _read_only_tool_handler(snapshot):
        snapshot = copy.deepcopy(snapshot or {})

        def handle(name, args):
            normalized = str(name or "").casefold()
            if normalized in {"get_recent_memory", "recent_memory", "read_memory"}:
                memory = snapshot.get("recent_memory", "")
                max_chars = (args or {}).get("max_chars", 3000) if isinstance(args, dict) else 3000
                if not isinstance(max_chars, int) or max_chars <= 0:
                    max_chars = 3000
                truncated = len(memory) > max_chars
                return {
                    "memory": memory[-max_chars:] if truncated else memory,
                    "truncated": truncated,
                }
            if normalized in {"get_session_snapshot", "session_snapshot", "get_state", "read_state"}:
                return {
                    "current_state": copy.deepcopy(snapshot.get("current_state", {})),
                    "recent_turns": copy.deepcopy(snapshot.get("recent_turns", [])),
                }
            if normalized in {"load_worldbook_entry", "load_worldbook", "read_worldbook", "get_worldbook"}:
                title = (args or {}).get("title") if isinstance(args, dict) else None
                books = snapshot.get("worldbooks") or []
                for book in books:
                    for entry in book.get("entries", []) if isinstance(book, dict) else []:
                        if not title or entry.get("title") == title:
                            return {
                                "title": entry.get("title"),
                                "content": entry.get("content", ""),
                                "content_hash": entry.get("content_hash"),
                            }
                return {"error": "worldbook_entry_not_found", "title": title}
            return {"error": "replay_tool_read_only", "tool": str(name)}

        return handle

    @staticmethod
    def _effective_config_diff(old, new):
        old = old if isinstance(old, dict) else {}
        new = new if isinstance(new, dict) else {}
        diff = {}
        for key in sorted(set(old) | set(new)):
            before = old.get(key)
            after = new.get(key)
            if before == after:
                continue
            diff[key] = {"old": copy.deepcopy(before), "new": copy.deepcopy(after)}
        return diff

    def _node_run_payload(self, row) -> dict[str, Any]:
        artifact = _load_trace_json(row["artifact_json"], None)
        return _redact_trace(
            {
                "node_run_id": row["id"],
                "id": row["id"],
                "graph_run_id": row["graph_run_id"],
                "run_id": row["graph_run_id"],
                "task_id": row["task_id"],
                "node_id": row["node_id"],
                "agent_id": row["agent_id"],
                "label": row["label"],
                "order": row["order_index"],
                "state": row["state"],
                "status": row["state"],
                "input": _load_trace_json(row["input_artifact_json"], None),
                "input_artifact": _load_trace_json(row["input_artifact_json"], None),
                "prompt": _load_trace_json(row["prompt_json"], []),
                "prompt_provenance": _load_trace_json(row["prompt_provenance_json"], []),
                "effective_config": _load_trace_json(row["effective_config_json"], {}),
                "tool_snapshot": _load_trace_json(row["tool_snapshot_json"], {}),
                "model_calls": _load_trace_json(row["model_calls_json"], []),
                "tool_calls": _load_trace_json(row["tool_calls_json"], []),
                "streamed_output": row["streamed_output"] or "",
                "final_output": row["final_output"],
                "artifact": artifact,
                "error": _load_trace_json(row["error_json"], None),
                "diagnostics_ref": row["diagnostics_ref"],
                "started_at": row["started_at"],
                "finished_at": row["finished_at"],
            }
        )

    def _prune_graph_runs(self) -> None:
        """Keep only the active run and latest terminal trace for this Session."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            running = connection.execute(
                "SELECT id FROM graph_runs WHERE session_id = ? AND status = 'running'",
                (self.session_id,),
            ).fetchall()
            terminal = connection.execute(
                "SELECT id FROM graph_runs WHERE session_id = ? AND status IN ('succeeded', 'failed', 'interrupted') ORDER BY finished_at DESC, created_at DESC, rowid DESC LIMIT 1",
                (self.session_id,),
            ).fetchone()
            keep = {row["id"] for row in running if row["id"]}
            if terminal:
                keep.add(terminal["id"])
            if keep:
                placeholders = ",".join("?" for _ in keep)
                params = [self.session_id, *keep]
                old = connection.execute(
                    f"SELECT id FROM graph_runs WHERE session_id = ? AND id NOT IN ({placeholders})",
                    params,
                ).fetchall()
                for row in old:
                    connection.execute("DELETE FROM node_runs WHERE graph_run_id = ? AND session_id = ?", (row["id"], self.session_id))
                    connection.execute("DELETE FROM graph_runs WHERE id = ? AND session_id = ?", (row["id"], self.session_id))
        self._prune_debug_replays()

    def _prune_debug_replays(self) -> None:
        """Keep active and latest terminal Debug Replay traces only."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            running = connection.execute(
                "SELECT id FROM debug_replays WHERE session_id = ? AND state = 'running'",
                (self.session_id,),
            ).fetchall()
            terminal = connection.execute(
                "SELECT id FROM debug_replays WHERE session_id = ? AND state IN ('succeeded', 'failed', 'interrupted') "
                "ORDER BY finished_at DESC, created_at DESC, rowid DESC LIMIT 1",
                (self.session_id,),
            ).fetchone()
            keep = {row["id"] for row in running if row["id"]}
            if terminal:
                keep.add(terminal["id"])
            if keep:
                placeholders = ",".join("?" for _ in keep)
                connection.execute(
                    f"DELETE FROM debug_replays WHERE session_id = ? AND id NOT IN ({placeholders})",
                    [self.session_id, *keep],
                )
            else:
                connection.execute(
                    "DELETE FROM debug_replays WHERE session_id = ?",
                    (self.session_id,),
                )

    def projection_checkpoint(self, commit_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state FROM projection_checkpoints WHERE commit_id = ?", (commit_id,)
            ).fetchone()
        return row["state"] if row else None

    def manifest_for_task(self, task_id, call_ordinal):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, manifest_json, payload_json FROM context_manifests "
                "WHERE session_id = ? AND task_id = ? AND call_ordinal = ?",
                (self.session_id, task_id, call_ordinal),
            ).fetchone()
        return self._manifest_row(row)

    def manifests_for_task(self, task_id):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT id, manifest_json, payload_json FROM context_manifests "
                "WHERE session_id = ? AND task_id = ? ORDER BY call_ordinal",
                (self.session_id, task_id),
            ).fetchall()
        return [self._manifest_row(row) for row in rows]

    def replay_manifest(self, manifest_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json, payload_hash FROM context_manifests WHERE id = ? AND session_id = ?",
                (manifest_id, self.session_id),
            ).fetchone()
        if not row:
            return None
        payload = json.loads(row["payload_json"])
        if self._hash_bytes(self._canonical(payload).encode("utf-8")) != row["payload_hash"]:
            raise RuntimeError("persisted manifest payload hash mismatch")
        return payload

    def load_worldbook_for_task(self, task_id, title, reason):
        with self._lock:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                task = connection.execute(
                    "SELECT base_revision, source_snapshot FROM tasks WHERE id = ? AND session_id = ?", (task_id, self.session_id)
                ).fetchone()
                if not task:
                    raise ValueError("unknown task")
                call_ordinal = connection.execute(
                    "SELECT COUNT(*) AS count FROM context_manifests WHERE session_id = ? AND task_id = ?",
                    (self.session_id, task_id),
                ).fetchone()["count"]
                load_count = connection.execute(
                    "SELECT COUNT(*) AS count FROM worldbook_loads WHERE session_id = ? AND task_id = ? AND call_ordinal = ?",
                    (self.session_id, task_id, call_ordinal),
                ).fetchone()["count"]
                if load_count >= self.manifest_policy.max_worldbook_loads:
                    raise ValueError("worldbook load limit exceeded")
                snapshot = json.loads(task["source_snapshot"])
                entry = load_worldbook_entry_from_texts(
                    self._canonical(snapshot["worldbook_catalog"]),
                    snapshot.get("worldbook_reference", ""),
                    snapshot.get("worldbook_user", ""),
                    title,
                )
                connection.execute(
                    "INSERT INTO worldbook_loads "
                    "(id, session_id, task_id, base_revision, call_ordinal, title, catalog_hash, reference_hash, content_hash, content, reason) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._id(), self.session_id, task_id, task["base_revision"], call_ordinal, entry.title,
                        entry.catalog_hash, entry.reference_hash, entry.content_hash, entry.content, reason,
                    ),
                )
                self._event(connection, "worldbook.loaded", {"task_id": task_id, "title": entry.title, "content_hash": entry.content_hash})
        return entry

    def _create_or_get_task(self, text, idempotency_key, *, graph_retry_of: str | None = None):
        # Snapshot reads open their own connections; never nest them under
        # BEGIN IMMEDIATE or the process deadlocks against itself. Loop until
        # the base we snapshotted still matches the head at insert time.
        while True:
            with self._connect() as connection:
                task = self._task_for_key(connection, idempotency_key)
                if task:
                    return task
                base_revision = self._active_revision(connection)
            source_snapshot = self._source_snapshot(base_revision, player_input=text)
            if graph_retry_of:
                source_snapshot["graph_retry_of"] = graph_retry_of
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                task = self._task_for_key(connection, idempotency_key)
                if task:
                    return task
                current_base = self._active_revision(connection)
                if current_base != base_revision:
                    continue
                task_id = self._id()
                connection.execute(
                    "INSERT INTO tasks (id, session_id, idempotency_key, text, status, revision, base_revision, source_snapshot, queue_sequence) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task_id,
                        self.session_id,
                        self._stored_idempotency_key(idempotency_key),
                        text,
                        "queued",
                        0,
                        base_revision,
                        self._canonical(source_snapshot),
                        connection.execute(
                            "SELECT COALESCE(MAX(queue_sequence), 0) + 1 AS next_sequence "
                            "FROM tasks WHERE session_id = ?",
                            (self.session_id,),
                        ).fetchone()["next_sequence"],
                    ),
                )
                if graph_retry_of:
                    self._event(
                        connection,
                        "graph.run.retry_requested",
                        {
                            "task_id": task_id,
                            "retry_of": graph_retry_of,
                            "text": text,
                            "base_revision": base_revision,
                        },
                    )
                else:
                    self._event(connection, "player_message.submitted", {"task_id": task_id, "text": text})
                self._event(connection, "task.queued", {"task_id": task_id, "base_revision": base_revision})
                return self._task_for_key(connection, idempotency_key)

    def compile_follow_up_manifest(self, task_id, player_input):
        with self._lock:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                task = connection.execute(
                    "SELECT id, text, base_revision, source_snapshot FROM tasks WHERE id = ? AND session_id = ?",
                    (task_id, self.session_id),
                ).fetchone()
                if not task:
                    raise ValueError("unknown task")
                call_ordinal = connection.execute(
                    "SELECT COUNT(*) AS count FROM context_manifests WHERE session_id = ? AND task_id = ?",
                    (self.session_id, task_id),
                ).fetchone()["count"]
                snapshot = json.loads(task["source_snapshot"])
                loads = self._worldbook_loads_in_connection(connection, task_id, call_ordinal)
                policy = self._policy_for_snapshot(snapshot)
                compiled = compile_context(ContextCompileRequest(
                    session_id=self.session_id,
                    task_id=task_id,
                    base_revision=task["base_revision"],
                    player_input=player_input,
                    snapshot=snapshot,
                    policy=policy,
                    call_ordinal=call_ordinal,
                    worldbook_loads=tuple(loads),
                ))
                return self._persist_manifest_in_connection(connection, task, compiled)

    def compile_sequential_handoff_manifest(self, task_id, parent_compiled, source_node, target_node, text):
        """Compile and persist the next graph-node context from a prior manifest.

        Sequential nodes may only receive an immutable, persisted handoff. This
        prevents a payload/hash pair from being reused after the handoff changes.
        """
        with self._lock:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                task = connection.execute(
                    "SELECT id, text, base_revision, source_snapshot FROM tasks WHERE id = ? AND session_id = ?",
                    (task_id, self.session_id),
                ).fetchone()
                if not task:
                    raise ValueError("unknown task")
                parent_manifest = parent_compiled.manifest
                parent_id = parent_manifest.get("id")
                if not parent_id:
                    raise ValueError("sequential handoff requires a persisted parent manifest")
                call_ordinal = connection.execute(
                    "SELECT COUNT(*) AS count FROM context_manifests WHERE session_id = ? AND task_id = ?",
                    (self.session_id, task_id),
                ).fetchone()["count"]
                snapshot = json.loads(task["source_snapshot"])
                loads = self._worldbook_loads_in_connection(connection, task_id, call_ordinal)
                compiled = compile_sequential_handoff_context(
                    ContextCompileRequest(
                        session_id=self.session_id,
                        task_id=task_id,
                        base_revision=task["base_revision"],
                        player_input=task["text"],
                        snapshot=snapshot,
                        policy=self._policy_for_snapshot(snapshot),
                        call_ordinal=call_ordinal,
                        worldbook_loads=tuple(loads),
                    ),
                    parent_manifest,
                    source_node,
                    target_node,
                    text,
                )
                return self._persist_manifest_in_connection(connection, task, compiled)

    def _worldbook_loads(self, task_id, before_call_ordinal):
        with self._connect() as connection:
            return self._worldbook_loads_in_connection(connection, task_id, before_call_ordinal)

    def _worldbook_loads_in_connection(self, connection, task_id, before_call_ordinal):
        rows = connection.execute(
            "SELECT title, catalog_hash, reference_hash, content_hash, content, reason FROM worldbook_loads "
            "WHERE session_id = ? AND task_id = ? AND call_ordinal <= ? ORDER BY call_ordinal, title",
            (self.session_id, task_id, before_call_ordinal),
        ).fetchall()
        return [dict(row) for row in rows]

    def _persist_manifest(self, task, compiled):
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._lease_is_authoritative(connection, task):
                return None
            return self._persist_manifest_in_connection(connection, task, compiled)

    def _persist_manifest_in_connection(self, connection, task, compiled):
        row = connection.execute(
            "SELECT id, manifest_json, payload_json FROM context_manifests WHERE session_id = ? AND task_id = ? AND call_ordinal = ?",
            (self.session_id, task["id"], compiled.manifest["call_ordinal"]),
        ).fetchone()
        if row:
            return self._compiled_from_manifest(self._manifest_row(row))
        manifest = dict(compiled.manifest)
        manifest_id = self._id()
        manifest["id"] = manifest_id
        connection.execute(
            "INSERT INTO context_manifests "
            "(id, session_id, task_id, base_revision, call_ordinal, manifest_json, payload_json, payload_hash, stable_payload_hash, policy_version, token_budget, estimated_tokens) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                manifest_id, self.session_id, task["id"], task["base_revision"], manifest["call_ordinal"],
                self._canonical(manifest), self._canonical(compiled.payload), compiled.payload_hash,
                compiled.stable_payload_hash, manifest["policy_version"], manifest["token_budget"],
                manifest["estimated_tokens"],
            ),
        )
        self._event(connection, "context.compiled", {"task_id": task["id"], "manifest_id": manifest_id, "base_revision": task["base_revision"], "payload_hash": compiled.payload_hash})
        manifest["payload"] = compiled.payload
        return type(compiled)(compiled.payload, manifest, compiled.payload_hash, compiled.stable_payload_hash)

    def _compile_and_persist(self, task):
        existing = self.manifest_for_task(task["id"], 0)
        if existing:
            return self._compiled_from_manifest(existing)
        if not task["source_snapshot"]:
            raise RuntimeError("task has no trustworthy context snapshot")
        snapshot = json.loads(task["source_snapshot"])
        if not snapshot:
            raise RuntimeError("task has no trustworthy context snapshot")
        loads = self._worldbook_loads(task["id"], 0)
        request = ContextCompileRequest(
            session_id=self.session_id,
            task_id=task["id"],
            base_revision=task["base_revision"],
            player_input=task["text"],
            snapshot=snapshot,
            policy=self._policy_for_snapshot(snapshot),
            worldbook_loads=tuple(loads),
        )
        compiled = compile_context(request)
        compiled = self._persist_manifest(task, compiled)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # A stale worker may still hold an in-memory task after restart. The
            # session lease/fence is the authority for every generation mutation.
            if not self._lease_is_authoritative(connection, task):
                return None
            if "lease_fence" not in task.keys():
                cur = connection.execute(
                    "UPDATE tasks SET status = ? WHERE id = ? AND status = ?",
                    ("running", task["id"], "queued"),
                )
            else:
                cur = connection.execute(
                    "UPDATE tasks SET status = ? WHERE id = ? AND status = ? AND lease_fence = ?",
                    ("running", task["id"], "leased", task["lease_fence"]),
                )
            if cur.rowcount:
                payload = {"task_id": task["id"]}
                if "lease_fence" in task.keys():
                    payload["lease_fence"] = task["lease_fence"]
                self._event(connection, "task.running", payload)
        return compiled

    def _graph_turn_executor_for_task(self, task, signal=None):
        snapshot = json.loads(task["source_snapshot"]) if task["source_snapshot"] else {}
        plan_data = snapshot.get("execution_plan")
        if plan_data and self.graph_runtime is not None:
            plan = ExecutionPlan.from_dict(plan_data)
            observer = GraphRunObserver(
                self,
                task,
                plan,
                retry_of=snapshot.get("graph_retry_of"),
            )
            framework = AgentFrameworkExecutor(self.graph_runtime, plan, observer=observer)
            return GraphTurnCommitExecutor(
                framework,
                execution_context=NodeExecutionContext(
                    tool_registry=self._graph_tool_registry(task, plan),
                    abort_signal=signal,
                ),
            )
        return None

    def _commit_draft(self, task, draft):
        stale = False
        commit_id = None
        revision = None
        validation_error = None
        # Preload base state OUTSIDE the write txn — _state_at_revision opens its
        # own connection and must not nest under BEGIN IMMEDIATE.
        base_state = self._state_at_revision(task["base_revision"])
        projected_state = self._projected_state_from_base(base_state, draft)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if not self._lease_is_authoritative(connection, task):
                row = connection.execute(
                    "SELECT id, commit_id, revision, status FROM tasks WHERE id = ?", (task["id"],)
                ).fetchone()
                return self._result(row) if row else RuntimeResult(task["id"], None, 0, "abandoned_running")
            # Idempotent: a concurrent duplicate for the same task may already
            # have committed while we were outside the session lock.
            existing = connection.execute(
                "SELECT id, revision, status, commit_id FROM tasks WHERE id = ?",
                (task["id"],),
            ).fetchone()
            if existing and existing["commit_id"]:
                return RuntimeResult(
                    task["id"], existing["commit_id"], existing["revision"], "projection_pending"
                )
            if self._active_revision(connection) != task["base_revision"]:
                stale = True
            else:
                validation_error = self._validate_draft_before_commit(
                    connection, task, draft, base_state=base_state
                )
                if validation_error is None:
                    commit_id, revision = self._perform_commit_in_connection(
                        connection, task, draft, projected_state=projected_state
                    )
        if stale:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                # Don't clobber a task that committed between our check and now.
                connection.execute(
                    "UPDATE tasks SET status = ? WHERE id = ? AND commit_id IS NULL",
                    ("stale_revision", task["id"]),
                )
                row = connection.execute(
                    "SELECT commit_id, revision, status FROM tasks WHERE id = ?", (task["id"],)
                ).fetchone()
                if row and row["commit_id"]:
                    return RuntimeResult(task["id"], row["commit_id"], row["revision"], "projection_pending")
                self._event(connection, "task.stale_revision", {"task_id": task["id"]})
            raise RuntimeError("stale revision")
        if validation_error is not None:
            raise RuntimeError(validation_error)
        return RuntimeResult(task["id"], commit_id, revision, "projection_pending")

    def _fail_graph_run(self, task, error: GraphExecutionError):
        """Persist fail-fast Graph semantics without entering draft commit."""
        graph_result = getattr(error, "result", None)
        payload = {
            "task_id": task["id"],
            "plan_id": getattr(graph_result, "plan_id", None),
            "failed_node_id": getattr(graph_result, "failed_node_id", None),
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT commit_id, revision, status FROM tasks WHERE id = ?",
                (task["id"],),
            ).fetchone()
            if row and not row["commit_id"] and self._lease_is_authoritative(connection, task):
                connection.execute(
                    "UPDATE tasks SET status = ? WHERE id = ? AND commit_id IS NULL",
                    ("failed_terminal", task["id"]),
                )
                self._event(connection, "graph.run.failed", payload)
                self._event(connection, "task.failed_terminal", payload)
                return RuntimeResult(task["id"], None, row["revision"] or task["base_revision"], "failed_terminal")
            if row:
                return RuntimeResult(task["id"], row["commit_id"], row["revision"], row["status"])
        return RuntimeResult(task["id"], None, task["base_revision"], "failed_terminal")

    def _fail_graph_configuration(self, task):
        """Fail a leased task when no active Studio Graph was frozen into it."""
        payload = {
            "task_id": task["id"],
            "error": {"code": "active_graph_required", "message": "select an active Studio Graph before starting a run"},
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT commit_id, revision, status FROM tasks WHERE id = ?",
                (task["id"],),
            ).fetchone()
            if row and not row["commit_id"] and self._lease_is_authoritative(connection, task):
                connection.execute(
                    "UPDATE tasks SET status = ? WHERE id = ? AND commit_id IS NULL",
                    ("failed_terminal", task["id"]),
                )
                self._event(connection, "graph.run.failed", payload)
                self._event(connection, "task.failed_terminal", payload)
                return RuntimeResult(task["id"], None, row["revision"] or task["base_revision"], "failed_terminal")
            if row:
                return RuntimeResult(task["id"], row["commit_id"], row["revision"], row["status"])
        return RuntimeResult(task["id"], None, task["base_revision"], "failed_terminal")

    def _validate_draft_before_commit(self, connection, task, draft, base_state=None):
        verdict = self.quality_gate.validate(
            draft,
            self._quality_context(task),
        )
        if not verdict.ok:
            details = (verdict.metrics or {}) | {"reasons": list(verdict.reasons)}
            return self._reject_precommit(connection, task, "quality_gate_failed", details=details)

        if base_state is None:
            base_state = self._state_at_revision(task["base_revision"])
        source = draft.mvu_commands if draft.mvu_commands else draft.content
        commands = extract_commands(source)
        if not commands:
            return None
        schema = generate_schema(base_state, strict_template=True)
        for command in commands:
            ok, reason = validate_command_strict(command, schema)
            if not ok:
                return self._reject_precommit(
                    connection,
                    task,
                    "mvu_validation_failed",
                    details={"reason": reason, "command": command.full_match or repr(command.args)},
                )
        try:
            execute_commands(base_state, commands)
        except Exception as exc:
            return self._reject_precommit(
                connection,
                task,
                "mvu_validation_failed",
                details={"reason": str(exc)},
            )
        return None

    def _quality_context(self, task):
        snapshot = json.loads(task["source_snapshot"]) if task["source_snapshot"] else {}
        settings = snapshot.get("settings") if isinstance(snapshot, dict) else {}
        return QualityContext(
            settings=settings or {},
            task_id=task["id"],
            base_revision=task["base_revision"],
        )

    def _reject_precommit(self, connection, task, code, details=None):
        row = connection.execute(
            "SELECT validation_failures, validation_exhausted FROM tasks WHERE id = ?",
            (task["id"],),
        ).fetchone()
        failures = ((row["validation_failures"] if row else 0) or 0) + 1
        exhausted = True
        terminal_code = code
        if exhausted and code == "quality_gate_failed":
            terminal_code = "quality_exhausted"
        connection.execute(
            "UPDATE tasks SET status = ?, validation_failures = ?, validation_exhausted = ? WHERE id = ?",
            (terminal_code, failures, 1 if exhausted else 0, task["id"]),
        )
        payload = {"task_id": task["id"], "attempt": failures}
        if details:
            payload.update(details)
        self._event(connection, f"task.{terminal_code}", payload)
        return terminal_code

    def _perform_commit_in_connection(self, connection, task, draft, projected_state=None):
        """Insert commit + state snapshot + advance revision + emit turn.committed.

        Caller holds ``BEGIN IMMEDIATE`` and has verified revision freshness.
        Revisions are globally monotonic (``max(revision)+1``), never renumbered.
        ``parent_revision`` is the task's frozen ``base_revision``.
        ``projected_state`` must be precomputed outside the write txn (avoids
        nested connections via ``_state_at_revision``).
        Returns ``(commit_id, revision)``.
        """
        parent_revision = task["base_revision"] if task["base_revision"] is not None else 0
        # Globally unique monotonic revision — not active+1, so branches after
        # rollback/reroll never collide with superseded siblings.
        row = connection.execute(
            "SELECT COALESCE(MAX(revision), 0) AS max_rev FROM commits WHERE session_id = ?",
            (self.session_id,),
        ).fetchone()
        revision = int(row["max_rev"]) + 1
        commit_id = self._id()
        if projected_state is None:
            projected_state = self._projected_state(task["base_revision"], draft)
        connection.execute(
            "INSERT INTO commits (id, session_id, revision, task_id, draft, parent_revision) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (commit_id, self.session_id, revision, task["id"], draft.to_json(), parent_revision),
        )
        connection.execute(
            "INSERT INTO projection_checkpoints (commit_id, state) VALUES (?, ?)",
            (commit_id, "pending"),
        )
        connection.execute(
            "INSERT INTO state_snapshots (session_id, revision, state_json) VALUES (?, ?, ?)",
            (self.session_id, revision, self._canonical(projected_state)),
        )
        connection.execute(
            "UPDATE sessions SET active_revision = ? WHERE id = ?",
            (revision, self.session_id),
        )
        connection.execute(
            "UPDATE tasks SET status = ?, commit_id = ?, revision = ? WHERE id = ?",
            ("projection_pending", commit_id, revision, task["id"]),
        )
        self._event(
            connection,
            "turn.committed",
            {
                "task_id": task["id"],
                "commit_id": commit_id,
                "revision": revision,
                "parent_revision": parent_revision,
            },
        )
        return commit_id, revision

    def _emit_tool_started(self, task_id, name, args_hash, redacted_args):
        with self._connect() as connection:
            self._event(
                connection,
                "tool_run.started",
                {
                    "task_id": task_id,
                    "tool": name,
                    "args_hash": args_hash,
                    "args": redacted_args,
                },
            )

    def _emit_tool_finished(self, task_id, name, args_hash, redacted_args, ok, error, duration):
        with self._connect() as connection:
            self._event(
                connection,
                "tool_run.finished",
                {
                    "task_id": task_id,
                    "tool": name,
                    "args_hash": args_hash,
                    "ok": bool(ok),
                    "error": error,
                    "duration_ms": int(duration * 1000),
                },
            )

    def model_calls_for_task(self, task_id):
        """Public read accessor for model-call telemetry (used by contract tests)."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT call_ordinal, manifest_id, model, prompt_tokens, completion_tokens, "
                "total_tokens, stop_reason, latency_ms, cost_amount, cost_currency, cost_rate_version "
                "FROM model_calls WHERE session_id = ? AND task_id = ? ORDER BY call_ordinal",
                (self.session_id, task_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def _project(self, result):
        if result.commit_id is None and result.status == "succeeded":
            return result
        if result.commit_id is None:
            return result
        with self._lock:
            with self._connect() as connection:
                checkpoint = connection.execute(
                    "SELECT state, applied_marker FROM projection_checkpoints WHERE commit_id = ?",
                    (result.commit_id,),
                ).fetchone()
                if checkpoint is None:
                    return result
                if checkpoint["state"] == "applied" or checkpoint["applied_marker"] == result.commit_id:
                    # Already projected — just ensure task status is terminal.
                    # Avoid BEGIN IMMEDIATE on a connection that already ran a
                    # SELECT (implicit read txn); use a fresh write connection.
                    pass
            if checkpoint["state"] == "applied" or checkpoint["applied_marker"] == result.commit_id:
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "UPDATE projection_checkpoints SET state = ?, applied_marker = ? WHERE commit_id = ?",
                        ("applied", result.commit_id, result.commit_id),
                    )
                    connection.execute("UPDATE tasks SET status = ? WHERE id = ?", ("succeeded", result.task_id))
                return RuntimeResult(result.task_id, result.commit_id, result.revision, "succeeded")
            # Rebuild compatibility files from the active parent-chain only.
            # Append-only projection would leave superseded branch tips in chat_log
            # after reroll/rollback; lineage rebuild keeps one assistant turn per
            # active position while commits remain auditable in SQLite.
            try:
                self._rebuild_active_projections(result.revision)
            except Exception:
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE tasks SET status = ? WHERE id = ?",
                        ("projection_pending", result.task_id),
                    )
                raise
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE projection_checkpoints SET state = ?, applied_marker = ? WHERE commit_id = ?",
                    ("applied", result.commit_id, result.commit_id),
                )
                connection.execute("UPDATE tasks SET status = ? WHERE id = ?", ("succeeded", result.task_id))
                self._event(connection, "task.succeeded", {"task_id": result.task_id, "commit_id": result.commit_id})
            return RuntimeResult(result.task_id, result.commit_id, result.revision, "succeeded")

    def _source_snapshot(self, base_revision, *, player_input=""):
        memory = self.card_folder / "memory"
        initvar_path = self.card_folder / ".initvar.json"
        card_data_path = self.card_folder / ".card_data.json"
        catalog_path = memory / ".worldbook_index.json"
        reference_path = memory / "reference.md"
        user_path = memory / "user.md"
        structure_path = memory / ".card_structure.json"
        project_path = memory / "project.md"
        initvar = self._read_json(initvar_path, {})
        runtime_turns = self._source_recent_turns(base_revision)
        current_state = self._state_at_revision(base_revision)
        card_facts = self._read_json(card_data_path, {})
        card_structure = self._read_json(structure_path, {})
        recent_memory = self._recent_memory(project_path)
        worldbooks = self._worldbook_snapshot(catalog_path, reference_path, user_path)
        settings = self.session_settings
        execution_plan = None
        if self.execution_plan_compiler is not None:
            execution_plan = self.execution_plan_compiler.compile(
                project_id=self.project_id,
                player_input=player_input,
                context={
                    "project_id": self.project_id,
                    "player_input": player_input,
                    "card_facts": card_facts,
                    "settings": settings,
                    "worldbook_catalog": worldbooks["worldbook_catalog"],
                    "card_structure": card_structure,
                    "initvar": initvar,
                    "current_state": current_state,
                    "recent_memory": recent_memory,
                    "recent_turns": runtime_turns,
                },
                graph_id=self.execution_graph_id,
            )
        return {
            "card_facts": card_facts,
            "settings": settings,
            "worldbook_catalog": worldbooks["worldbook_catalog"],
            "worldbook_reference": worldbooks["worldbook_reference"],
            "worldbook_user": worldbooks["worldbook_user"],
            "card_structure": card_structure,
            "initvar": initvar,
            "current_state": current_state,
            "recent_memory": recent_memory,
            "recent_turns": runtime_turns,
            "execution_plan": execution_plan.to_dict() if execution_plan is not None else None,
            "sources": {
                "card_facts": self._file_source(card_data_path),
                "settings": {"id": "session_settings", "version": self._hash_bytes(self._canonical(settings).encode("utf-8"))},
                "worldbook_catalog": worldbooks["source"],
                "card_structure": self._file_source(structure_path),
                "initvar": self._file_source(initvar_path),
                "current_state": {"id": "runtime_state", "version": str(base_revision)},
                "recent_memory": self._file_source(project_path),
                "recent_turns": {"id": "active_lineage", "version": str(base_revision)},
            },
        }

    def _graph_tool_snapshot(self, plan: ExecutionPlan, task) -> dict[str, Any]:
        """Freeze the read-only data exposed to a Graph node's tools."""
        try:
            source = json.loads(task["source_snapshot"]) if task is not None and task["source_snapshot"] else {}
        except (TypeError, json.JSONDecodeError):
            source = {}
        return _redact_trace(
            {
                "project": plan.project,
                "worldbooks": list(plan.worldbooks),
                "card_facts": source.get("card_facts", {}),
                "worldbook_catalog": source.get("worldbook_catalog", []),
                "worldbook_reference": source.get("worldbook_reference", ""),
                "worldbook_user": source.get("worldbook_user", ""),
                "current_state": source.get("current_state", {}),
                "recent_memory": source.get("recent_memory", ""),
                "recent_turns": source.get("recent_turns", []),
            }
        )

    def _graph_tool_registry(self, task, plan) -> ToolRegistry:
        "Build one read-only registry over the immutable Graph snapshot."
        return ToolRegistry(
            self,
            task,
            self.manifest_policy,
            snapshot=self._graph_tool_snapshot(plan, task),
        )

    def _worldbook_snapshot(self, catalog_path, reference_path, user_path):
        if self.worldbook_snapshot_provider is not None:
            snapshot = self.worldbook_snapshot_provider(self.project_id)
            if snapshot is not None:
                return snapshot
        return {
            "worldbook_catalog": self._read_json(catalog_path, []),
            "worldbook_reference": self._read_text(reference_path),
            "worldbook_user": self._read_text(user_path),
            "source": self._file_source(catalog_path),
        }

    def _policy_for_snapshot(self, snapshot):
        del snapshot
        return self.manifest_policy

    def _state_at_revision(self, revision):
        if revision == 0:
            opening = self._opening_entry()
            if isinstance(opening, dict):
                opening_state = (opening.get("variables") or {}).get("stat_data")
                if isinstance(opening_state, dict):
                    return copy.deepcopy(opening_state)
            return self._read_json(self.card_folder / ".initvar.json", {})
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM state_snapshots WHERE session_id = ? AND revision = ?",
                (self.session_id, revision),
            ).fetchone()
        if not row:
            raise RuntimeError("revision state snapshot is unavailable")
        return json.loads(row["state_json"])

    def _runtime_turns(self, base_revision, limit=3):
        """Walk the parent chain from ``base_revision`` (active-lineage context).

        Superseded sibling branches are excluded even when their revision numbers
        are lower — only the chain head→parent→… is returned (oldest→newest),
        capped at ``limit``.
        """
        if base_revision is None or base_revision <= 0:
            return []
        with self._connect() as connection:
            chain = []
            current = base_revision
            seen = set()
            while current and current > 0 and current not in seen and len(chain) < max(1, int(limit)):
                seen.add(current)
                row = connection.execute(
                    "SELECT commits.revision, commits.parent_revision, commits.draft, tasks.text "
                    "FROM commits JOIN tasks ON tasks.id = commits.task_id "
                    "WHERE commits.session_id = ? AND commits.revision = ?",
                    (self.session_id, current),
                ).fetchone()
                if not row:
                    break
                chain.append(
                    {
                        "revision": row["revision"],
                        "user": row["text"],
                        "assistant": TurnDraft.from_json(row["draft"]).content,
                    }
                )
                parent = row["parent_revision"]
                current = parent if parent is not None else 0
        chain.reverse()
        return chain

    def _source_recent_turns(self, base_revision, limit=3):
        """Build prompt history with the durable AI-only opening as its anchor.

        The opening is persisted separately from commits and therefore does not
        appear in ``_runtime_turns(0)``.  Prompt snapshots must still carry it,
        otherwise the first player input starts a conversation with no opening
        context.  Keep the opening out of the public commit-lineage API and
        reserve one recent-turn slot for it in provider-facing snapshots.
        """
        opening = self._opening_recent_turn()
        commit_limit = max(0, int(limit) - (1 if opening is not None else 0))
        turns = self._runtime_turns(base_revision, limit=max(1, commit_limit)) if commit_limit else []
        return ([opening] if opening is not None else []) + turns

    def _opening_recent_turn(self):
        opening = self._opening_entry()
        if not isinstance(opening, dict):
            return None
        content = opening.get("ai")
        if content is None:
            content = opening.get("assistant")
        if content is None:
            content = opening.get("content")
        if not isinstance(content, str) or not content:
            return None
        return {"revision": 0, "user": "", "assistant": content}

    def _projected_state(self, base_revision, draft):
        base_state = self._state_at_revision(base_revision)
        return self._projected_state_from_base(base_state, draft)

    @staticmethod
    def _projected_state_from_base(base_state, draft):
        source = draft.mvu_commands if draft.mvu_commands else draft.content
        commands = extract_commands(source)
        state, _ = execute_commands(base_state, commands) if commands else (base_state, {})
        return state

    @staticmethod
    def _file_source(path):
        try:
            raw = Path(path).read_bytes()
        except OSError:
            raw = b""
        return {"id": str(Path(path).name), "version": SessionTurnRuntime._hash_bytes(raw)}

    @staticmethod
    def _hash_bytes(value):
        import hashlib
        return hashlib.sha256(value).hexdigest()

    def _task_text(self, task_id):
        with self._connect() as connection:
            return connection.execute("SELECT text FROM tasks WHERE id = ?", (task_id,)).fetchone()["text"]

    def _active_revision(self, connection):
        return connection.execute("SELECT active_revision FROM sessions WHERE id = ?", (self.session_id,)).fetchone()["active_revision"]

    @staticmethod
    def _result(task):
        attempt = int(task["attempt"] or 0) if "attempt" in task.keys() else 0
        return RuntimeResult(task["id"], task["commit_id"], task["revision"], task["status"], attempt=attempt)

    def _task_for_key(self, connection, idempotency_key):
        stored_key = self._stored_idempotency_key(idempotency_key)
        return connection.execute(
            "SELECT id, commit_id, revision, status, text, base_revision, source_snapshot "
            "FROM tasks WHERE session_id = ? AND idempotency_key IN (?, ?) "
            "ORDER BY (idempotency_key = ?) DESC LIMIT 1",
            (self.session_id, stored_key, idempotency_key, stored_key),
        ).fetchone()

    def _stored_idempotency_key(self, idempotency_key):
        return f"{self.session_id}::{idempotency_key}"

    @staticmethod
    def _compiled_from_manifest(manifest):
        return CompiledContext(
            manifest["payload"],
            manifest,
            manifest["payload_hash"],
            manifest["stable_payload_hash"],
        )

    @staticmethod
    def _manifest_row(row):
        if not row:
            return None
        manifest = json.loads(row["manifest_json"])
        manifest["id"] = row["id"]
        manifest["payload"] = json.loads(row["payload_json"])
        return manifest

    def capture_opening_from_chat_log(self, *, event_type=None, event_payload=None):
        """Persist the AI-only opening currently projected in ``chat_log``.

        Startup and legacy recovery only need the stored opening. Interactive
        opening selection additionally supplies an event so reconnecting
        browser clients can refresh from the durable runtime stream.
        """
        log = self._read_json(self.card_folder / "chat_log.json", [])
        opening = None
        if isinstance(log, list) and log:
            candidate = log[0]
            if isinstance(candidate, dict) and not candidate.get("user"):
                opening = copy.deepcopy(candidate)
                opening["index"] = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE sessions SET opening_turn_json = ? WHERE id = ?",
                (self._canonical(opening) if opening is not None else None, self.session_id),
            )
            if event_type:
                self._event(connection, event_type, dict(event_payload or {}))
        return opening is not None

    def opening_turn(self):
        """Return this session's durable AI-only opening, if one exists."""
        opening = self._opening_entry()
        return copy.deepcopy(opening) if opening is not None else None

    def card_facts(self):
        """Return the imported card payload without exposing projection I/O helpers."""
        card_data = self._read_json(self.card_folder / ".card_data.json", {})
        facts = card_data.get("data") if isinstance(card_data.get("data"), dict) else card_data
        return copy.deepcopy(facts) if isinstance(facts, dict) else {}

    def set_opening_turn(self, opening, *, event_type=None, event_payload=None):
        """Replace this session's durable opening without reading shared projection files."""
        if not isinstance(opening, dict) or opening.get("user"):
            raise ValueError("opening must be an AI-only turn object")
        stored = copy.deepcopy(opening)
        stored["index"] = 0
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE sessions SET opening_turn_json = ? WHERE id = ?",
                (self._canonical(stored), self.session_id),
            )
            if event_type:
                self._event(connection, event_type, dict(event_payload or {}))
        return copy.deepcopy(stored)

    def visible_turns(self, head_revision=None):
        head = self.active_revision() if head_revision is None else head_revision
        visible = []
        if self._opening_entry() is not None:
            visible.append({"visible_index": 0, "revision": 0, "is_opening": True})
        for item in self._lineage_commits(head):
            visible.append({
                "visible_index": len(visible),
                "revision": item["revision"],
                "is_opening": False,
            })
        return visible

    def resume_projection(self):
        if self._opening_entry() is None:
            self.capture_opening_from_chat_log()
        self._rebuild_active_projections(self.active_revision())

    def _rebuild_active_projections(self, head_revision):
        """Rewrite compatibility files from opening + active lineage + head state."""
        from airp.engine.card import write_chat_log, write_state

        projection_log = self._projection_log(head_revision)
        state_js = self._state_js_for_projection(projection_log, head_revision)
        backups = self.projection._backup()
        try:
            write_chat_log(self.card_folder, [])
            for item in self._lineage_commits(head_revision):
                self.projection.apply(item["text"], item["draft"], tokens=item.get("tokens"))
            if self._opening_entry() is not None:
                write_chat_log(self.card_folder, projection_log)
                self.projection.rewrite_content()
            write_state(state_js, self.card_folder, projection_root=self.projection.projection_root)
        except Exception:
            self.projection._restore(backups)
            raise

    def _projection_log(self, head_revision):
        log = []
        opening = self._opening_entry()
        previous_state = None
        if opening is not None:
            opening_entry = copy.deepcopy(opening)
            opening_entry["index"] = 0
            log.append(opening_entry)
            previous_state = ((opening_entry.get("variables") or {}).get("stat_data") or None)
        if previous_state is None:
            previous_state = self._read_json(self.card_folder / ".initvar.json", {})
        for item in self._lineage_commits(head_revision):
            entry, previous_state = self._projection_entry_for_commit(item, len(log), previous_state)
            log.append(entry)
        return log

    def _projection_entry_for_commit(self, item, index, previous_state):
        state = self._state_at_revision(item["revision"])
        draft = item["draft"]
        entry = {
            "index": index,
            # Rebuilds must use the same task-owned input as live projection.
            "user": item.get("text") or "",
            "ai": self._compose_ai_text(draft),
            "summary": draft.summary or "",
            "variables": {
                "stat_data": state,
                "delta": self._state_delta(previous_state or {}, state),
            },
        }
        if item.get("tokens"):
            entry["tokens"] = item["tokens"]
        return entry, state

    @staticmethod
    def _compose_ai_text(draft):
        ai_text = draft.content or ""
        if draft.summary:
            ai_text += "\n\n<summary>" + draft.summary + "</summary>"
        if draft.options:
            ai_text += "\n\n<options>\n" + draft.options + "\n</options>"
        return ai_text

    @staticmethod
    def _state_delta(before, after):
        delta = {}
        keys = set(before.keys()) | set(after.keys())
        for key in keys:
            b = before.get(key)
            a = after.get(key)
            if isinstance(b, dict) and isinstance(a, dict):
                nested = SessionTurnRuntime._state_delta(b, a)
                if nested:
                    delta[key] = nested
            elif b != a:
                delta[key] = a
        return delta

    def _state_js_for_projection(self, projection_log, head_revision):
        state = self._projection_root_state(head_revision, projection_log)
        total_tokens = sum(((turn.get("tokens") or {}).get("total") or 0) for turn in projection_log)
        payload = {
            "world": self._projection_field(state, (("世界", "世界名"), ("世界", "名称"))) or self._projection_field(self._read_json(self.card_folder / ".card_data.json", {}), (("name",), ("data", "name"))) or "",
            "stage": self._projection_field(state, (("剧情", "阶段"), ("玩家", "当前阶段"), ("stage",))) or "开局",
            "time": self._projection_field(state, (("世界", "时间"), ("time",))) or "",
            "location": self._projection_field(state, (("世界", "地点"), ("玩家", "现处地点"), ("location",))) or "",
            "env": self._projection_field(state, (("世界", "环境"), ("世界", "天气"), ("env",))) or "",
            "quest": self._projection_field(state, (("世界", "任务"), ("世界", "当前任务"), ("quest",))) or "",
            "generatedCount": len(projection_log),
            "totalTokens": int(total_tokens),
            "actions": [],
            "player": self._projection_field(state, (("玩家", "姓名"), ("player", "name"))) or "",
            "hp": self._projection_field(state, (("玩家", "HP"), ("player", "hp"))) or 0,
            "hpMax": self._projection_field(state, (("玩家", "HP上限"), ("player", "hpMax"))) or 0,
            "mp": self._projection_field(state, (("玩家", "MP"), ("player", "mp"))) or 0,
            "mpMax": self._projection_field(state, (("玩家", "MP上限"), ("player", "mpMax"))) or 0,
            "exp": self._projection_field(state, (("玩家", "EXP"), ("player", "exp"))) or 0,
            "expMax": self._projection_field(state, (("玩家", "EXP上限"), ("player", "expMax"))) or 0,
            "ed": bool(self._projection_field(state, (("玩家", "ed"), ("player", "ed"))) or False),
            "npcs": self._projection_npcs(state),
        }
        lines = ["window.STATE = {"]
        for key, value in payload.items():
            lines.append(f"  {key}: {json.dumps(value, ensure_ascii=False)},")
        lines.append("};")
        return "\n".join(lines) + "\n"

    def _projection_root_state(self, head_revision, projection_log):
        if head_revision and head_revision > 0:
            return self._state_at_revision(head_revision)
        if projection_log:
            opening_state = ((projection_log[0].get("variables") or {}).get("stat_data") or None)
            if opening_state is not None:
                return opening_state
        return self._read_json(self.card_folder / ".initvar.json", {})

    @staticmethod
    def _projection_field(state, path_options):
        for path in path_options:
            node = state
            ok = True
            for part in path:
                if not isinstance(node, dict) or part not in node:
                    ok = False
                    break
                node = node[part]
            if ok and node not in (None, ""):
                return node
        return None

    @staticmethod
    def _projection_npcs(state):
        if not isinstance(state, dict):
            return []
        out = []
        for key, value in state.items():
            if key.startswith("_") or key in {"世界", "玩家", "player", "world"}:
                continue
            if isinstance(value, dict):
                out.append({
                    "name": key,
                    "status": value.get("当前状况") or value.get("现状") or value.get("status") or "",
                })
        return out

    def _opening_entry(self):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT opening_turn_json FROM sessions WHERE id = ?",
                (self.session_id,),
            ).fetchone()
        if not row or not row["opening_turn_json"]:
            return None
        try:
            data = json.loads(row["opening_turn_json"])
        except json.JSONDecodeError:
            return None
        return data if isinstance(data, dict) else None

    def _bootstrap_legacy_history_if_needed(self):
        legacy_log = self._read_json(self.card_folder / "chat_log.json", [])
        if not isinstance(legacy_log, list):
            legacy_log = []
        imported = False
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            opening_captured = self._capture_opening_if_missing_in_connection(connection, legacy_log)
            commit_count = connection.execute(
                "SELECT COUNT(*) AS count FROM commits WHERE session_id = ?",
                (self.session_id,),
            ).fetchone()["count"]
            task_count = connection.execute(
                "SELECT COUNT(*) AS count FROM tasks WHERE session_id = ?",
                (self.session_id,),
            ).fetchone()["count"]
            if commit_count == 0 and task_count == 0 and legacy_log:
                active_revision, imported_count = self._import_legacy_turns_in_connection(connection, legacy_log)
                connection.execute(
                    "UPDATE sessions SET active_revision = ? WHERE id = ?",
                    (active_revision, self.session_id),
                )
                if imported_count or opening_captured:
                    self._event(
                        connection,
                        "session.legacy_bootstrapped",
                        {"imported_revisions": imported_count, "active_revision": active_revision},
                    )
                    imported = True
        if imported:
            self._rebuild_active_projections(self.active_revision())

    def _capture_opening_if_missing_in_connection(self, connection, legacy_log):
        if not legacy_log:
            return False
        current = connection.execute(
            "SELECT opening_turn_json FROM sessions WHERE id = ?",
            (self.session_id,),
        ).fetchone()
        if current and current["opening_turn_json"]:
            return False
        opening = legacy_log[0]
        if not isinstance(opening, dict) or opening.get("user"):
            return False
        opening_copy = copy.deepcopy(opening)
        opening_copy["index"] = 0
        connection.execute(
            "UPDATE sessions SET opening_turn_json = ? WHERE id = ?",
            (self._canonical(opening_copy), self.session_id),
        )
        return True

    def _import_legacy_turns_in_connection(self, connection, legacy_log):
        imported_turns = []
        previous_state = self._read_json(self.card_folder / ".initvar.json", {})
        previous_revision = 0
        imported_count = 0
        max_revision = 0
        if legacy_log and isinstance(legacy_log[0], dict) and not legacy_log[0].get("user"):
            opening_state = ((legacy_log[0].get("variables") or {}).get("stat_data") or None)
            if isinstance(opening_state, dict) and opening_state:
                previous_state = copy.deepcopy(opening_state)
        for turn in legacy_log:
            if not isinstance(turn, dict) or not (turn.get("user") or "").strip():
                continue
            draft = parse_legacy_turn(
                turn.get("ai", ""),
                fallback_input=turn.get("user", ""),
            )
            if not draft.summary and turn.get("summary"):
                draft = TurnDraft(
                    content=draft.content,
                    summary=turn.get("summary", ""),
                    options=draft.options,
                    polished_input=draft.polished_input,
                    mvu_commands=draft.mvu_commands,
                )
            state = ((turn.get("variables") or {}).get("stat_data") or None)
            if not isinstance(state, dict) or not state:
                state = self._projected_state_from_base(copy.deepcopy(previous_state), draft)
            task_id = self._id()
            commit_id = self._id()
            imported_count += 1
            revision = imported_count
            max_revision = revision
            source_snapshot = self._legacy_source_snapshot(previous_state, imported_turns)
            connection.execute(
                "INSERT INTO tasks (id, session_id, idempotency_key, text, status, commit_id, revision, base_revision, source_snapshot, validation_failures, validation_exhausted) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)",
                (
                    task_id,
                    self.session_id,
                    f'legacy-import-{revision}-{task_id[:8]}',
                    turn.get("user", ""),
                    "succeeded",
                    commit_id,
                    revision,
                    previous_revision,
                    self._canonical(source_snapshot),
                ),
            )
            connection.execute(
                "INSERT INTO commits (id, session_id, revision, task_id, draft, parent_revision) VALUES (?, ?, ?, ?, ?, ?)",
                (commit_id, self.session_id, revision, task_id, draft.to_json(), previous_revision),
            )
            connection.execute(
                "INSERT INTO projection_checkpoints (commit_id, state, applied_marker) VALUES (?, ?, ?)",
                (commit_id, "applied", commit_id),
            )
            connection.execute(
                "INSERT INTO state_snapshots (session_id, revision, state_json) VALUES (?, ?, ?)",
                (self.session_id, revision, self._canonical(state)),
            )
            tokens = turn.get("tokens") or {}
            prompt_tokens = int(tokens.get("in") or 0)
            completion_tokens = int(tokens.get("out") or 0)
            total_tokens = int(tokens.get("total") or tokens.get("round_total") or tokens.get("startup_total") or (prompt_tokens + completion_tokens))
            if total_tokens or prompt_tokens or completion_tokens:
                connection.execute(
                    "INSERT INTO model_calls (id, session_id, task_id, call_ordinal, manifest_id, model, prompt_tokens, completion_tokens, total_tokens, stop_reason, latency_ms, cost_amount, cost_currency, cost_rate_version) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self._id(),
                        self.session_id,
                        task_id,
                        1,
                        None,
                        "legacy-import",
                        prompt_tokens,
                        completion_tokens,
                        total_tokens,
                        "legacy_import",
                        0,
                        0.0,
                        "USD",
                        "legacy-import",
                    ),
                )
            imported_turns.append({"revision": revision, "user": turn.get("user", ""), "assistant": draft.content})
            if len(imported_turns) > 3:
                imported_turns = imported_turns[-3:]
            previous_state = copy.deepcopy(state)
            previous_revision = revision
        return max_revision, imported_count

    def _legacy_source_snapshot(self, current_state, recent_turns):
        memory = self.card_folder / "memory"
        initvar_path = self.card_folder / ".initvar.json"
        card_data_path = self.card_folder / ".card_data.json"
        catalog_path = memory / ".worldbook_index.json"
        reference_path = memory / "reference.md"
        user_path = memory / "user.md"
        structure_path = memory / ".card_structure.json"
        project_path = memory / "project.md"
        initvar = self._read_json(initvar_path, {})
        return {
            "card_facts": self._read_json(card_data_path, {}),
            "settings": self.session_settings,
            "worldbook_catalog": self._read_json(catalog_path, []),
            "worldbook_reference": self._read_text(reference_path),
            "worldbook_user": self._read_text(user_path),
            "card_structure": self._read_json(structure_path, {}),
            "initvar": initvar,
            "current_state": current_state,
            "recent_memory": self._recent_memory(project_path),
            "recent_turns": list(recent_turns),
            "sources": {
                "card_facts": self._file_source(card_data_path),
                "settings": {"id": "session_settings", "version": self._hash_bytes(self._canonical(self.session_settings).encode("utf-8"))},
                "worldbook_catalog": self._file_source(catalog_path),
                "card_structure": self._file_source(structure_path),
                "initvar": self._file_source(initvar_path),
                "current_state": {"id": "runtime_state", "version": "legacy"},
                "recent_memory": self._file_source(project_path),
                "recent_turns": {"id": "legacy_lineage", "version": str(len(recent_turns))},
            },
        }

    def _recover_startup_state(self):
        pending = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT id, commit_id, revision, status, lease_fence FROM tasks WHERE session_id = ? "
                "AND status IN ('projection_pending', 'running', 'leased', 'queued')",
                (self.session_id,),
            ).fetchall()
            for row in rows:
                if row["status"] == "projection_pending" and row["commit_id"]:
                    pending.append(RuntimeResult(row["id"], row["commit_id"], row["revision"], "projection_pending"))
                elif row["status"] in ("running", "leased") and not row["commit_id"]:
                    recovered_status = f'abandoned_{row["status"]}'
                    connection.execute(
                        "UPDATE tasks SET status = ? WHERE id = ?",
                        (recovered_status, row["id"]),
                    )
                    connection.execute(
                        "UPDATE task_attempts SET status = ? WHERE task_id = ? AND lease_fence = ? AND status = ?",
                        ("abandoned", row["id"], row["lease_fence"], "leased"),
                    )
                    self._event(connection, f"task.{recovered_status}", {"task_id": row["id"], "recovered_on_startup": True})
            # Queued work remains durable and eligible. Clearing the active holder
            # makes the next submit atomically acquire a fresh (higher) fence.
            connection.execute(
                "UPDATE sessions SET active_generation_task_id = NULL WHERE id = ?",
                (self.session_id,),
            )
        for result in pending:
            try:
                self._project(result)
            except Exception:
                pass
        self._recover_graph_runs()
        self._recover_debug_replays()

    def _recover_graph_runs(self) -> None:
        """Classify persisted in-flight graph traces after a process restart."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT id, task_id, plan_id, retry_of FROM graph_runs WHERE session_id = ? AND status = 'running'",
                (self.session_id,),
            ).fetchall()
            for row in rows:
                now = int(time.time())
                error = {"code": "interrupted", "message": "Graph Run interrupted by runtime restart"}
                connection.execute(
                    "UPDATE graph_runs SET status = 'interrupted', error_json = ?, finished_at = ? WHERE id = ? AND session_id = ?",
                    (_trace_json(error), now, row["id"], self.session_id),
                )
                connection.execute(
                    "UPDATE node_runs SET state = 'failed', error_json = ?, finished_at = ? WHERE graph_run_id = ? AND session_id = ? AND state = 'running'",
                    (_trace_json(error), now, row["id"], self.session_id),
                )
                self._event(
                    connection,
                    "graph.run.finished",
                    {
                        "task_id": row["task_id"],
                        "graph_run_id": row["id"],
                        "run_id": row["id"],
                        "plan_id": row["plan_id"],
                        "retry_of": row["retry_of"],
                        "state": "interrupted",
                        "status": "interrupted",
                        "error": error,
                    },
                )
        self._prune_graph_runs()

    def _recover_debug_replays(self) -> None:
        """Classify in-flight Debug Replays after a process restart."""
        now = int(time.time())
        error = {
            "code": "interrupted",
            "message": "Debug Replay interrupted by runtime restart",
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE debug_replays SET state = 'interrupted', error_json = ?, finished_at = ? "
                "WHERE session_id = ? AND state = 'running'",
                (_trace_json(error), now, self.session_id),
            )
        self._prune_debug_replays()

    def _lineage_commits(self, head_revision):
        """Full parent-chain walk from head (oldest→newest), no limit.

        Each item carries ``tokens`` (aggregated model_call usage for its task)
        so projection can populate totalTokens without a separate query per turn.
        """
        if head_revision is None or head_revision <= 0:
            return []
        with self._connect() as connection:
            chain = []
            current = head_revision
            seen = set()
            while current and current > 0 and current not in seen:
                seen.add(current)
                row = connection.execute(
                    "SELECT commits.revision, commits.parent_revision, commits.draft, commits.task_id, tasks.text "
                    "FROM commits JOIN tasks ON tasks.id = commits.task_id "
                    "WHERE commits.session_id = ? AND commits.revision = ?",
                    (self.session_id, current),
                ).fetchone()
                if not row:
                    break
                task_tokens = connection.execute(
                    "SELECT COALESCE(SUM(total_tokens),0) AS t FROM model_calls "
                    "WHERE session_id = ? AND task_id = ?",
                    (self.session_id, row["task_id"]),
                ).fetchone()["t"]
                chain.append(
                    {
                        "revision": row["revision"],
                        "parent_revision": row["parent_revision"] if row["parent_revision"] is not None else 0,
                        "text": row["text"],
                        "draft": TurnDraft.from_json(row["draft"]),
                        "tokens": {"in": 0, "out": 0, "total": int(task_tokens)} if task_tokens else None,
                    }
                )
                parent = row["parent_revision"]
                current = parent if parent is not None else 0
        chain.reverse()
        return chain

    def _initialize(self):
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, active_revision INTEGER NOT NULL, active_generation_task_id TEXT, generation_fence INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
                    text TEXT NOT NULL, status TEXT NOT NULL, commit_id TEXT, revision INTEGER NOT NULL,
                    base_revision INTEGER, source_snapshot TEXT, validation_failures INTEGER NOT NULL DEFAULT 0,
                    validation_exhausted INTEGER NOT NULL DEFAULT 0, queue_sequence INTEGER, lease_fence INTEGER
                );
                CREATE TABLE IF NOT EXISTS commits (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    task_id TEXT NOT NULL UNIQUE, draft TEXT NOT NULL, parent_revision INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS projection_checkpoints (commit_id TEXT PRIMARY KEY, state TEXT NOT NULL, applied_marker TEXT);
                CREATE TABLE IF NOT EXISTS state_snapshots (session_id TEXT NOT NULL, revision INTEGER NOT NULL, state_json TEXT NOT NULL, PRIMARY KEY (session_id, revision));
                CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS task_attempts (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, task_id TEXT NOT NULL,
                    lease_fence INTEGER NOT NULL, status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS context_manifests (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, task_id TEXT NOT NULL, base_revision INTEGER NOT NULL,
                    call_ordinal INTEGER NOT NULL, manifest_json TEXT NOT NULL, payload_json TEXT NOT NULL,
                    payload_hash TEXT NOT NULL, stable_payload_hash TEXT NOT NULL, policy_version TEXT NOT NULL,
                    token_budget INTEGER NOT NULL, estimated_tokens INTEGER NOT NULL, UNIQUE(task_id, call_ordinal)
                );
                CREATE TABLE IF NOT EXISTS worldbook_loads (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, task_id TEXT NOT NULL, base_revision INTEGER NOT NULL,
                    call_ordinal INTEGER NOT NULL, title TEXT NOT NULL, catalog_hash TEXT NOT NULL, reference_hash TEXT NOT NULL,
                    content_hash TEXT NOT NULL, content TEXT NOT NULL, reason TEXT NOT NULL, UNIQUE(task_id, call_ordinal, title)
                );
                CREATE TABLE IF NOT EXISTS model_calls (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, task_id TEXT NOT NULL, call_ordinal INTEGER NOT NULL,
                    manifest_id TEXT, model TEXT, prompt_tokens INTEGER NOT NULL, completion_tokens INTEGER NOT NULL,
                    total_tokens INTEGER NOT NULL, stop_reason TEXT, latency_ms INTEGER NOT NULL,
                    cost_amount REAL NOT NULL, cost_currency TEXT NOT NULL, cost_rate_version TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS graph_runs (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, task_id TEXT NOT NULL UNIQUE,
                    plan_id TEXT NOT NULL, graph_id TEXT NOT NULL, retry_of TEXT, status TEXT NOT NULL,
                    player_input TEXT NOT NULL, plan_json TEXT NOT NULL, failed_node_id TEXT,
                    error_json TEXT, created_at INTEGER NOT NULL, started_at INTEGER,
                    finished_at INTEGER
                );
                CREATE TABLE IF NOT EXISTS node_runs (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, graph_run_id TEXT NOT NULL,
                    task_id TEXT NOT NULL, node_id TEXT NOT NULL, agent_id TEXT NOT NULL,
                    label TEXT, order_index INTEGER NOT NULL, state TEXT NOT NULL,
                    input_artifact_json TEXT, prompt_json TEXT NOT NULL,
                    prompt_provenance_json TEXT NOT NULL, effective_config_json TEXT NOT NULL,
                    model_calls_json TEXT NOT NULL, tool_calls_json TEXT NOT NULL,
                    streamed_output TEXT NOT NULL, tool_snapshot_json TEXT NOT NULL DEFAULT '{}', final_output TEXT,
                    artifact_json TEXT, error_json TEXT, diagnostics_ref TEXT,
                    started_at INTEGER, finished_at INTEGER,
                    UNIQUE(graph_run_id, node_id)
                );
                CREATE TABLE IF NOT EXISTS debug_replays (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, idempotency_key TEXT UNIQUE,
                    source_node_run_id TEXT NOT NULL, source_graph_run_id TEXT NOT NULL,
                    node_id TEXT NOT NULL, agent_id TEXT NOT NULL, state TEXT NOT NULL,
                    input_artifact_json TEXT NOT NULL, upstream_artifacts_json TEXT NOT NULL,
                    tool_snapshot_json TEXT NOT NULL, old_output TEXT, new_output TEXT,
                    old_effective_config_json TEXT NOT NULL, new_effective_config_json TEXT NOT NULL,
                    effective_config_diff_json TEXT NOT NULL, error_json TEXT,
                    created_at INTEGER NOT NULL, started_at INTEGER, finished_at INTEGER
                );
                """
            )
            columns = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
            if "base_revision" not in columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN base_revision INTEGER")
            if "source_snapshot" not in columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN source_snapshot TEXT")
            if "validation_failures" not in columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN validation_failures INTEGER NOT NULL DEFAULT 0")
            if "validation_exhausted" not in columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN validation_exhausted INTEGER NOT NULL DEFAULT 0")
            checkpoint_columns = {row["name"] for row in connection.execute("PRAGMA table_info(projection_checkpoints)")}
            if "applied_marker" not in checkpoint_columns:
                connection.execute("ALTER TABLE projection_checkpoints ADD COLUMN applied_marker TEXT")
            commit_columns = {row["name"] for row in connection.execute("PRAGMA table_info(commits)")}
            if "parent_revision" not in commit_columns:
                connection.execute(
                    "ALTER TABLE commits ADD COLUMN parent_revision INTEGER NOT NULL DEFAULT 0"
                )
            session_columns = {row["name"] for row in connection.execute("PRAGMA table_info(sessions)")}
            if "opening_turn_json" not in session_columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN opening_turn_json TEXT")
            if "generation_fence" not in session_columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN generation_fence INTEGER NOT NULL DEFAULT 0")
            if "active_generation_task_id" not in session_columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN active_generation_task_id TEXT")
            task_columns = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
            if "queue_sequence" not in task_columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN queue_sequence INTEGER")
            if "lease_fence" not in task_columns:
                connection.execute("ALTER TABLE tasks ADD COLUMN lease_fence INTEGER")
            graph_run_columns = {row["name"] for row in connection.execute("PRAGMA table_info(graph_runs)")}
            if "retry_of" not in graph_run_columns:
                connection.execute("ALTER TABLE graph_runs ADD COLUMN retry_of TEXT")
            node_run_columns = {row["name"] for row in connection.execute("PRAGMA table_info(node_runs)")}
            if "tool_snapshot_json" not in node_run_columns:
                connection.execute(
                    "ALTER TABLE node_runs ADD COLUMN tool_snapshot_json TEXT NOT NULL DEFAULT '{}'"
                )
            connection.execute(
                "UPDATE tasks SET queue_sequence = rowid WHERE queue_sequence IS NULL"
            )
            # Backfill linear history: parent = revision - 1 (rev 1 → 0).
            # Only fill rows still at the DEFAULT 0 that are not the first commit.
            # For a pure linear DB this is correct; branched DBs already set parents.
            connection.execute(
                "UPDATE commits SET parent_revision = CASE "
                "WHEN revision <= 1 THEN 0 ELSE revision - 1 END "
                "WHERE parent_revision = 0 AND revision > 1 "
                "AND session_id = ?",
                (self.session_id,),
            )
            # Also fix any NULL-ish leftover if an older ALTER path left defaults odd.
            connection.execute(
                "UPDATE commits SET parent_revision = 0 WHERE parent_revision IS NULL"
            )
            connection.execute("UPDATE tasks SET base_revision = MAX(revision - 1, 0) WHERE base_revision IS NULL")
            connection.execute("UPDATE tasks SET validation_failures = 0 WHERE validation_failures IS NULL")
            connection.execute("UPDATE tasks SET validation_exhausted = 0 WHERE validation_exhausted IS NULL")
            connection.execute("INSERT OR IGNORE INTO schema_migrations (version) VALUES (2)")
            connection.execute("INSERT OR IGNORE INTO schema_migrations (version) VALUES (3)")
            connection.execute("INSERT OR IGNORE INTO schema_migrations (version) VALUES (4)")
            connection.execute("INSERT OR IGNORE INTO sessions (id, active_revision) VALUES (?, 0)", (self.session_id,))
        if self.bootstrap_legacy_history:
            self._bootstrap_legacy_history_if_needed()
        self._recover_startup_state()

    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    def _event(self, connection, event_type, payload):
        connection.execute("INSERT INTO events (session_id, type, payload) VALUES (?, ?, ?)", (self.session_id, event_type, self._canonical(payload)))

    @staticmethod
    def _read_json(path, fallback):
        try:
            return json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return fallback

    @staticmethod
    def _read_text(path):
        try:
            return Path(path).read_text(encoding="utf-8")
        except OSError:
            return ""

    @staticmethod
    def _recent_memory(path):
        try:
            return Path(path).read_text(encoding="utf-8")[-3000:]
        except OSError:
            return ""

    @staticmethod
    def _canonical(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _id():
        return str(uuid.uuid4())


# ═══ Module-level helpers for state-proposal validation ═══


def extract_commands_for_proposal(proposal):
    """Translate a JSONPatch proposal (list of ops) into MVU commands.

    The ``validate_state_proposal`` tool accepts the same JSONPatch op shape
    the model emits inside ``<JSONPatch>`` blocks (op/path/value[/from]). We
    synthesize a ``<JSONPatch>`` envelope and reuse :func:`extract_commands`
    so the proposal path and the live commit path share one parser — no
    duplicate MVU semantics.
    """
    if not isinstance(proposal, list):
        raise ValueError("proposal must be a list of JSONPatch operations")
    envelope = "<JSONPatch>\n" + json.dumps(proposal, ensure_ascii=False) + "\n</JSONPatch>"
    return extract_commands(envelope)


def _collect_changed_paths(before, after, prefix=""):
    """Yield leaf paths whose values differ between ``before`` and ``after``."""
    paths = set()
    if isinstance(before, dict) and isinstance(after, dict):
        for key in set(before.keys()) | set(after.keys()):
            full = f"{prefix}.{key}" if prefix else key
            if key not in before or key not in after:
                paths.add(full)
            elif isinstance(before[key], dict) and isinstance(after[key], dict):
                paths.update(_collect_changed_paths(before[key], after[key], full))
            elif before[key] != after[key]:
                paths.add(full)
    return paths
