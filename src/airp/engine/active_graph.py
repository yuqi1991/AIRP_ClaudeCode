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


class ActiveGraphSelectionStore:
    """Persist one active Graph id per Project behind a small interface."""

    def __init__(self, workspace=None) -> None:
        runtime_root = getattr(workspace, "runtime_root", None)
        self._path = Path(runtime_root).resolve() / "active_graphs.json" if runtime_root else None
        self._memory: dict[str, str] = {}
        self._lock = threading.RLock()

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
        if not self._path.is_file():
            return {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return {
            project_id: graph_id
            for project_id, graph_id in raw.items()
            if isinstance(project_id, str) and project_id and isinstance(graph_id, str) and graph_id
        }

    def _write(self, selections: dict[str, str]) -> None:
        if self._path is None:
            self._memory = dict(selections)
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=".active-graphs-", suffix=".json", dir=self._path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(selections, handle, ensure_ascii=False, sort_keys=True, indent=2)
                handle.write("\n")
            Path(name).replace(self._path)
        finally:
            Path(name).unlink(missing_ok=True)
