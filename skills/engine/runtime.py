import copy
import hashlib
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

import handler
from engine.context_compiler import (
    CompiledContext,
    ContextCompileRequest,
    ContextPolicy,
    compile_context,
    compile_sequential_handoff_context,
    replay_payload,
)
from engine.director import DirectorHandle, NarrativeDirector
from engine.mvu import execute_commands, extract_commands, generate_schema, validate_command_strict
from engine.provider import AbortSignal, ProviderAborted, ProviderError
from engine.quality import DefaultQualityGate, QualityContext, QualityGate, QualityPolicy
from engine.runtime_config import prompt_preset_from_snapshot, runtime_config_manifest
from engine.tools import ToolResult, ToolRegistry, validate_draft_dict
from engine.turn_parser import parse_turn_text
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


@dataclass(frozen=True)
class CommitLineage:
    """Public read model for a commit's place in the revision DAG."""

    id: str
    revision: int
    task_id: str
    parent_revision: int
    text: str = ""


class FakeNarrativeExecutor:
    def __init__(self, content, summary="", options=""):
        self._draft = TurnDraft(content=content, summary=summary, options=options)

    def run(self, text, compiled_context=None):
        return self._draft


class MultiTurnFakeExecutor:
    """Reusable deterministic fake executor for mock/runtime smoke flows."""

    def __init__(self):
        self._turn = 0

    def run(self, text, compiled_context=None):
        self._turn += 1
        hour = min(23, 9 + self._turn)
        polished = (text or '').strip() or f'玩家行动 {self._turn}'
        return TurnDraft(
            polished_input=polished,
            content=f'<p>Mock 回合 {self._turn}：{polished}</p>',
            summary=f'Mock 回合 {self._turn}',
            options='<font color="#5a7a5a">继续行动</font>',
            mvu_commands=f"_.set('世界.时间', '1月1日 {hour:02d}:00');",
        )


