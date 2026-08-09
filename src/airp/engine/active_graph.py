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


class ActiveGraphSelectionClaim:
    """Proof that one Project selection write is still current for one store scope."""

    def __init__(
        self,
        scope: object,
        project_id: str,
        graph_id: str,
        generation: int,
    ) -> None:
        self._scope = scope
        self.project_id = project_id
        self.graph_id = graph_id
        self._generation = generation


class ActiveGraphSelectionError(ValueError):
    """A stable Active Graph selection persistence failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ActiveGraphSelectionStore:
    """Persist one active Graph id per Project behind a small interface."""

    _path_locks_guard = threading.Lock()
    _path_locks: dict[Path, threading.RLock] = {}
    _mutation_generations: dict[tuple[object, str], int] = {}

    def __init__(self, workspace=None) -> None:
        selection_path = getattr(workspace, "active_graph_selections_path", None)
        self._path = Path(selection_path).resolve() if selection_path is not None else None
        self._memory: dict[str, str] = {}
        self._scope = self._path if self._path is not None else object()
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
            self._advance_generation(project_id)
        return graph_id

    def select_if_unset(self, project_id: str, graph_id: str) -> bool:
        """Select ``graph_id`` only when the Project has no selection."""
        return self.select_if_unset_claim(project_id, graph_id) is not None

    def select_if_unset_claim(
        self, project_id: str, graph_id: str
    ) -> ActiveGraphSelectionClaim | None:
        """Select once and return proof suitable for exact compensation."""
        self._validate_project_id(project_id)
        if not isinstance(graph_id, str) or not graph_id.strip():
            raise ValueError("graph_id must be a non-empty string")
        graph_id = graph_id.strip()
        with self._lock:
            selections = self._read()
            if project_id in selections:
                return None
            selections[project_id] = graph_id
            self._write(selections)
            generation = self._advance_generation(project_id)
            return ActiveGraphSelectionClaim(
                self._scope, project_id, graph_id, generation
            )

    def clear(self, project_id: str) -> None:
        self._validate_project_id(project_id)
        with self._lock:
            selections = self._read()
            if project_id not in selections:
                return
            selections.pop(project_id, None)
            self._write(selections)
            self._advance_generation(project_id)

    def clear_if_equals(self, project_id: str, graph_id: str) -> bool:
        """Clear a Project selection only while it still equals ``graph_id``."""
        self._validate_project_id(project_id)
        if not isinstance(graph_id, str) or not graph_id.strip():
            raise ValueError("graph_id must be a non-empty string")
        graph_id = graph_id.strip()
        with self._lock:
            selections = self._read()
            if selections.get(project_id) != graph_id:
                return False
            selections.pop(project_id)
            self._write(selections)
            self._advance_generation(project_id)
            return True

    def clear_claim(self, claim: ActiveGraphSelectionClaim) -> bool:
        """Clear only if no writer has replaced the claimed Project selection."""
        if not isinstance(claim, ActiveGraphSelectionClaim):
            raise TypeError("claim must be an ActiveGraphSelectionClaim")
        with self._lock:
            if claim._scope != self._scope:
                return False
            if self._generation_for(claim.project_id) != claim._generation:
                return False
            selections = self._read()
            if selections.get(claim.project_id) != claim.graph_id:
                return False
            selections.pop(claim.project_id)
            self._write(selections)
            self._advance_generation(claim.project_id)
            return True

    def _generation_for(self, project_id: str) -> int:
        return self._mutation_generations.get((self._scope, project_id), 0)

    def _advance_generation(self, project_id: str) -> int:
        generation = self._generation_for(project_id) + 1
        self._mutation_generations[(self._scope, project_id)] = generation
        return generation

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
                for project_id in affected:
                    self._advance_generation(project_id)
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
