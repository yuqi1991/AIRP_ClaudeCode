"""Card-local save catalog and active Session runtime lifecycle."""

from __future__ import annotations

import copy
import re
import sqlite3
import threading
import time
import uuid
from pathlib import Path

from engine.runtime import SessionTurnRuntime


SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class SessionManagerError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def to_dict(self):
        return {"ok": False, "error": self.code, "message": self.message}


class SessionManager:
    """Own the active runtime while keeping every save in one card-local DB.

    The runtime remains authoritative for each session's opening, revisions,
    tasks and events. This catalog owns only human-facing names, ordering and
    the persisted active-session pointer.
    """

    def __init__(
        self,
        runtime: SessionTurnRuntime,
        runtime_factory,
        *,
        default_opening=None,
        initial_title="主存档",
    ):
        self.database_path = Path(runtime.database_path)
        self._runtime = runtime
        self._runtime_factory = runtime_factory
        self._default_opening = copy.deepcopy(
            default_opening if default_opening is not None else runtime.opening_turn()
        )
        self._lock = threading.RLock()
        self._initialize_catalog(initial_title)

    @property
    def runtime(self) -> SessionTurnRuntime:
        return self._runtime

    @property
    def active_session_id(self) -> str:
        return self._runtime.session_id

    @staticmethod
    def load_active_session_id(database_path, fallback="local") -> str:
        path = Path(database_path)
        if not path.is_file():
            return fallback
        try:
            with sqlite3.connect(path) as connection:
                row = connection.execute(
                    "SELECT runtime_metadata.value FROM runtime_metadata "
                    "JOIN sessions ON sessions.id = runtime_metadata.value "
                    "WHERE runtime_metadata.key = 'active_session_id'"
                ).fetchone()
        except sqlite3.Error:
            return fallback
        value = row[0] if row else fallback
        return value if isinstance(value, str) and SESSION_ID_RE.fullmatch(value) else fallback

    def list_sessions(self) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT sessions.id, sessions.active_revision, "
                "COALESCE(session_catalog.title, sessions.id) AS title, "
                "COALESCE(session_catalog.created_at, 0) AS created_at, "
                "COALESCE(session_catalog.updated_at, 0) AS updated_at "
                "FROM sessions LEFT JOIN session_catalog ON session_catalog.session_id = sessions.id "
                "ORDER BY (sessions.id = ?) DESC, updated_at DESC, created_at DESC, sessions.id",
                (self.active_session_id,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "title": row["title"],
                "active": row["id"] == self.active_session_id,
                "active_revision": int(row["active_revision"] or 0),
                "created_at": int(row["created_at"] or 0),
                "updated_at": int(row["updated_at"] or 0),
            }
            for row in rows
        ]

    def create_session(self, title="新存档") -> dict:
        title = self._validate_title(title)
        with self._lock:
            self._require_idle()
            session_id = "session-" + uuid.uuid4().hex[:12]
            runtime = self._runtime_factory(
                session_id, bootstrap_legacy_history=False
            )
            if self._default_opening is not None:
                runtime.set_opening_turn(
                    self._default_opening,
                    event_type="session.created",
                    event_payload={"title": title},
                )
            runtime.resume_projection()
            now = self._now()
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO session_catalog (session_id, title, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (session_id, title, now, now),
                )
                self._set_active_in_connection(connection, session_id)
            self._runtime = runtime
            return self._session(session_id)

    def switch_session(self, session_id: str) -> dict:
        self._validate_session_id(session_id)
        with self._lock:
            self._require_idle()
            self._require_session(session_id)
            if session_id != self.active_session_id:
                runtime = self._runtime_factory(
                    session_id, bootstrap_legacy_history=False
                )
                runtime.resume_projection()
                with self._connect() as connection:
                    self._set_active_in_connection(connection, session_id)
                    connection.execute(
                        "UPDATE session_catalog SET updated_at = ? WHERE session_id = ?",
                        (self._now(), session_id),
                    )
                self._runtime = runtime
            return self._session(session_id)

    def rename_session(self, session_id: str, title: str) -> dict:
        self._validate_session_id(session_id)
        title = self._validate_title(title)
        with self._lock:
            self._require_session(session_id)
            with self._connect() as connection:
                connection.execute(
                    "UPDATE session_catalog SET title = ?, updated_at = ? WHERE session_id = ?",
                    (title, self._now(), session_id),
                )
            return self._session(session_id)

    def delete_session(self, session_id: str) -> dict:
        self._validate_session_id(session_id)
        with self._lock:
            self._require_idle()
            self._require_session(session_id)
            sessions = self.list_sessions()
            if len(sessions) <= 1:
                raise SessionManagerError(
                    "cannot_delete_only_session", "至少保留一个存档"
                )
            if session_id == self.active_session_id:
                replacement = next(item["id"] for item in sessions if item["id"] != session_id)
                runtime = self._runtime_factory(
                    replacement, bootstrap_legacy_history=False
                )
                runtime.resume_projection()
                self._runtime = runtime
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "DELETE FROM projection_checkpoints WHERE commit_id IN "
                    "(SELECT id FROM commits WHERE session_id = ?)",
                    (session_id,),
                )
                for table in (
                    "model_calls",
                    "worldbook_loads",
                    "context_manifests",
                    "task_attempts",
                    "state_snapshots",
                    "events",
                    "commits",
                    "tasks",
                ):
                    connection.execute(
                        f"DELETE FROM {table} WHERE session_id = ?", (session_id,)
                    )
                connection.execute(
                    "DELETE FROM session_catalog WHERE session_id = ?", (session_id,)
                )
                connection.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
                self._set_active_in_connection(connection, self.active_session_id)
            return {"deleted_session_id": session_id}

    def touch_active(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE session_catalog SET updated_at = ? WHERE session_id = ?",
                (self._now(), self.active_session_id),
            )

    def _initialize_catalog(self, initial_title):
        now = self._now()
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS session_catalog (
                    session_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runtime_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO session_catalog "
                "(session_id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (self.active_session_id, self._validate_title(initial_title), now, now),
            )
            self._set_active_in_connection(connection, self.active_session_id)

    def _session(self, session_id):
        return next(item for item in self.list_sessions() if item["id"] == session_id)

    def _require_idle(self):
        if self._runtime.generation_active():
            raise SessionManagerError(
                "generation_active", "生成进行中，完成或取消后才能切换存档"
            )

    def _require_session(self, session_id):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise SessionManagerError("unknown_session", "存档不存在")

    @staticmethod
    def _validate_session_id(session_id):
        if not isinstance(session_id, str) or not SESSION_ID_RE.fullmatch(session_id):
            raise SessionManagerError("invalid_session_id", "存档 ID 无效")

    @staticmethod
    def _validate_title(title):
        if not isinstance(title, str) or not title.strip():
            raise SessionManagerError("invalid_session_title", "存档名称不能为空")
        title = title.strip()
        if len(title) > 60:
            raise SessionManagerError("invalid_session_title", "存档名称不能超过 60 个字符")
        return title

    @staticmethod
    def _now():
        return int(time.time() * 1000)

    def _set_active_in_connection(self, connection, session_id):
        connection.execute(
            "INSERT INTO runtime_metadata (key, value) VALUES ('active_session_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (session_id,),
        )

    def _connect(self):
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection
