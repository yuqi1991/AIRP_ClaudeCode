"""Runtime contexts for switching between Workspace Projects.

The legacy runtime still reads a card-shaped directory for compatibility.  A
Project is the source of truth, so this module materializes the small card
projection needed by :class:`SessionTurnRuntime` and gives every Project its
own Session database and active-session pointer.
"""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from airp.host.rp.session_manager import SessionManager
from airp.host.rp.session_runtime import SessionTurnRuntime


class ProjectRuntimeError(RuntimeError):
    """A typed failure before a runtime discard becomes irreversible."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class ProjectRuntimeContext:
    project_id: str
    runtime: SessionTurnRuntime
    sessions: SessionManager
    card_folder: Path
    database_path: Path


@dataclass
class ProjectRuntimeDiscard:
    owner: object
    project_id: str
    context: ProjectRuntimeContext | None
    moves: tuple[tuple[Path, Path], ...]
    previous_state: dict
    finalized: bool = False


class ProjectRuntimeStore:
    """Create and restore one runtime/session context per Project."""

    def __init__(
        self,
        project_store,
        *,
        workspace=None,
        static_root: str | Path | None = None,
        projection_root: str | Path | None = None,
        initial_runtime: SessionTurnRuntime,
        initial_sessions: SessionManager | None = None,
    ) -> None:
        self.project_store = project_store
        self.workspace = workspace
        self.static_root = Path(static_root).resolve() if static_root else None
        self.projection_root = Path(projection_root).resolve()
        if workspace is not None:
            self.state_root = Path(workspace.runtime_root).resolve() / "projects"
            self.sessions_root = Path(workspace.sessions_root).resolve() / "projects"
        else:
            fallback = self.static_root or Path.cwd()
            self.state_root = fallback / ".airp-runtime" / "projects"
            self.sessions_root = fallback / ".airp-runtime" / "sessions"
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        self._active_path = self.state_root.parent / "active_project.json"
        self._contexts: dict[str, ProjectRuntimeContext] = {}
        self._lock = threading.RLock()
        self._initial_runtime = initial_runtime
        self._initial_sessions = initial_sessions
        self._discard_owner = object()

        # A CLI-launched card may already have a durable runtime. Reuse it for
        # the matching Project so existing saves remain available.
        try:
            self.project_store.get_project(initial_runtime.project_id)
        except Exception:
            return
        self._contexts[initial_runtime.project_id] = ProjectRuntimeContext(
            project_id=initial_runtime.project_id,
            runtime=initial_runtime,
            sessions=initial_sessions or self._session_manager(initial_runtime, initial_runtime.project_id),
            card_folder=Path(initial_runtime.card_folder),
            database_path=Path(initial_runtime.database_path),
        )

    def project_ids(self) -> set[str]:
        try:
            return {item["id"] for item in self.project_store.list_projects()}
        except Exception:
            return set()

    def restore_project_id(self, preferred: str | None = None) -> str | None:
        """Choose the saved active Project, with a deterministic fallback."""
        projects = self.project_store.list_projects()
        if not projects:
            return None
        valid = {item["id"] for item in projects}
        state = self._read_state()
        candidates = [state.get("active_project_id"), preferred]
        candidates.extend(state.get("recent_project_ids", []))
        for project_id in candidates:
            if project_id in valid:
                return project_id
        return max(
            projects,
            key=lambda item: (int(item.get("updated_at") or 0), str(item.get("name", "")).casefold(), item["id"]),
        )["id"]

    def get(self, project_id: str) -> ProjectRuntimeContext:
        with self._lock:
            context = self._contexts.get(project_id)
            if context is not None:
                self._materialize_project(self.project_store.get_project(project_id), context.card_folder)
                return context
            project = self.project_store.get_project(project_id)
            context = self._build(project)
            self._contexts[project_id] = context
            return context

    def adopt(self, project_id: str, runtime: SessionTurnRuntime, sessions: SessionManager | None = None) -> ProjectRuntimeContext:
        """Keep a caller-owned runtime when its Project is created in place."""
        with self._lock:
            context = self._contexts.get(project_id)
            if context is not None:
                return context
            # A legacy runtime may temporarily carry the same Project id after
            # the server creates its first Project, but still point at the
            # legacy card/database paths. Never adopt those paths into the
            # Project-owned context; materialize the isolated context instead.
            expected_card = (self.state_root / project_id).resolve()
            expected_database = (self.sessions_root / f"{project_id}.sqlite3").resolve()
            if Path(runtime.card_folder).resolve() != expected_card or Path(runtime.database_path).resolve() != expected_database:
                project = self.project_store.get_project(project_id)
                context = self._build(project)
                self._contexts[project_id] = context
                return context
            context = ProjectRuntimeContext(
                project_id=project_id,
                runtime=runtime,
                sessions=sessions or self._session_manager(runtime, project_id),
                card_folder=Path(runtime.card_folder),
                database_path=Path(runtime.database_path),
            )
            self._contexts[project_id] = context
            return context

    def remember(self, project_id: str) -> None:
        with self._lock:
            state = self._read_state()
            recent = [item for item in state.get("recent_project_ids", []) if item != project_id]
            recent.insert(0, project_id)
            self._write_state({"active_project_id": project_id, "recent_project_ids": recent[:32]})

    def refresh(self, project: dict) -> None:
        with self._lock:
            context = self._contexts.get(project.get("id"))
            if context is not None:
                self._materialize_project(project, context.card_folder)

    def stage_discard(self, project_id: str) -> ProjectRuntimeDiscard:
        """Quarantine Project runtime state so deletion can still be rolled back."""
        if not isinstance(project_id, str) or not project_id:
            raise ValueError("project_id must be a non-empty string")
        with self._lock:
            context = self._contexts.pop(project_id, None)
            card_folder = context.card_folder if context is not None else self.state_root / project_id
            database_path = (
                context.database_path
                if context is not None
                else self.sessions_root / f"{project_id}.sqlite3"
            )
            previous_state = self._read_state()
            moves: list[tuple[Path, Path]] = []
            token = uuid4().hex
            sources = [
                (card_folder, self.state_root),
                (database_path, self.sessions_root),
                (Path(f"{database_path}-wal"), self.sessions_root),
                (Path(f"{database_path}-shm"), self.sessions_root),
            ]
            try:
                for source, root in sources:
                    resolved = self._owned_existing_path(source, root)
                    if resolved is None:
                        continue
                    quarantine = resolved.with_name(f".{resolved.name}.deleting-{token}")
                    resolved.replace(quarantine)
                    moves.append((resolved, quarantine))
                recent = [
                    item for item in previous_state.get("recent_project_ids", [])
                    if item != project_id
                ]
                active = previous_state.get("active_project_id")
                self._write_state({
                    "active_project_id": None if active == project_id else active,
                    "recent_project_ids": recent[:32],
                })
            except Exception:
                for original, quarantine in reversed(moves):
                    if quarantine.exists():
                        quarantine.replace(original)
                if context is not None:
                    self._contexts[project_id] = context
                self._write_state(previous_state)
                raise
            return ProjectRuntimeDiscard(
                owner=self._discard_owner, project_id=project_id, context=context,
                moves=tuple(moves), previous_state=previous_state,
            )

    def rollback_discard(self, discard: ProjectRuntimeDiscard) -> None:
        """Restore a staged Project runtime exactly once."""
        with self._lock:
            self._validate_discard(discard)
            for original, quarantine in reversed(discard.moves):
                if quarantine.exists():
                    quarantine.replace(original)
            if discard.context is not None:
                self._contexts[discard.project_id] = discard.context
            self._write_state(discard.previous_state)
            discard.finalized = True

    def commit_discard(self, discard: ProjectRuntimeDiscard) -> tuple[str, ...]:
        """Finalize a logical discard and best-effort its quarantined files."""
        with self._lock:
            self._validate_discard(discard)
            committed_moves: list[tuple[Path, Path]] = []
            try:
                for _, quarantine in discard.moves:
                    committed = quarantine.with_name(
                        quarantine.name.replace(".deleting-", ".deleted-", 1)
                    )
                    quarantine.replace(committed)
                    committed_moves.append((quarantine, committed))
            except OSError:
                try:
                    for quarantine, committed in reversed(committed_moves):
                        if committed.exists():
                            committed.replace(quarantine)
                except OSError:
                    raise ProjectRuntimeError(
                        "project_runtime_commit_compensation_failed",
                        "Project runtime commit compensation failed",
                    ) from None
                raise ProjectRuntimeError(
                    "project_runtime_commit_failed",
                    "Project runtime commit failed",
                ) from None

            pending: list[str] = []
            for _, committed in committed_moves:
                root = self.state_root if committed.parent == self.state_root else self.sessions_root
                try:
                    self._remove_owned_path(committed, root)
                except OSError:
                    pending.append(str(committed))
            discard.finalized = True
            return tuple(pending)

    def cleanup_quarantine(self) -> tuple[str, ...]:
        """Retry removal of runtime artifacts left by committed discards."""
        with self._lock:
            pending: list[str] = []
            for root in (self.state_root, self.sessions_root):
                for quarantine in root.glob(".*.deleted-*"):
                    try:
                        self._remove_owned_path(quarantine, root)
                    except OSError:
                        pending.append(str(quarantine))
            return tuple(pending)

    def discard(self, project_id: str) -> None:
        discard = self.stage_discard(project_id)
        self.commit_discard(discard)

    def _validate_discard(self, discard: ProjectRuntimeDiscard) -> None:
        if (
            not isinstance(discard, ProjectRuntimeDiscard)
            or discard.owner is not self._discard_owner
            or discard.finalized
        ):
            raise ValueError("invalid or finalized Project runtime discard")

    @staticmethod
    def _owned_existing_path(path: Path, root: Path) -> Path | None:
        try:
            resolved = path.resolve()
            resolved.relative_to(root.resolve())
        except (OSError, ValueError):
            return None
        if resolved == root.resolve() or not resolved.exists():
            return None
        return resolved

    def session_payload(self, project_id: str) -> dict:
        with self._lock:
            context = self._contexts.get(project_id)
            if context is not None:
                sessions = context.sessions.list_sessions()
                return {
                    "last_session_id": context.sessions.active_session_id,
                    "sessions": sessions,
                }
            database_path = self.sessions_root / f"{project_id}.sqlite3"
            active_session_id = SessionManager.load_active_session_id(database_path)
        return {
            "last_session_id": active_session_id,
            "sessions": [],
        }

    def _build(self, project: dict) -> ProjectRuntimeContext:
        project_id = project["id"]
        card_folder = self.state_root / project_id
        database_path = self.sessions_root / f"{project_id}.sqlite3"
        self._materialize_project(project, card_folder)

        manifest_policy = getattr(self._initial_runtime, "manifest_policy", None)
        session_settings = copy.deepcopy(getattr(self._initial_runtime, "session_settings", {}))

        def build_runtime(session_id, *, bootstrap_legacy_history=False):
            return SessionTurnRuntime(
                database_path=database_path,
                card_folder=card_folder,
                projection_root=self.projection_root,
                session_id=session_id,
                session_settings=session_settings,
                manifest_policy=manifest_policy,
                bootstrap_legacy_history=bootstrap_legacy_history,
                project_id=project_id,
            )

        active_session_id = SessionManager.load_active_session_id(database_path)
        runtime = build_runtime(active_session_id, bootstrap_legacy_history=False)
        default_opening = self._default_opening(project)
        if runtime.opening_turn() is None and runtime.active_revision() == 0 and default_opening is not None:
            runtime.set_opening_turn(default_opening, event_type="project.opening_restored", event_payload={"project_id": project_id})
        sessions = SessionManager(runtime, build_runtime, default_opening=runtime.opening_turn(), initial_title="主存档")
        runtime.resume_projection()
        return ProjectRuntimeContext(project_id, runtime, sessions, card_folder, database_path)

    def _session_manager(self, runtime: SessionTurnRuntime, project_id: str) -> SessionManager:
        def build_runtime(session_id, *, bootstrap_legacy_history=False):
            return SessionTurnRuntime(
                database_path=runtime.database_path,
                card_folder=runtime.card_folder,
                projection_root=runtime.projection.projection_root,
                session_id=session_id,
                session_settings=copy.deepcopy(runtime.session_settings),
                manifest_policy=runtime.manifest_policy,
                bootstrap_legacy_history=bootstrap_legacy_history,
                project_id=project_id,
            )

        return SessionManager(runtime, build_runtime, default_opening=runtime.opening_turn())

    @staticmethod
    def _default_opening(project: dict) -> dict | None:
        openings = project.get("openings") if isinstance(project, dict) else None
        if not isinstance(openings, list):
            return None
        opening = next((item for item in openings if item.get("is_default")), openings[0] if openings else None)
        if not isinstance(opening, dict) or not isinstance(opening.get("content"), str) or not opening["content"].strip():
            return None
        return {
            "user": "",
            "content": opening["content"],
            "summary": "",
            "options": "",
            "is_opening": True,
            "index": 0,
        }

    def _materialize_project(self, project: dict, card_folder: Path) -> None:
        card_folder.mkdir(parents=True, exist_ok=True)
        memory = card_folder / "memory"
        memory.mkdir(parents=True, exist_ok=True)
        openings = project.get("openings") if isinstance(project.get("openings"), list) else []
        default = next((item for item in openings if item.get("is_default")), openings[0] if openings else None)
        alternates = [item.get("content", "") for item in openings if item is not default]
        prompt = project.get("card_prompt") if isinstance(project.get("card_prompt"), dict) else {}
        card_data = {
            "name": project.get("name", project["id"]),
            "avatar": project.get("avatar", ""),
            "description": project.get("description", ""),
            "personality": project.get("personality", ""),
            "scenario": project.get("scenario", ""),
            "system_prompt": prompt.get("system", ""),
            "post_history_instructions": prompt.get("post_history", ""),
            "first_mes": default.get("content", "") if isinstance(default, dict) else "",
            "alternate_greetings": alternates,
        }
        self._atomic_json(card_folder / ".card_data.json", card_data)
        initvar = project.get("variables") if isinstance(project.get("variables"), dict) else {}
        initvar_path = card_folder / ".initvar.json"
        if not initvar_path.exists():
            self._atomic_json(initvar_path, initvar)
        for filename, value in (("chat_log.json", []), ("content.js", "window.CONTENT_HTML = '';\n"), ("state.js", "window.STATE = {};\n")):
            path = card_folder / filename
            if not path.exists():
                if filename.endswith(".json"):
                    self._atomic_json(path, value)
                else:
                    path.write_text(value, encoding="utf-8")

    def _read_state(self) -> dict:
        try:
            value = json.loads(self._active_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def _write_state(self, state: dict) -> None:
        self._active_path.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_json(self._active_path, state)

    @staticmethod
    def _remove_owned_path(path: Path, root: Path) -> None:
        """Delete a known Project artifact only when it is inside ``root``."""
        try:
            resolved = path.resolve()
            resolved.relative_to(root.resolve())
        except (OSError, ValueError):
            return
        if resolved == root.resolve() or not resolved.exists():
            return
        if resolved.is_dir():
            shutil.rmtree(resolved)
        else:
            resolved.unlink()

    @staticmethod
    def _atomic_json(path: Path, value) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with open(descriptor, "w", encoding="utf-8", closefd=True) as handle:
                json.dump(value, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            Path(temporary).replace(path)
        finally:
            Path(temporary).unlink(missing_ok=True)
