import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

import handler


@dataclass(frozen=True)
class TurnDraft:
    content: str
    summary: str = ""
    options: str = ""

    def to_json(self):
        return json.dumps(self.__dict__, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw):
        return cls(**json.loads(raw))


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

    def run(self, text):
        return self._draft


class LegacyProjectionAdapter:
    def __init__(self, card_folder, projection_root):
        self.card_folder = Path(card_folder)
        self.projection_root = Path(projection_root)

    def apply(self, text, draft):
        backups = self._backup()
        try:
            handler.append_turn(
                self.card_folder,
                polished_input=text,
                content=draft.content,
                summary=draft.summary,
                options=draft.options,
                full_text=draft.content,
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
    def __init__(self, database_path, card_folder, projection_root, executor, session_id="local"):
        self.database_path = Path(database_path)
        self.card_folder = Path(card_folder)
        self.executor = executor
        self.session_id = session_id
        self.projection = LegacyProjectionAdapter(card_folder, projection_root)
        self._lock = threading.RLock()
        self._initialize()

    def submit(self, text, idempotency_key):
        if not text.strip():
            raise ValueError("empty input")
        with self._lock:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                task = self._task_for_key(connection, idempotency_key)
                if task:
                    result = self._result(task)
                else:
                    task_id = self._id()
                    connection.execute(
                        "INSERT INTO tasks (id, session_id, idempotency_key, text, status, revision) VALUES (?, ?, ?, ?, ?, ?)",
                        (task_id, self.session_id, idempotency_key, text, "queued", 0),
                    )
                    self._event(connection, "player_message.submitted", {"task_id": task_id, "text": text})
                    self._event(connection, "task.queued", {"task_id": task_id})
                    connection.execute("UPDATE tasks SET status = ? WHERE id = ?", ("running", task_id))
                    self._event(connection, "task.running", {"task_id": task_id})
                    draft = self.executor.run(text)
                    revision = self._next_revision(connection)
                    commit_id = self._id()
                    connection.execute(
                        "INSERT INTO commits (id, session_id, revision, task_id, draft) VALUES (?, ?, ?, ?, ?)",
                        (commit_id, self.session_id, revision, task_id, draft.to_json()),
                    )
                    connection.execute(
                        "INSERT INTO projection_checkpoints (commit_id, state) VALUES (?, ?)",
                        (commit_id, "pending"),
                    )
                    connection.execute(
                        "UPDATE sessions SET active_revision = ? WHERE id = ?", (revision, self.session_id)
                    )
                    connection.execute(
                        "UPDATE tasks SET status = ?, commit_id = ?, revision = ? WHERE id = ?",
                        ("projection_pending", commit_id, revision, task_id),
                    )
                    self._event(
                        connection,
                        "turn.committed",
                        {"task_id": task_id, "commit_id": commit_id, "revision": revision},
                    )
                    result = RuntimeResult(task_id=task_id, commit_id=commit_id, revision=revision, status="projection_pending")
            return self._project(result)

    def active_revision(self):
        with self._connect() as connection:
            return connection.execute(
                "SELECT active_revision FROM sessions WHERE id = ?", (self.session_id,)
            ).fetchone()["active_revision"]

    def task(self, task_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT id, commit_id, revision, status FROM tasks WHERE id = ?", (task_id,)
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

    def _project(self, result):
        if result.status == "succeeded":
            return result
        with self._lock:
            with self._connect() as connection:
                commit = connection.execute(
                    "SELECT draft FROM commits WHERE id = ?", (result.commit_id,)
                ).fetchone()
                checkpoint = connection.execute(
                    "SELECT state FROM projection_checkpoints WHERE commit_id = ?", (result.commit_id,)
                ).fetchone()
                if checkpoint["state"] == "applied":
                    return RuntimeResult(result.task_id, result.commit_id, result.revision, "succeeded")
                draft = TurnDraft.from_json(commit["draft"])
            try:
                self.projection.apply(self._task_text(result.task_id), draft)
            except Exception:
                with self._connect() as connection:
                    connection.execute(
                        "UPDATE tasks SET status = ? WHERE id = ?", ("projection_pending", result.task_id)
                    )
                raise
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE projection_checkpoints SET state = ? WHERE commit_id = ?", ("applied", result.commit_id)
                )
                connection.execute("UPDATE tasks SET status = ? WHERE id = ?", ("succeeded", result.task_id))
                self._event(connection, "task.succeeded", {"task_id": result.task_id, "commit_id": result.commit_id})
            return RuntimeResult(result.task_id, result.commit_id, result.revision, "succeeded")

    def _task_text(self, task_id):
        with self._connect() as connection:
            return connection.execute("SELECT text FROM tasks WHERE id = ?", (task_id,)).fetchone()["text"]

    def _next_revision(self, connection):
        return connection.execute(
            "SELECT active_revision FROM sessions WHERE id = ?", (self.session_id,)
        ).fetchone()["active_revision"] + 1

    @staticmethod
    def _result(task):
        return RuntimeResult(
            task_id=task["id"],
            commit_id=task["commit_id"],
            revision=task["revision"],
            status=task["status"],
        )

    @staticmethod
    def _task_for_key(connection, idempotency_key):
        return connection.execute(
            "SELECT id, commit_id, revision, status FROM tasks WHERE idempotency_key = ?", (idempotency_key,)
        ).fetchone()

    def _initialize(self):
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    active_revision INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    text TEXT NOT NULL,
                    status TEXT NOT NULL,
                    commit_id TEXT,
                    revision INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS commits (
                    id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    task_id TEXT NOT NULL UNIQUE,
                    draft TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS projection_checkpoints (
                    commit_id TEXT PRIMARY KEY,
                    state TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    type TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO sessions (id, active_revision) VALUES (?, 0)",
                (self.session_id,),
            )

    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _event(self, connection, event_type, payload):
        connection.execute(
            "INSERT INTO events (session_id, type, payload) VALUES (?, ?, ?)",
            (self.session_id, event_type, json.dumps(payload, ensure_ascii=False)),
        )

    @staticmethod
    def _id():
        return str(uuid.uuid4())
