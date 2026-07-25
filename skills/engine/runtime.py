import hashlib
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

import handler
from engine.context_compiler import CompiledContext, ContextCompileRequest, ContextPolicy, compile_context, replay_payload
from engine.director import DirectorHandle, NarrativeDirector
from engine.mvu import execute_commands, extract_commands, generate_schema, validate_command_strict
from engine.provider import AbortSignal, ProviderAborted, ProviderError
from engine.quality import DefaultQualityGate, QualityContext, QualityGate, QualityPolicy
from engine.tools import ToolResult, ToolRegistry, validate_draft_dict
from engine.worldbook import load_worldbook_entry_from_texts


@dataclass(frozen=True)
class TurnDraft:
    """Structured narrative turn, authored by the director and committed by the runtime.

    ``content`` / ``summary`` / ``options`` mirror the existing projection
    contract; ``polished_input`` carries the (optional) editor-pass of the
    player's input and ``mvu_commands`` carries the raw MVU payload
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


@dataclass(frozen=True)
class RuntimeEvent:
    sequence: int
    type: str
    payload: dict


@dataclass(frozen=True)
class TurnCommit:
    id: str
    revision: int
    task_id: str


class FakeNarrativeExecutor:
    def __init__(self, content, summary="", options=""):
        self._draft = TurnDraft(content=content, summary=summary, options=options)

    def run(self, text, compiled_context=None):
        return self._draft


class LegacyProjectionAdapter:
    def __init__(self, card_folder, projection_root):
        self.card_folder = Path(card_folder)
        self.projection_root = Path(projection_root)

    def apply(self, text, draft):
        backups = self._backup()
        try:
            full_text = draft.content
            if draft.mvu_commands:
                full_text = full_text + "\n" + draft.mvu_commands
            polished = draft.polished_input or text
            handler.append_turn(
                self.card_folder,
                polished_input=polished,
                content=draft.content,
                summary=draft.summary,
                options=draft.options,
                full_text=full_text,
                projection_root=self.projection_root,
            )
        except Exception:
            self._restore(backups)
            raise

    def _backup(self):
        paths = [
            self.card_folder / "chat_log.json",
            self.card_folder / "content.js",
            self.card_folder / "state.js",
            self.card_folder / ".var_diff.json",
            self.projection_root / "content.js",
            self.projection_root / "state.js",
        ]
        return {path: path.read_bytes() if path.exists() else None for path in paths}

    @staticmethod
    def _restore(backups):
        for path, contents in backups.items():
            if contents is None:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(contents)


class SessionTurnRuntime:
    def __init__(
        self,
        database_path,
        card_folder,
        projection_root,
        executor,
        session_id="local",
        manifest_policy=None,
        session_settings=None,
        quality_gate: QualityGate | None = None,
        quality_policy: QualityPolicy | None = None,
        max_commit_validation_retries: int = 3,
    ):
        self.database_path = Path(database_path)
        self.card_folder = Path(card_folder)
        self.executor = executor
        self.session_id = session_id
        self.manifest_policy = manifest_policy or ContextPolicy(version="runtime-v1", token_budget=8000)
        self.session_settings = json.loads(self._canonical(session_settings or {}))
        self.quality_policy = quality_policy or QualityPolicy()
        self.quality_gate = quality_gate or DefaultQualityGate(self.quality_policy)
        self.max_commit_validation_retries = max(1, int(max_commit_validation_retries))
        self.projection = LegacyProjectionAdapter(card_folder, projection_root)
        self._lock = threading.RLock()
        self._abort_signals: dict[str, AbortSignal] = {}
        self._initialize()

    def submit(self, text, idempotency_key):
        if not text.strip():
            raise ValueError("empty input")
        with self._lock:
            task = self._create_or_get_task(text, idempotency_key)
            result = self._result(task)
            if result.commit_id:
                return self._project(result)
            if result.status == "succeeded":
                return result

            is_director = isinstance(self.executor, NarrativeDirector)
            signal = None
            if is_director:
                # Register the abort signal BEFORE the task flips to "running".
                # stop() deliberately does not take self._lock (it must be able to
                # interrupt a blocked director), so cancellation correctness rests
                # on the signal being visible for the task's entire live window —
                # including the I/O-performing context compilation that precedes
                # the director. Without this, a stop() during compile finds no
                # signal and a non-"queued" status and is silently lost. Full
                # lease/recovery semantics land in Tickets 04/07.
                signal = AbortSignal()
                self._abort_signals[task["id"]] = signal
            try:
                compiled = self._compile_and_persist(task)
                if is_director:
                    return self._run_director(task, text, compiled, signal)
                draft = self._execute(text, compiled)
                result = self._commit_draft(task, draft)
                return self._project(result)
            finally:
                if signal is not None:
                    self._abort_signals.pop(task["id"], None)

    def stop(self, task_id):
        """Cancel a running or queued narrative-director task.

        For a task whose director is currently running, this cancels the shared
        :class:`AbortSignal`; the director loop and provider stream observe it
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
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id FROM tasks WHERE idempotency_key = ?", (idempotency_key,)
            ).fetchone()
        return row["id"] if row else None

    def active_revision(self):
        with self._connect() as connection:
            return connection.execute(
                "SELECT active_revision FROM sessions WHERE id = ?", (self.session_id,)
            ).fetchone()["active_revision"]

    def task(self, task_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, commit_id, revision, status FROM tasks WHERE id = ? AND session_id = ?",
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

    def events_after(self, sequence):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT sequence, type, payload FROM events WHERE session_id = ? AND sequence > ? ORDER BY sequence",
                (self.session_id, sequence),
            ).fetchall()
        return [RuntimeEvent(row["sequence"], row["type"], json.loads(row["payload"])) for row in rows]

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

    def _create_or_get_task(self, text, idempotency_key):
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task = self._task_for_key(connection, idempotency_key)
            if task:
                return task
            base_revision = self._active_revision(connection)
            source_snapshot = self._source_snapshot(base_revision)
            task_id = self._id()
            connection.execute(
                "INSERT INTO tasks (id, session_id, idempotency_key, text, status, revision, base_revision, source_snapshot) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (task_id, self.session_id, idempotency_key, text, "queued", 0, base_revision, self._canonical(source_snapshot)),
            )
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
                compiled = compile_context(ContextCompileRequest(
                    session_id=self.session_id,
                    task_id=task_id,
                    base_revision=task["base_revision"],
                    player_input=player_input,
                    snapshot=snapshot,
                    policy=self.manifest_policy,
                    call_ordinal=call_ordinal,
                    worldbook_loads=tuple(loads),
                ))
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
                compiled.stable_payload_hash, self.manifest_policy.version, self.manifest_policy.token_budget,
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
            policy=self.manifest_policy,
            worldbook_loads=tuple(loads),
        )
        compiled = compile_context(request)
        compiled = self._persist_manifest(task, compiled)
        with self._connect() as connection:
            # Guard the transition so a concurrent stop() that already marked this
            # queued task "cancelled" is not silently overwritten to "running".
            cur = connection.execute(
                "UPDATE tasks SET status = ? WHERE id = ? AND status = ?",
                ("running", task["id"], "queued"),
            )
            if cur.rowcount:
                self._event(connection, "task.running", {"task_id": task["id"]})
        return compiled

    def _execute(self, text, compiled):
        return self.executor.run(text, compiled)

    def _commit_draft(self, task, draft):
        stale = False
        commit_id = None
        revision = None
        validation_error = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._active_revision(connection) != task["base_revision"]:
                stale = True
            else:
                validation_error = self._validate_draft_before_commit(connection, task, draft)
                if validation_error is None:
                    commit_id, revision = self._perform_commit_in_connection(connection, task, draft)
        if stale:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute("UPDATE tasks SET status = ? WHERE id = ?", ("stale_revision", task["id"]))
                self._event(connection, "task.stale_revision", {"task_id": task["id"]})
            raise RuntimeError("stale revision")
        if validation_error is not None:
            raise RuntimeError(validation_error)
        return RuntimeResult(task["id"], commit_id, revision, "projection_pending")

    def _validate_draft_before_commit(self, connection, task, draft):
        verdict = self.quality_gate.validate(
            draft,
            self._quality_context(task),
        )
        if not verdict.ok:
            details = (verdict.metrics or {}) | {"reasons": list(verdict.reasons)}
            return self._reject_precommit(connection, task, "quality_gate_failed", details=details)

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
        exhausted = failures >= self.max_commit_validation_retries
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

    def _perform_commit_in_connection(self, connection, task, draft):
        """Insert commit + state snapshot + advance revision + emit turn.committed.

        Caller holds ``BEGIN IMMEDIATE`` and has verified revision freshness.
        Returns ``(commit_id, revision)``.
        """
        revision = self._active_revision(connection) + 1
        commit_id = self._id()
        connection.execute(
            "INSERT INTO commits (id, session_id, revision, task_id, draft) VALUES (?, ?, ?, ?, ?)",
            (commit_id, self.session_id, revision, task["id"], draft.to_json()),
        )
        connection.execute(
            "INSERT INTO projection_checkpoints (commit_id, state) VALUES (?, ?)",
            (commit_id, "pending"),
        )
        connection.execute(
            "INSERT INTO state_snapshots (session_id, revision, state_json) VALUES (?, ?, ?)",
            (self.session_id, revision, self._canonical(self._projected_state(task["base_revision"], draft))),
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
            {"task_id": task["id"], "commit_id": commit_id, "revision": revision},
        )
        return commit_id, revision

    def _commit_via_tool(self, task, draft_dict, expected_revision):
        """Commit path exercised by the ``commit_turn_draft`` tool.

        Idempotent: a second call for an already-committed task returns the
        existing commit. Validates ``expected_revision`` against the current
        active revision (optimistic) before any write and runs quality + MVU
        validation against the task's frozen ``base_revision`` snapshot.
        Performs NO projection — projection is applied by the submit loop once
        the director returns.
        """
        draft = TurnDraft(
            content=draft_dict["content"],
            summary=draft_dict.get("summary", "") or "",
            options=draft_dict.get("options", "") or "",
            polished_input=draft_dict.get("polished_input", "") or "",
            mvu_commands=draft_dict.get("mvu_commands", "") or "",
        )
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                task_now = connection.execute(
                    "SELECT status, validation_failures, validation_exhausted FROM tasks WHERE id = ?",
                    (task["id"],),
                ).fetchone()
                existing = connection.execute(
                    "SELECT id, revision FROM commits WHERE task_id = ?", (task["id"],)
                ).fetchone()
                if existing:
                    return ToolResult(
                        ok=True,
                        value={
                            "commit_id": existing["id"],
                            "revision": existing["revision"],
                            "reused": True,
                        },
                    )
                if task_now and task_now["validation_exhausted"]:
                    return ToolResult(ok=False, error=task_now["status"])
                active = self._active_revision(connection)
                if expected_revision != active:
                    connection.execute(
                        "UPDATE tasks SET status = ? WHERE id = ?",
                        ("stale_revision", task["id"]),
                    )
                    self._event(
                        connection,
                        "task.stale_revision",
                        {
                            "task_id": task["id"],
                            "expected": expected_revision,
                            "actual": active,
                        },
                    )
                    return ToolResult(
                        ok=False,
                        error="stale_revision",
                        value={"expected": expected_revision, "actual": active},
                    )
                validation_error = self._validate_draft_before_commit(connection, task, draft)
                if validation_error is not None:
                    return ToolResult(ok=False, error=validation_error)
                commit_id, revision = self._perform_commit_in_connection(connection, task, draft)
        except Exception as exc:
            return ToolResult(ok=False, error="commit_failed", value={"detail": str(exc)})
        return ToolResult(
            ok=True,
            value={"commit_id": commit_id, "revision": revision, "reused": False},
        )

    def _task_status(self, task_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
        return row["status"] if row else None

    def _finalize_cancelled(self, task_id):
        """Idempotently mark a non-committed task cancelled and return its result.

        Never overwrites a task that already committed (abort after commit must
        leave the turn standing — spec Decisions 20–21).
        """
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status, commit_id, revision FROM tasks WHERE id = ?", (task_id,)
            ).fetchone()
            if row and not row["commit_id"] and row["status"] != "cancelled":
                connection.execute(
                    "UPDATE tasks SET status = ? WHERE id = ?", ("cancelled", task_id)
                )
                self._event(connection, "task.cancelled", {"task_id": task_id})
            status = "cancelled" if (not row or not row["commit_id"]) else row["status"]
            commit_id = row["commit_id"] if row else None
            revision = row["revision"] if row else 0
        return RuntimeResult(task_id, commit_id, revision, status)

    # ═══ Narrative-director execution (Ticket 03) ═══

    def _run_director(self, task, text, compiled, signal):
        # If a concurrent stop() cancelled this task during context compilation
        # (or before the director body started), finalize as cancelled without
        # running the director — no commit, no preview promotion.
        if signal.cancelled or self._task_status(task["id"]) == "cancelled":
            return self._finalize_cancelled(task["id"])
        tools = ToolRegistry(self, task, self.manifest_policy)
        handle = DirectorHandle(task["id"], text, tools, signal, self)
        terminal_status: str | None = None
        try:
            self.executor.direct(handle, compiled)
        except ProviderAborted:
            terminal_status = "cancelled"
        except ProviderError as exc:
            terminal_status = "failed_terminal" if not exc.retryable else "failed_retryable"
            self._last_provider_error = exc

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task_now = connection.execute(
                "SELECT status, commit_id, revision FROM tasks WHERE id = ?",
                (task["id"],),
            ).fetchone()
            status = task_now["status"]
            commit_id = task_now["commit_id"]
            revision = task_now["revision"]
            if not commit_id:
                # No authoritative commit happened. Classify the non-committed
                # terminal state: provider error / abort / silent return.
                if terminal_status:
                    connection.execute(
                        "UPDATE tasks SET status = ? WHERE id = ?",
                        (terminal_status, task["id"]),
                    )
                    self._event(
                        connection,
                        f"task.{terminal_status}",
                        {"task_id": task["id"]},
                    )
                    status = terminal_status
                elif signal.cancelled:
                    connection.execute(
                        "UPDATE tasks SET status = ? WHERE id = ?",
                        ("cancelled", task["id"]),
                    )
                    self._event(connection, "task.cancelled", {"task_id": task["id"]})
                    status = "cancelled"
                elif status == "running":
                    connection.execute(
                        "UPDATE tasks SET status = ? WHERE id = ?",
                        ("failed_terminal", task["id"]),
                    )
                    self._event(
                        connection,
                        "task.failed_terminal",
                        {"task_id": task["id"], "reason": "director_did_not_commit"},
                    )
                    status = "failed_terminal"
            # If commit_id is set, an authoritative commit happened; abort /
            # error after commit cannot un-commit the turn (spec Decision 20–21).

        result = RuntimeResult(task["id"], commit_id, revision, status)
        if commit_id and status in ("projection_pending", "succeeded"):
            return self._project(result)
        return result

    # --- tool implementations (called by engine.tools.ToolRegistry) ---

    def _tool_session_snapshot(self, task, args):
        revision = args.get("revision")
        with self._connect() as connection:
            if revision is None:
                revision = self._active_revision(connection)
            commit_row = connection.execute(
                "SELECT id FROM commits WHERE session_id = ? AND revision = ?",
                (self.session_id, revision),
            ).fetchone()
            task_row = connection.execute(
                "SELECT status FROM tasks WHERE id = ?", (task["id"],)
            ).fetchone()
            recent = connection.execute(
                "SELECT commits.revision, tasks.text, commits.draft FROM commits "
                "JOIN tasks ON tasks.id = commits.task_id "
                "WHERE commits.session_id = ? AND commits.revision <= ? "
                "ORDER BY commits.revision DESC LIMIT 3",
                (self.session_id, revision),
            ).fetchall()
        return ToolResult(
            ok=True,
            value={
                "session_id": self.session_id,
                "active_revision": revision,
                "task_id": task["id"],
                "task_status": task_row["status"] if task_row else None,
                "commit_id": commit_row["id"] if commit_row else None,
                "recent_revisions": [row["revision"] for row in reversed(recent)],
            },
        )

    def _tool_recent_memory(self, task, args):
        max_chars = args.get("max_chars", 3000)
        if max_chars <= 0:
            max_chars = 3000
        project_path = self.card_folder / "memory" / "project.md"
        text = self._recent_memory(project_path) if project_path.exists() else ""
        if len(text) > max_chars:
            text = text[-max_chars:]
        return ToolResult(ok=True, value={"memory": text, "truncated": len(text) == max_chars})

    def _tool_load_worldbook(self, task, args):
        title = args["title"]
        reason = args.get("reason", "")
        try:
            entry = self.load_worldbook_for_task(task["id"], title, reason)
        except ValueError as exc:
            return ToolResult(ok=False, error="worldbook_load_failed", value={"detail": str(exc)})
        return ToolResult(
            ok=True,
            value={
                "title": entry.title,
                "content": entry.content,
                "content_hash": entry.content_hash,
            },
        )

    def _tool_validate_state(self, task, args):
        proposal = args["proposal"]
        base_state = self._state_at_revision(task["base_revision"])
        try:
            commands = extract_commands_for_proposal(proposal)
            new_state, _changes = execute_commands(base_state, commands) if commands else (base_state, {})
        except Exception as exc:
            return ToolResult(ok=False, error="state_proposal_invalid", value={"detail": str(exc)})
        return ToolResult(
            ok=True,
            value={
                "accepted": True,
                "touched_paths": sorted(_collect_changed_paths(base_state, new_state)),
            },
        )

    # --- director-side trace emitters ---

    def _emit_preview(self, task_id, delta_text):
        delta_hash = hashlib.sha256(delta_text.encode("utf-8")).hexdigest()
        with self._connect() as connection:
            self._event(
                connection,
                "narrative.preview.delta",
                {
                    "task_id": task_id,
                    "length": len(delta_text),
                    "delta_hash": delta_hash,
                    "preview": delta_text,
                },
            )

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

    def _emit_model_call_started(self, task_id, meta):
        with self._connect() as connection:
            self._event(
                connection,
                "model_call.started",
                {
                    "task_id": task_id,
                    "call_ordinal": meta["call_ordinal"],
                    "manifest_id": meta.get("manifest_id"),
                    "model": meta.get("model"),
                },
            )

    def _record_model_call(self, task_id, meta):
        call_id = self._id()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO model_calls "
                "(id, session_id, task_id, call_ordinal, manifest_id, model, "
                "prompt_tokens, completion_tokens, total_tokens, stop_reason, latency_ms, "
                "cost_amount, cost_currency, cost_rate_version) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    call_id,
                    self.session_id,
                    task_id,
                    meta["call_ordinal"],
                    meta.get("manifest_id"),
                    meta.get("model"),
                    meta.get("prompt_tokens", 0),
                    meta.get("completion_tokens", 0),
                    meta.get("total_tokens", 0),
                    meta.get("stop_reason", ""),
                    meta.get("latency_ms", 0),
                    meta.get("cost_amount", 0.0),
                    meta.get("cost_currency", "USD"),
                    meta.get("cost_rate_version", "fake-rates-v0"),
                ),
            )
            self._event(
                connection,
                "model_call.finished",
                {
                    "task_id": task_id,
                    "call_ordinal": meta["call_ordinal"],
                    "manifest_id": meta.get("manifest_id"),
                    "model": meta.get("model"),
                    "usage": {
                        "prompt_tokens": meta.get("prompt_tokens", 0),
                        "completion_tokens": meta.get("completion_tokens", 0),
                        "total_tokens": meta.get("total_tokens", 0),
                    },
                    "stop_reason": meta.get("stop_reason", ""),
                    "latency_ms": meta.get("latency_ms", 0),
                    "cost_estimate": {
                        "amount": meta.get("cost_amount", 0.0),
                        "currency": meta.get("cost_currency", "USD"),
                        "rate_version": meta.get("cost_rate_version", "fake-rates-v0"),
                    },
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
        with self._lock:
            with self._connect() as connection:
                commit = connection.execute("SELECT draft FROM commits WHERE id = ?", (result.commit_id,)).fetchone()
                checkpoint = connection.execute(
                    "SELECT state, applied_marker FROM projection_checkpoints WHERE commit_id = ?",
                    (result.commit_id,),
                ).fetchone()
                if checkpoint["state"] == "applied" or checkpoint["applied_marker"] == result.commit_id:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute(
                        "UPDATE projection_checkpoints SET state = ?, applied_marker = ? WHERE commit_id = ?",
                        ("applied", result.commit_id, result.commit_id),
                    )
                    connection.execute("UPDATE tasks SET status = ? WHERE id = ?", ("succeeded", result.task_id))
                    return RuntimeResult(result.task_id, result.commit_id, result.revision, "succeeded")
                draft = TurnDraft.from_json(commit["draft"])
            try:
                self.projection.apply(self._task_text(result.task_id), draft)
            except Exception:
                with self._connect() as connection:
                    connection.execute("UPDATE tasks SET status = ? WHERE id = ?", ("projection_pending", result.task_id))
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

    def _source_snapshot(self, base_revision):
        memory = self.card_folder / "memory"
        initvar_path = self.card_folder / ".initvar.json"
        card_data_path = self.card_folder / ".card_data.json"
        catalog_path = memory / ".worldbook_index.json"
        reference_path = memory / "reference.md"
        user_path = memory / "user.md"
        structure_path = memory / ".card_structure.json"
        project_path = memory / "project.md"
        initvar = self._read_json(initvar_path, {})
        runtime_turns = self._runtime_turns(base_revision)
        current_state = self._state_at_revision(base_revision)
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
            "recent_turns": runtime_turns,
            "sources": {
                "card_facts": self._file_source(card_data_path),
                "settings": {"id": "session_settings", "version": self._hash_bytes(self._canonical(self.session_settings).encode("utf-8"))},
                "worldbook_catalog": self._file_source(catalog_path),
                "card_structure": self._file_source(structure_path),
                "initvar": self._file_source(initvar_path),
                "current_state": {"id": "runtime_state", "version": str(base_revision)},
                "recent_memory": self._file_source(project_path),
                "recent_turns": {"id": "active_lineage", "version": str(base_revision)},
            },
        }

    def _state_at_revision(self, revision):
        if revision == 0:
            return self._read_json(self.card_folder / ".initvar.json", {})
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM state_snapshots WHERE session_id = ? AND revision = ?",
                (self.session_id, revision),
            ).fetchone()
        if not row:
            raise RuntimeError("revision state snapshot is unavailable")
        return json.loads(row["state_json"])

    def _runtime_turns(self, base_revision):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT commits.revision, tasks.text, commits.draft FROM commits JOIN tasks ON tasks.id = commits.task_id "
                "WHERE commits.session_id = ? AND commits.revision <= ? ORDER BY commits.revision DESC LIMIT 3",
                (self.session_id, base_revision),
            ).fetchall()
        return [
            {"revision": row["revision"], "user": row["text"], "assistant": TurnDraft.from_json(row["draft"]).content}
            for row in reversed(rows)
        ]

    def _projected_state(self, base_revision, draft):
        base_state = self._state_at_revision(base_revision)
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
        return RuntimeResult(task["id"], task["commit_id"], task["revision"], task["status"])

    @staticmethod
    def _task_for_key(connection, idempotency_key):
        return connection.execute(
            "SELECT id, commit_id, revision, status, text, base_revision, source_snapshot FROM tasks WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()

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

    def _initialize(self):
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, active_revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
                    text TEXT NOT NULL, status TEXT NOT NULL, commit_id TEXT, revision INTEGER NOT NULL,
                    base_revision INTEGER, source_snapshot TEXT, validation_failures INTEGER NOT NULL DEFAULT 0,
                    validation_exhausted INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS commits (id TEXT PRIMARY KEY, session_id TEXT NOT NULL, revision INTEGER NOT NULL, task_id TEXT NOT NULL UNIQUE, draft TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS projection_checkpoints (commit_id TEXT PRIMARY KEY, state TEXT NOT NULL, applied_marker TEXT);
                CREATE TABLE IF NOT EXISTS state_snapshots (session_id TEXT NOT NULL, revision INTEGER NOT NULL, state_json TEXT NOT NULL, PRIMARY KEY (session_id, revision));
                CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL);
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
            connection.execute("UPDATE tasks SET base_revision = MAX(revision - 1, 0) WHERE base_revision IS NULL")
            connection.execute("UPDATE tasks SET validation_failures = 0 WHERE validation_failures IS NULL")
            connection.execute("UPDATE tasks SET validation_exhausted = 0 WHERE validation_exhausted IS NULL")
            connection.execute("INSERT OR IGNORE INTO schema_migrations (version) VALUES (2)")
            connection.execute("INSERT OR IGNORE INTO sessions (id, active_revision) VALUES (?, 0)", (self.session_id,))

    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
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
