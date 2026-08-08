"""Active Studio Graph selection, independent of Project content.

Projects own cards and Worldbook bindings.  Selecting which reusable Graph
executes a Project is runtime state, so this module persists that one choice
outside both Project definitions and the retired Runtime Config format.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path


class ActiveGraphSelectionError(ValueError):
    """A stable Active Graph selection persistence failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ActiveGraphSelectionStore:
    """Persist one active Graph id per Project behind a small interface."""

    _path_locks_guard = threading.Lock()
    _path_locks: dict[Path, threading.RLock] = {}

    def __init__(self, workspace=None) -> None:
        selection_path = getattr(workspace, "active_graph_selections_path", None)
        self._path = Path(selection_path).resolve() if selection_path is not None else None
        self._memory: dict[str, str] = {}
        self._lock = self._lock_for_path(self._path) if self._path is not None else threading.RLock()

    @classmethod
    def _lock_for_path(cls, path: Path) -> threading.RLock:
        with cls._path_locks_guard:
            return cls._path_locks.setdefault(path, threading.RLock())

    def graph_id_for(self, project_id: str) -> str | None:
        self._validate_project_id(project_id)
        with self._lock:
            return self._read().get(project_id)

    def select(self, project_id: str, graph_id: str) -> str:
        self._validate_project_id(project_id)
        if not isinstance(graph_id, str) or not graph_id.strip():
            raise ValueError("graph_id must be a non-empty string")
        graph_id = graph_id.strip()
        with self._lock:
            selections = self._read()
            selections[project_id] = graph_id
            self._write(selections)
        return graph_id

    def select_if_unset(self, project_id: str, graph_id: str) -> bool:
        """Select ``graph_id`` only when the Project has no selection."""
        self._validate_project_id(project_id)
        if not isinstance(graph_id, str) or not graph_id.strip():
            raise ValueError("graph_id must be a non-empty string")
        graph_id = graph_id.strip()
        with self._lock:
            selections = self._read()
            if project_id in selections:
                return False
            selections[project_id] = graph_id
            self._write(selections)
            return True

    def clear(self, project_id: str) -> None:
        self._validate_project_id(project_id)
        with self._lock:
            selections = self._read()
            if project_id not in selections:
                return
            selections.pop(project_id, None)
            self._write(selections)

    def clear_graph(self, graph_id: str) -> tuple[str, ...]:
        """Clear every Project selection that points to a deleted Graph."""
        if not isinstance(graph_id, str) or not graph_id.strip():
            raise ValueError("graph_id must be a non-empty string")
        graph_id = graph_id.strip()
        with self._lock:
            selections = self._read()
            affected = tuple(
                project_id
                for project_id, selected_graph_id in selections.items()
                if selected_graph_id == graph_id
            )
            if affected:
                for project_id in affected:
                    selections.pop(project_id, None)
                self._write(selections)
            return affected

    @staticmethod
    def _validate_project_id(project_id: str) -> None:
        if not isinstance(project_id, str) or not project_id.strip():
            raise ValueError("project_id must be a non-empty string")

    def _read(self) -> dict[str, str]:
        if self._path is None:
            return dict(self._memory)
        try:
            serialized = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {}
        except (OSError, UnicodeDecodeError) as exc:
            raise ActiveGraphSelectionError(
                "active_graph_selection_unreadable",
                "cannot read active Graph selections",
            ) from exc
        try:
            raw = json.loads(serialized)
        except json.JSONDecodeError as exc:
            raise ActiveGraphSelectionError(
                "active_graph_selection_unreadable",
                "cannot read active Graph selections",
            ) from exc
        if not isinstance(raw, dict) or not all(
            isinstance(project_id, str)
            and bool(project_id.strip())
            and isinstance(graph_id, str)
            and bool(graph_id.strip())
            for project_id, graph_id in raw.items()
        ):
            raise ActiveGraphSelectionError(
                "active_graph_selection_unreadable",
                "cannot read active Graph selections",
            )
        return dict(raw)

    def _write(self, selections: dict[str, str]) -> None:
        if self._path is None:
            self._memory = dict(selections)
            return
        temporary: str | None = None
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                prefix=".active-graphs-",
                suffix=".json",
                dir=self._path.parent,
            )
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(selections, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
            temporary = None
        except OSError as exc:
            raise ActiveGraphSelectionError(
                "active_graph_selection_write_failed",
                "cannot write active Graph selections",
            ) from exc
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