class LegacyProjectionAdapter:
    def __init__(self, card_folder, projection_root):
        self.card_folder = Path(card_folder)
        self.projection_root = Path(projection_root)

    def apply(self, text, draft, tokens=None):
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
                tokens=tokens,
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
        runtime_config_store=None,
        executor_factory=None,
    ):
        self.database_path = Path(database_path)
        self.card_folder = Path(card_folder)
        self.executor = executor
        self.session_id = session_id
        self.manifest_policy = manifest_policy or ContextPolicy(version="runtime-v1", token_budget=8000)
        self.session_settings = json.loads(self._canonical(session_settings or {}))
        self.runtime_config_store = runtime_config_store
        self.executor_factory = executor_factory
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
        # Create/lookup under the session lock. For NarrativeDirector runs we
        # release the lock before the long-running body so concurrent rollback
        # can move the active head (Ticket 06 branch-stale). Fake/deterministic
        # executors stay serialized under the lock so duplicate concurrent
        # submits with the same key still collapse to one commit.
        with self._lock:
            task = self._create_or_get_task(text, idempotency_key)
            result = self._result(task)
            if result.commit_id:
                return self._project(result)
            if result.status == "succeeded":
                return result

            executor = self._executor_for_task(task)
            is_director = isinstance(executor, NarrativeDirector)
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
            else:
                try:
                    compiled = self._compile_and_persist(task)
                    draft = self._execute(executor, text, compiled)
                    result = self._commit_draft(task, draft)
                    return self._project(result)
                finally:
                    pass

        # Director path — lock released so rollback/stop can interleave.
        try:
            compiled = self._compile_and_persist(task)
            return self._run_director(task, text, compiled, signal, executor)
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
        """Return the active branch head revision (what new turns build on)."""
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
        source_snapshot = self._source_snapshot(parent_revision)
        project_existing = None
        task = None
        signal = None
        is_director = False
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
                            idempotency_key,
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

            executor = self._executor_for_task(task)
            is_director = isinstance(executor, NarrativeDirector)
            if is_director:
                signal = AbortSignal()
                self._abort_signals[task["id"]] = signal
            else:
                compiled = self._compile_and_persist(task)
                draft = self._execute(executor, text, compiled)
                result = self._commit_draft(task, draft)
                return self._project(result)

        # Director path — lock released so concurrent commands can interleave.
        try:
            compiled = self._compile_and_persist(task)
            return self._run_director(task, text, compiled, signal, executor)
        finally:
            if signal is not None:
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
        # Snapshot reads open their own connections; never nest them under
        # BEGIN IMMEDIATE or the process deadlocks against itself. Loop until
        # the base we snapshotted still matches the head at insert time.
        while True:
            with self._connect() as connection:
                task = self._task_for_key(connection, idempotency_key)
                if task:
                    return task
                base_revision = self._active_revision(connection)
            source_snapshot = self._source_snapshot(base_revision)
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
                    "INSERT INTO tasks (id, session_id, idempotency_key, text, status, revision, base_revision, source_snapshot) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task_id,
                        self.session_id,
                        idempotency_key,
                        text,
                        "queued",
                        0,
                        base_revision,
                        self._canonical(source_snapshot),
                    ),
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
                    preset=prompt_preset_from_snapshot(snapshot.get("runtime_config")),
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
                        preset=prompt_preset_from_snapshot(snapshot.get("runtime_config")),
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
            preset=prompt_preset_from_snapshot(snapshot.get("runtime_config")),
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

    def _executor_for_task(self, task):
        if self.executor_factory is None:
            return self.executor
        snapshot = json.loads(task["source_snapshot"]) if task["source_snapshot"] else {}
        return self.executor_factory(snapshot.get("runtime_config"))

    @staticmethod
    def _execute(executor, text, compiled):
        return executor.run(text, compiled)

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
                # Preload outside nested connection use: we already hold the write
                # txn, so compute from the task's frozen base via a pure helper.
                # base_state was snapshotted at task creation; re-read is fine for
                # committed revisions (immutable snapshots). Use connection-local
                # read of state_snapshots to avoid a second sqlite connection.
                base_rev = task["base_revision"]
                if base_rev == 0:
                    base_state = self._read_json(self.card_folder / ".initvar.json", {})
                else:
                    snap = connection.execute(
                        "SELECT state_json FROM state_snapshots WHERE session_id = ? AND revision = ?",
                        (self.session_id, base_rev),
                    ).fetchone()
                    if not snap:
                        return ToolResult(ok=False, error="revision state snapshot is unavailable")
                    base_state = json.loads(snap["state_json"])
                projected_state = self._projected_state_from_base(base_state, draft)
                validation_error = self._validate_draft_before_commit(
                    connection, task, draft, base_state=base_state
                )
                if validation_error is not None:
                    return ToolResult(ok=False, error=validation_error)
                commit_id, revision = self._perform_commit_in_connection(
                    connection, task, draft, projected_state=projected_state
                )
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

    def _run_director(self, task, text, compiled, signal, executor):
        # If a concurrent stop() cancelled this task during context compilation
        # (or before the director body started), finalize as cancelled without
        # running the director — no commit, no preview promotion.
        if signal.cancelled or self._task_status(task["id"]) == "cancelled":
            return self._finalize_cancelled(task["id"])
        tools = ToolRegistry(self, task, self.manifest_policy)
        handle = DirectorHandle(task["id"], text, tools, signal, self)
        terminal_status: str | None = None
        # Harness-owned commit (ADR-0011): the director produces narrative text;
        # this loop parses → validates → commits. Bounded re-entry on quality/MVU
        # rejection so a bad first draft can be corrected within the same task.
        snapshot = json.loads(task["source_snapshot"]) if task["source_snapshot"] else {}
        graph = ((snapshot.get("runtime_config") or {}).get("graph") or {})
        max_attempts = max(1, int(graph.get("commit_validation_retries") or self.max_commit_validation_retries))
        for attempt in range(max_attempts):
            if signal.cancelled or self._task_status(task["id"]) == "cancelled":
                return self._finalize_cancelled(task["id"])
            # A prior attempt may have already committed (idempotent) — stop.
            existing = self.task(task["id"])
            if existing and existing.commit_id:
                return self._project(existing)
            try:
                executor.direct(handle, compiled)
            except ProviderAborted:
                terminal_status = "cancelled"
                break
            except ProviderError as exc:
                terminal_status = "failed_terminal" if not exc.retryable else "failed_retryable"
                self._last_provider_error = exc
                break

            if signal.cancelled:
                terminal_status = "cancelled"
                break

            final_text = handle.take_final_text()
            if final_text is None:
                # Director returned without setting text (e.g. pure abort path
                # inside ScriptedDirector wait_for_aborted). Do not invent content.
                break

            draft = parse_turn_text(final_text, fallback_input=text)
            commit_result = self._commit_parsed_draft(task, draft)
            if commit_result.ok:
                # Authoritative commit landed (or was reused). Project and return.
                value = commit_result.value or {}
                result = RuntimeResult(
                    task["id"],
                    value.get("commit_id"),
                    value.get("revision", task["base_revision"]),
                    "projection_pending",
                )
                return self._project(result)

            error = commit_result.error or "commit_failed"
            # Stale / exhausted / hard failures are not retriable via re-generation.
            if error in ("stale_revision", "quality_exhausted", "commit_failed"):
                break
            if error in ("quality_gate_failed", "mvu_validation_failed"):
                # Feed rejection back; re-enter director for a corrected draft.
                handle.set_commit_feedback(error, (commit_result.value or {}))
                # Re-check exhaustion flag set by _reject_precommit.
                status_now = self._task_status(task["id"])
                if status_now == "quality_exhausted":
                    break
                continue
            # Unknown non-ok: stop retrying.
            break

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            task_now = connection.execute(
                "SELECT status, commit_id, revision, validation_exhausted FROM tasks WHERE id = ?",
                (task["id"],),
            ).fetchone()
            status = task_now["status"]
            commit_id = task_now["commit_id"]
            revision = task_now["revision"]
            if not commit_id:
                # No authoritative commit happened. Classify the non-committed
                # terminal state: provider error / abort / quality / empty text.
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
                elif status in (
                    "quality_exhausted",
                    "quality_gate_failed",
                    "mvu_validation_failed",
                    "stale_revision",
                ):
                    # Already classified by the commit path; leave as-is.
                    pass
                elif status == "running":
                    # Empty / unusable final text after director return → treat as
                    # quality failure (content empty), NOT a flow hang. Prefer
                    # quality_exhausted when retries are spent.
                    terminal = "quality_exhausted"
                    connection.execute(
                        "UPDATE tasks SET status = ?, validation_exhausted = 1 WHERE id = ?",
                        (terminal, task["id"]),
                    )
                    self._event(
                        connection,
                        f"task.{terminal}",
                        {
                            "task_id": task["id"],
                            "reason": "empty_or_missing_narrative",
                        },
                    )
                    status = terminal
            # If commit_id is set, an authoritative commit happened; abort /
            # error after commit cannot un-commit the turn (spec Decision 20–21).

        result = RuntimeResult(task["id"], commit_id, revision, status)
        if commit_id and status in ("projection_pending", "succeeded"):
            return self._project(result)
        return result

    def _commit_parsed_draft(self, task, draft: TurnDraft) -> ToolResult:
        """Harness-internal single-write commit (ADR-0011).

        Same optimistic revision + per-task idempotency + quality/MVU gates as
        the former model-facing ``commit_turn_draft`` tool. ``expected_revision``
        is always the task's frozen ``base_revision`` — the harness owns the
        comparison, the model does not.
        """
        draft_dict = {
            "content": draft.content,
            "summary": draft.summary,
            "options": draft.options,
            "polished_input": draft.polished_input,
            "mvu_commands": draft.mvu_commands,
        }
        # Shape check before domain work (empty content → quality path via gate,
        # but validate_draft_dict also rejects empty; map to quality_gate_failed
        # so the bounded-retry loop treats it uniformly).
        try:
            validate_draft_dict(draft_dict)
        except Exception as exc:  # _ToolError or similar
            code = getattr(exc, "code", None) or str(exc)
            if "draft_content_empty" in code or "draft_not_object" in code:
                # Record as a quality rejection against the task so exhaustion
                # accounting stays consistent with DefaultQualityGate failures.
                with self._connect() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    terminal = self._reject_precommit(
                        connection,
                        task,
                        "quality_gate_failed",
                        details={"reasons": ["content_empty"], "source": "harness_parse"},
                    )
                return ToolResult(ok=False, error=terminal)
            return ToolResult(ok=False, error=code)
        return self._commit_via_tool(task, draft_dict, task["base_revision"])

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
        # Active-lineage only — walk parent chain from the requested revision.
        recent = self._runtime_turns(revision, limit=3)
        return ToolResult(
            ok=True,
            value={
                "session_id": self.session_id,
                "active_revision": revision,
                "task_id": task["id"],
                "task_status": task_row["status"] if task_row else None,
                "commit_id": commit_row["id"] if commit_row else None,
                "recent_revisions": [row["revision"] for row in recent],
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
        runtime_config = None
        settings = self.session_settings
        if self.runtime_config_store is not None:
            runtime_config = self.runtime_config_store.freeze().data
            settings = runtime_config.get("settings") or settings
        return {
            "card_facts": self._read_json(card_data_path, {}),
            "settings": settings,
            "worldbook_catalog": self._read_json(catalog_path, []),
            "worldbook_reference": self._read_text(reference_path),
            "worldbook_user": self._read_text(user_path),
            "card_structure": self._read_json(structure_path, {}),
            "initvar": initvar,
            "current_state": current_state,
            "recent_memory": self._recent_memory(project_path),
            "recent_turns": runtime_turns,
            "runtime_config": runtime_config,
            "runtime_config_manifest": runtime_config_manifest(runtime_config),
            "sources": {
                "card_facts": self._file_source(card_data_path),
                "settings": {"id": "runtime_settings" if runtime_config is not None else "session_settings", "version": self._hash_bytes(self._canonical(settings).encode("utf-8"))},
                "worldbook_catalog": self._file_source(catalog_path),
                "card_structure": self._file_source(structure_path),
                "initvar": self._file_source(initvar_path),
                "current_state": {"id": "runtime_state", "version": str(base_revision)},
                "recent_memory": self._file_source(project_path),
                "recent_turns": {"id": "active_lineage", "version": str(base_revision)},
            },
        }

    def _policy_for_snapshot(self, snapshot):
        runtime_config = snapshot.get("runtime_config") if isinstance(snapshot, dict) else None
        preset = runtime_config.get("preset") if isinstance(runtime_config, dict) else None
        budget = preset.get("token_budget") if isinstance(preset, dict) else None
        if not isinstance(budget, int) or budget <= 0:
            return self.manifest_policy
        return ContextPolicy(
            version=f"{self.manifest_policy.version}:{runtime_config.get('preset_id', 'default')}",
            token_budget=budget,
            narrative_policy=self.manifest_policy.narrative_policy,
            max_worldbook_loads=self.manifest_policy.max_worldbook_loads,
        )

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

    def capture_opening_from_chat_log(self):
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
        return opening is not None

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
        self.capture_opening_from_chat_log()
        self._rebuild_active_projections(self.active_revision())

    def _rebuild_active_projections(self, head_revision):
        """Rewrite compatibility files from opening + active lineage + head state."""
        from engine.card import write_chat_log, write_state

        projection_log = self._projection_log(head_revision)
        state_js = self._state_js_for_projection(projection_log, head_revision)
        backups = self.projection._backup()
        try:
            write_chat_log(self.card_folder, [])
            for item in self._lineage_commits(head_revision):
                self.projection.apply(item["text"], item["draft"], tokens=item.get("tokens"))
            if self._opening_entry() is not None:
                write_chat_log(self.card_folder, projection_log)
                handler.write_content_js(self.card_folder, projection_root=self.projection.projection_root)
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
            "user": draft.polished_input or item.get("text") or "",
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
            draft = parse_turn_text(turn.get("ai", ""), fallback_input=turn.get("user", ""))
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
                "SELECT id, commit_id, revision, status FROM tasks WHERE session_id = ? AND status IN ('projection_pending', 'running', 'queued')",
                (self.session_id,),
            ).fetchall()
            for row in rows:
                if row["status"] == "projection_pending" and row["commit_id"]:
                    pending.append(RuntimeResult(row["id"], row["commit_id"], row["revision"], "projection_pending"))
                elif row["status"] in ("running", "queued") and not row["commit_id"]:
                    recovered_status = f'abandoned_{row["status"]}'
                    connection.execute(
                        "UPDATE tasks SET status = ? WHERE id = ?",
                        (recovered_status, row["id"]),
                    )
                    self._event(connection, recovered_status, {"task_id": row["id"], "recovered_on_startup": True})
        for result in pending:
            try:
                self._project(result)
            except Exception:
                pass

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
                CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, active_revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
                    text TEXT NOT NULL, status TEXT NOT NULL, commit_id TEXT, revision INTEGER NOT NULL,
                    base_revision INTEGER, source_snapshot TEXT, validation_failures INTEGER NOT NULL DEFAULT 0,
                    validation_exhausted INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS commits (
                    id TEXT PRIMARY KEY, session_id TEXT NOT NULL, revision INTEGER NOT NULL,
                    task_id TEXT NOT NULL UNIQUE, draft TEXT NOT NULL, parent_revision INTEGER NOT NULL DEFAULT 0
                );
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
            commit_columns = {row["name"] for row in connection.execute("PRAGMA table_info(commits)")}
            if "parent_revision" not in commit_columns:
                connection.execute(
                    "ALTER TABLE commits ADD COLUMN parent_revision INTEGER NOT NULL DEFAULT 0"
                )
            session_columns = {row["name"] for row in connection.execute("PRAGMA table_info(sessions)")}
            if "opening_turn_json" not in session_columns:
                connection.execute("ALTER TABLE sessions ADD COLUMN opening_turn_json TEXT")
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
            connection.execute("INSERT OR IGNORE INTO sessions (id, active_revision) VALUES (?, 0)", (self.session_id,))
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
